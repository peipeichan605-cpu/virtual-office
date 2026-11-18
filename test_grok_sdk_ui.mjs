import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {GrokSdkController,grokSdkLoginUrl,grokSdkRequestStore,grokSdkError,initGrokSdkUI} from './public/office/grok-sdk-ui.js';
import {initNewConversationUI} from './public/office/new-conversation-ui.js';
import {officeGet,officePost} from './public/office/office-api.js';

const ID='00000000-0000-4000-8000-000000000001',BOT='00000000-0000-4000-8000-000000000002',SESSION='00000000-0000-4000-8000-000000000003';
const LOGIN={login_id:ID,request_id:ID,login_url:'https://cursor.com/loginDeepControl?uuid='+ID,state:'pending'};
const CONNECT={connected:true,request_id:ID,agent_id:'grok_bot:sdk-example',name:'My Bot',mode:'grok_sdk',same_session:true,bot_id:BOT,session_id:SESSION};
const loggedOut={auth:{state:'logged_out'}},authenticated={auth:{state:'authenticated'}};
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const wait=()=>new Promise(resolve=>setImmediate(resolve));
function storage(){const values=new Map(),raw={getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)};return {values,store:grokSdkRequestStore(()=>raw,'fixture')};}
function fixture(overrides={}){
  const saved=storage(),calls=[],connected=[];
  const defaults={read:()=>authenticated,login:()=>LOGIN,poll:()=>({login_id:ID,state:'pending'}),cancel:()=>({state:'cancelled'}),bots:()=>({bots:[{id:BOT,name:'My Bot'},{id:'bot-two',name:'Second Bot'}]}),sessions:id=>({bot_id:id,sessions:[{session_id:SESSION,kind:'chat'}]}),connect:()=>CONNECT};
  const transport=Object.fromEntries(Object.keys(defaults).map(key=>[key,async(...args)=>{calls.push([key,...args]);return (overrides[key]||defaults[key])(...args);} ]));
  const controller=new GrokSdkController({transport,pendingStore:overrides.pendingStore||saved.store,makeId:()=>ID,onConnected:(agent,options)=>connected.push({agent,options})});
  return {controller,calls,connected,...saved};
}
async function selected(f,session=SESSION){await f.controller.open();await f.controller.selectBot(BOT);f.controller.selectSession(session);}

test('SDK controller does nothing until opened and multiple chats require an explicit session selection',async()=>{
  const f=fixture({sessions:id=>({bot_id:id,sessions:[{session_id:SESSION},{session_id:'other-chat'}]})});assert.equal(f.calls.length,0);await f.controller.open();assert.deepEqual(f.calls.map(row=>row[0]),['read','bots']);assert.equal(f.controller.botId,'');assert.equal(f.controller.sessionId,null);
  assert.equal(await f.controller.connect('My Bot'),false);assert.equal(f.calls.filter(row=>row[0]==='connect').length,0);
  await f.controller.selectBot(BOT);assert.equal(f.controller.sessionId,null);assert.equal(await f.controller.connect('My Bot'),false);
  f.controller.selectSession(SESSION);assert.equal(await f.controller.connect('My Bot'),true);assert.equal(f.controller.phase,'configured');assert.equal(f.connected.length,1);assert.equal(f.connected[0].agent.mode,'grok_sdk');assert.equal(f.values.size,0);
});

test('fresh login starts only on explicit action, uses a safe link, and never returns or stores secrets',async()=>{
  const f=fixture({read:()=>loggedOut,login:()=>({...LOGIN,accessToken:'secret-token',refreshToken:'secret-refresh'})});await f.controller.open();assert.deepEqual(f.calls.map(row=>row[0]),['read']);assert.equal(f.controller.phase,'login_required');
  await f.controller.startLogin();assert.equal(f.controller.phase,'login_pending');assert.deepEqual([...f.values.values()],[ID]);assert.equal(JSON.stringify(f.controller.login).includes('secret'),false);assert.equal(await f.controller.startLogin(),false);assert.equal(f.calls.filter(row=>row[0]==='login').length,1);
  assert.equal(grokSdkLoginUrl('https://cursor.com/loginDeepControl?uuid=test'),'https://cursor.com/loginDeepControl?uuid=test');
  for(const bad of ['javascript:alert(1)','http://cursor.com/loginDeepControl','https://cursor.com.evil.test/loginDeepControl','https://user:pass@cursor.com/loginDeepControl','https://cursor.com/loginDeepControl#secret','https://cursor.com:444/loginDeepControl','https://cursor.com/settings'])assert.equal(grokSdkLoginUrl(bad),null);
});

test('login POST timeout is recovered by read-only status using the saved UUID',async()=>{
  const saved=storage(),f=fixture({pendingStore:saved.store,read:()=>loggedOut,login:()=>{throw new Error('timeout');}});await f.controller.open();await f.controller.startLogin();assert.equal(f.controller.phase,'login_unknown');assert.equal(await f.controller.startLogin(),false);
  const recovery=fixture({pendingStore:saved.store,read:args=>{assert.equal(args.loginRequestId,ID);return {...loggedOut,latest_login:LOGIN};}});await recovery.controller.open();assert.equal(recovery.controller.phase,'login_pending');assert.equal(recovery.calls.length,1);assert.equal(recovery.controller.login.login_id,ID);
});

test('completed login loads a roster but does not select or register a Bot',async()=>{
  const f=fixture({read:()=>loggedOut,poll:()=>({login_id:ID,auth:{state:'authenticated'}})});await f.controller.open();await f.controller.startLogin();await f.controller.pollLogin();assert.equal(f.controller.phase,'selecting');assert.equal(f.controller.auth,'authenticated');assert.equal(f.controller.login,null);assert.equal(f.controller.botId,'');assert.equal(f.controller.sessionId,null);assert.equal(f.values.size,0);assert.equal(f.calls.filter(row=>row[0]==='connect').length,0);assert.equal(f.controller.pollFlight,null);
});

test('pending login remains pending even when account auth is not yet established',async()=>{
  const f=fixture({read:()=>loggedOut,poll:()=>({login_id:ID,state:'pending',auth:{state:'logged_out'}})});await f.controller.open();await f.controller.startLogin();await f.controller.pollLogin();assert.equal(f.controller.phase,'login_pending');assert.equal(f.controller.loginRequestId,ID);assert.equal(f.store.load('login'),ID);
});

test('reopening an expired login releases its saved UUID rather than trapping the user in unknown state',async()=>{
  const saved=storage();saved.store.save('login',ID);const f=fixture({pendingStore:saved.store,read:()=>({...loggedOut,login:{request_id:ID,login_id:ID,state:'expired'}})});await f.controller.open();assert.equal(f.controller.phase,'login_required');assert.equal(f.controller.loginRequestId,null);assert.equal(saved.values.size,0);assert.match(f.controller.error,/過期/);
});

test('an earlier status cannot undo a confirmed login poll',async()=>{
  const status=deferred();let reads=0;const f=fixture({read:()=>++reads===1?loggedOut:status.promise,poll:()=>({login_id:ID,state:'authenticated',auth:{state:'authenticated'}})});await f.controller.open();await f.controller.startLogin();const reading=f.controller.refresh();await f.controller.pollLogin();status.resolve({...loggedOut,login:LOGIN});await reading;assert.equal(f.controller.auth,'authenticated');assert.equal(f.controller.phase,'selecting');assert.equal(f.controller.login,null);
});

test('expired or cancelled login clears only the pending login receipt and requires new action',async()=>{
  const f=fixture({read:()=>loggedOut,poll:()=>({state:'expired'})});await f.controller.open();await f.controller.startLogin();await f.controller.pollLogin();assert.equal(f.controller.phase,'login_required');assert.equal(f.values.size,0);assert.match(f.controller.error,/過期/);
  await f.controller.startLogin();await f.controller.cancelLogin();assert.equal(f.controller.phase,'login_required');assert.equal(f.controller.login,null);assert.equal(f.values.size,0);assert.match(f.controller.error,/取消/);
});

test('late login and poll responses do not reopen a closed dialog or start enrollment',async()=>{
  const begin=deferred(),poll=deferred(),f=fixture({read:()=>loggedOut,login:()=>begin.promise,poll:()=>poll.promise});await f.controller.open();const request=f.controller.startLogin();f.controller.close();begin.resolve(LOGIN);await request;assert.equal(f.controller.phase,'closed');assert.equal(f.connected.length,0);
  const restored=fixture({read:()=>({...loggedOut,latest_login:LOGIN}),pendingStore:f.store,poll:()=>poll.promise});await restored.controller.open();const reading=restored.controller.pollLogin();restored.controller.close();poll.resolve({auth:{state:'authenticated'}});await reading;assert.equal(restored.controller.phase,'closed');assert.equal(restored.calls.filter(row=>row[0]==='bots').length,0);
});

test('cancel and a simultaneous authenticated poll cannot falsely report a completed cancellation',async()=>{
  const polling=deferred(),f=fixture({read:()=>loggedOut,poll:()=>polling.promise,cancel:()=>({auth:{state:'authenticated'}})});await f.controller.open();await f.controller.startLogin();const reading=f.controller.pollLogin();await f.controller.cancelLogin();polling.resolve({auth:{state:'authenticated'}});await reading;assert.equal(f.controller.auth,'authenticated');assert.equal(f.controller.phase,'selecting');assert.equal(f.calls.filter(row=>row[0]==='bots').length,1);
});

test('switching Bots fences stale sole-session responses out of the current selection',async()=>{
  const first=deferred(),f=fixture({sessions:id=>id===BOT?first.promise:{bot_id:id,sessions:[{session_id:'second-session',kind:'main'}]}});await f.controller.open();const one=f.controller.selectBot(BOT);await f.controller.selectBot('bot-two');first.resolve({bot_id:BOT,sessions:[{session_id:''}]});await one;assert.equal(f.controller.botId,'bot-two');assert.deepEqual(f.controller.sessions.map(row=>row.id),['second-session']);assert.equal(f.controller.sessionId,'second-session');assert.equal(f.calls.filter(row=>row[0]==='connect').length,0);
});

test('the sole server-confirmed main session is selected without automatically connecting',async()=>{
  const f=fixture({sessions:id=>({bot_id:id,sessions:[{session_id:'',label:'主要聊天'}]}),connect:data=>({...CONNECT,session_id:data.session_id})});await f.controller.open();assert.equal(f.controller.sessionId,null);await f.controller.selectBot(BOT);assert.equal(f.controller.sessionId,'');assert.equal(f.calls.filter(row=>row[0]==='connect').length,0);
  assert.equal(await f.controller.connect('Main Chat'),true);assert.equal(f.calls.find(row=>row[0]==='connect')[1].session_id,'');
});

test('an explicit choice among multiple chats survives reloading that same Bot',async()=>{
  const f=fixture({sessions:id=>({bot_id:id,sessions:[{session_id:''},{session_id:SESSION}]})});await f.controller.open();await f.controller.selectBot(BOT);assert.equal(f.controller.sessionId,null);f.controller.selectSession('');await f.controller.selectBot(BOT);assert.equal(f.controller.sessionId,'');assert.equal(f.calls.filter(row=>row[0]==='connect').length,0);
});

test('uncertain connection is read-only reconciled after reload and cannot duplicate POST',async()=>{
  const saved=storage(),f=fixture({pendingStore:saved.store,connect:()=>{throw new Error('timeout after commit');}});await selected(f);await f.controller.connect('My Bot');assert.equal(f.controller.phase,'connect_unknown');assert.equal(saved.store.load('connect'),ID);assert.equal(await f.controller.connect('My Bot'),false);
  const recovery=fixture({pendingStore:saved.store,read:({requestId})=>({...authenticated,request:{request_id:requestId,state:'completed',response:CONNECT}})});await recovery.controller.open();assert.equal(recovery.controller.phase,'configured');assert.equal(recovery.connected.length,1);assert.equal(recovery.calls.length,1);assert.equal(saved.values.size,0);
});

test('only matching completed receipts clear pending writes; unknown status does not authorize retries',async()=>{
  for(const request of [undefined,{request_id:'different',state:'not_found'},{request_id:ID,state:'unknown'},{request_id:ID,state:'completed',response:{...CONNECT,mode:'grok_webhook'}}]){
    const saved=storage();saved.store.save('connect',ID);const f=fixture({pendingStore:saved.store,read:()=>({...authenticated,request})});await f.controller.open();assert.equal(f.controller.phase,'connect_unknown');assert.equal(await f.controller.connect('My Bot'),false);assert.equal(saved.store.load('connect'),ID);
  }
});

test('double submit and late response are fenced, preserving successful registration after closing',async()=>{
  const post=deferred(),f=fixture({connect:()=>post.promise});await selected(f);const first=f.controller.connect('My Bot');assert.equal(await f.controller.connect('My Bot'),false);f.controller.close();post.resolve(CONNECT);assert.equal(await first,true);assert.equal(f.controller.phase,'closed');assert.equal(f.connected.length,1);assert.equal(f.connected[0].options.focus,false);assert.equal(f.calls.filter(row=>row[0]==='connect').length,1);
});

test('mismatched native identity receipt never reports success',async()=>{
  for(const response of [{...CONNECT,bot_id:'another-bot'},{...CONNECT,session_id:'other-chat'},{...CONNECT,same_session:false},{...CONNECT,request_id:'other-request'}]){
    const f=fixture({connect:()=>response});await selected(f);await f.controller.connect('My Bot');assert.equal(f.controller.phase,'connect_unknown');assert.equal(f.connected.length,0);assert.equal(f.store.load('connect'),ID);
  }
});

test('storage failure prevents writes and unknown errors cannot expose account data',async()=>{
  const broken={load(){return null;},save(){throw new Error('blocked');},clear(){}};const f=fixture({pendingStore:broken,read:()=>loggedOut});await f.controller.open();await f.controller.startLogin();assert.equal(f.calls.length,1);assert.match(f.controller.error,/分頁儲存/);
  assert.equal(grokSdkError(new Error('secret account token /private/path')).includes('secret'),false);const ready=fixture({pendingStore:broken});await selected(ready);await ready.controller.connect('My Bot');assert.equal(ready.calls.filter(row=>row[0]==='connect').length,0);
});

class Element{
  constructor(tag,doc){this.tagName=tag.toUpperCase();this.doc=doc;this.children=[];this.attrs={};this.handlers=new Map();this.value='';this.hidden=false;this.disabled=false;this.open=false;}
  setAttribute(n,v){this.attrs[n]=String(v);if(n==='class')this.className=v;if(n==='name')this.name=v;if(n==='type')this.type=v;if(n==='value')this.value=v;if(n==='hidden')this.hidden=true;}
  getAttribute(n){return this.attrs[n]??null;}
  append(...els){for(const el of els){this.children.push(el);el.parent=this;}}
  replaceChildren(...els){this.children=[];this.append(...els);}
  get options(){return this.children.filter(x=>x.tagName==='OPTION');}
  get elements(){const entries=this.all().filter(x=>x.name),out=[...entries];for(const x of entries)out[x.name]=x;return out;}
  all(){return this.children.flatMap(x=>[x,...x.all()]);}
  querySelector(q){return this.all().find(x=>q[0]==='.'?String(x.className||'').split(' ').includes(q.slice(1)):q.includes('[')?x.tagName===q.split('[')[0].toUpperCase()&&x.getAttribute('type')===q.match(/type="([^"]+)"/)[1]:x.tagName===q.toUpperCase())||null;}
  addEventListener(type,handler){if(!this.handlers.has(type))this.handlers.set(type,[]);this.handlers.get(type).push(handler);}
  async fire(type){for(const fn of this.handlers.get(type)||[])await fn({preventDefault(){},stopPropagation(){}});}
  showModal(){this.open=true;}close(){this.open=false;this.fire('close');}focus(){this.doc.activeElement=this;}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(x=>x!==this);}
  set innerHTML(html){const stack=[this],voids=new Set(['INPUT','BR']);for(const token of html.match(/<[^>]+>|[^<]+/g)||[]){if(token.startsWith('</')){stack.pop();continue;}if(token[0]!=='<')continue;const tag=token.match(/^<([\w-]+)/)?.[1];if(!tag)continue;const node=new Element(tag,this.doc);for(const m of token.slice(tag.length+1,-1).matchAll(/([\w-]+)(?:="([^"]*)")?/g))node.setAttribute(m[1],m[2]??'');stack.at(-1).append(node);if(!voids.has(node.tagName))stack.push(node);}}
}
function dom(){const doc={createElement:tag=>new Element(tag,doc)};doc.body=new Element('body',doc);return doc;}

test('chooser has no default platform or network calls; only explicit Grok choice reads SDK status',async t=>{
  const doc=dom(),events=new EventTarget(),calls=[],pendingStore=storage().store;
  t.mock.method(globalThis,'fetch',async(url)=>{calls.push(url);return {ok:true,json:async()=>loggedOut};});
  const idleStore={load:()=>null,save(){},clear(){}};
  const ui=initNewConversationUI({document:doc,events,key:()=> 'fixture-owner',grokOptions:{pendingStore},cursorOptions:{pendingStore:idleStore},codexOptions:{pendingStore:idleStore}});assert.equal(calls.length,0);events.dispatchEvent(new Event('office:new-conversation'));assert.equal(ui.dialog.open,true);assert.equal(ui.grok.dialog.open,false);assert.equal(calls.length,0);
  await ui.dialog.querySelector('.office-platform-grok').fire('click');await wait();assert.equal(ui.dialog.open,false);assert.equal(ui.grok.dialog.open,true);assert.deepEqual(calls,['/api/office/grok/sdk/status']);assert.equal(ui.grok.controller.phase,'login_required');assert.equal(ui.grok.dialog.querySelector('form').elements.api_key,undefined);
  ui.dispose();assert.equal(doc.body.children.length,0);
});

test('actual form displays the sole main chat, enables explicit connect and never submits automatically',async t=>{
  const doc=dom(),events=new EventTarget(),calls=[];
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([url,options]);return {ok:true,json:async()=>url.endsWith('/status')?authenticated:url.endsWith('/bots')?{bots:[{id:BOT,name:'My Bot'}]}:{bot_id:BOT,sessions:[{session_id:'',label:'主要聊天'}]}};});
  const ui=initGrokSdkUI({document:doc,events,key:()=> 'fixture-owner',pendingStore:storage().store});events.dispatchEvent(new Event('office:new-grok-sdk-conversation'));await wait();const form=ui.dialog.querySelector('form');form.elements.bot.value=BOT;await form.elements.bot.fire('change');await wait();
  assert.equal(form.elements.session.value,'0');assert.equal(form.elements.session.options[1].value,'0');assert.match(form.elements.session.options[1].textContent,/主要聊天/);assert.equal(form.querySelector('button[type="submit"]').disabled,false);assert.match(ui.dialog.querySelector('.grok-sdk-selection-note').textContent,/已自動選擇.*唯一.*主要聊天/);
  assert.equal(ui.controller.sessionId,'');assert.equal(calls.filter(row=>row[1]?.method==='POST').length,0);ui.dispose();
});

test('form explains loading, missing, multiple and unnamed selections; suggested names follow Bot switches',async t=>{
  const doc=dom(),events=new EventTarget(),calls=[],first=deferred();let pendingFirst=true;
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    calls.push([url,options]);
    if(url.endsWith('/status'))return {ok:true,json:async()=>authenticated};
    if(url.endsWith('/bots'))return {ok:true,json:async()=>({bots:[{id:BOT,name:'First Bot'},{id:'bot-two',name:'Second Bot'},{id:'bot-empty',name:'Empty Bot'}]})};
    const id=new URL(url,'https://fixture').searchParams.get('bot_id');
    if(id===BOT&&pendingFirst){pendingFirst=false;return first.promise;}
    return {ok:true,json:async()=>({bot_id:id,sessions:id==='bot-empty'?[]:id==='bot-two'?[{session_id:'',label:'主要聊天'},{session_id:SESSION,label:'專案聊天'}]:[{session_id:'',label:'主要聊天'}]})};
  });
  const ui=initGrokSdkUI({document:doc,events,key:()=> 'fixture-owner',pendingStore:storage().store});events.dispatchEvent(new Event('office:new-grok-sdk-conversation'));await wait();const form=ui.dialog.querySelector('form'),note=ui.dialog.querySelector('.grok-sdk-selection-note'),button=form.querySelector('button[type="submit"]');
  assert.equal(button.disabled,true);assert.match(note.textContent,/先選擇一隻 Bot/);
  form.elements.bot.value=BOT;await form.elements.bot.fire('change');assert.equal(form.elements.name.value,'First Bot');assert.equal(button.disabled,true);assert.match(note.textContent,/正在讀取/);
  form.elements.bot.value='bot-two';await form.elements.bot.fire('change');await wait();assert.equal(form.elements.name.value,'Second Bot');assert.equal(form.elements.session.value,'');assert.equal(button.disabled,true);assert.match(note.textContent,/多個聊天/);
  first.resolve({ok:true,json:async()=>({bot_id:BOT,sessions:[{session_id:'',label:'主要聊天'}]})});await wait();assert.equal(form.elements.bot.value,'bot-two');assert.equal(form.elements.name.value,'Second Bot');assert.equal(form.elements.session.value,'');assert.equal(button.disabled,true);
  form.elements.name.value='My custom name';await form.elements.name.fire('input');form.elements.bot.value=BOT;await form.elements.bot.fire('change');await wait();assert.equal(form.elements.name.value,'My custom name');assert.equal(form.elements.session.value,'0');assert.equal(button.disabled,false);
  form.elements.name.value='';await form.elements.name.fire('input');assert.equal(button.disabled,true);assert.match(note.textContent,/請填寫 OFFICE 名稱/);
  form.elements.name.value='Renamed';await form.elements.name.fire('input');assert.equal(button.disabled,false);form.elements.bot.value='bot-empty';await form.elements.bot.fire('change');await wait();assert.equal(form.elements.name.value,'Renamed');assert.equal(button.disabled,true);assert.match(note.textContent,/沒有可確認的聊天/);
  assert.equal(calls.filter(row=>row[1]?.method==='POST').length,0);ui.dispose();
});

test('login button and verified-state copy remain separate; secret fields and automatic browser open are absent',()=>{
  const source=readFileSync(new URL('./public/office/grok-sdk-ui.js',import.meta.url),'utf8');assert.match(source,/待實際收發驗證/);assert.match(source,/非官方 SDK/);assert.match(source,/授權涵蓋帳戶，比單一 Webhook 的範圍廣/);assert.doesNotMatch(source,/window\.open|accessToken|refreshToken|type="password"/);assert.match(source,/rel="noopener noreferrer"/);
});

test('SDK network budget covers bounded login and connection reads without changing other platform limits',async t=>{
  const timeouts=[],calls=[];
  t.mock.method(AbortSignal,'timeout',ms=>{timeouts.push(ms);return new AbortController().signal;});
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>({})};});
  await officeGet('/api/office/grok/sdk/login/status?login_id='+ID,'fixture-key');
  await officePost('/api/office/grok/sdk/login','fixture-key',{request_id:ID});
  await officePost('/api/office/grok/sdk/connect','fixture-key',{request_id:ID,bot_id:BOT,session_id:''});
  await officeGet('/api/office/codex/background/status','fixture-key');
  await officePost('/api/office/codex/background/create','fixture-key',{});
  await officePost('/api/office/cursor/local/create','fixture-key',{});
  await officePost('/api/office/commands','fixture-key',{});
  assert.deepEqual(timeouts,[60000,60000,60000,5000,30000,30000,10000]);
  assert.ok(calls.every(row=>row.options.headers['X-Office-Key']==='fixture-key'&&row.options.credentials==='same-origin'));
});

test('SDK vendor access denial stays distinct from Office authentication denial',async t=>{
  let detail={error:'grok_sdk_access_denied'};
  t.mock.method(globalThis,'fetch',async()=>({ok:false,status:403,json:async()=>detail}));
  await assert.rejects(officeGet('/api/office/grok/sdk/bots','fixture-key'),error=>error.code==='grok_sdk_access_denied'&&error.status===403&&!error.message.includes('私人 OFFICE'));
  await assert.rejects(officeGet('/api/office/commands','fixture-key'),error=>error.code==='private_office_required');
  for(const value of [{error:'forbidden'},{error:'private_office_required'},{error:'unexpected secret account text'},{}]){
    detail=value;await assert.rejects(officeGet('/api/office/grok/sdk/bots','fixture-key'),error=>error.code==='private_office_required'&&!error.message.includes('secret'));
  }
});
