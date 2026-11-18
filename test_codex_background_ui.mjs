import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {CodexBackgroundController,codexBackgroundError,codexBackgroundRequestStore,initCodexBackgroundUI} from './public/office/codex-background-ui.js';
import {initNewConversationUI} from './public/office/new-conversation-ui.js';
import {cursorSdkRequestStore} from './public/office/cursor-sdk-ui.js';
import {receivingHint,publicSyncHint,CommandComposer,codexBackgroundFailureHint} from './public/office/commands.js';
import {officePost} from './public/office/office-api.js';

const ID='00000000-0000-4000-8000-000000000101',AGENT='codex:bg-00000000-0000-4000-8000-000000000201';
const ready={available:true,logged_in:true,model:'configured-model',models:[{id:'selected-model',name:'Selected model',is_default:true}],workspace:'/selected/project',workspaces:[{name:'Project',path:'/selected/project'}]};
const session={request_id:ID,agent_id:AGENT,name:'Codex fixture',mode:'codex_background',execution_verified:false};
const values={name:'Codex fixture',workspace:'/selected/project'};
const wait=()=>new Promise(resolve=>setImmediate(resolve));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
function storeFixture(){const values=new Map(),storage={getItem:k=>values.get(k)||null,setItem:(k,v)=>values.set(k,v),removeItem:k=>values.delete(k)};return {values,storage,store:codexBackgroundRequestStore(()=>storage,'http://localhost:4321/office/')};}
function fixture(overrides={}){
  const calls=[],changes=[],created=[];let number=0;
  const controller=new CodexBackgroundController({pendingStore:overrides.pendingStore||storeFixture().store,transport:{read:async id=>{calls.push(['read',id]);return overrides.read?overrides.read(id):ready;},create:async body=>{calls.push(['create',{...body}]);return overrides.create?overrides.create(body):{...session,request_id:body.request_id};}},makeId:()=>number++?ID.replace(/101$/,'102'):ID,onChange:c=>changes.push(c.phase),onCreated:(s,options)=>created.push({session:s,options})});
  return {controller,calls,changes,created};
}

test('Codex unavailable or unconfirmed login cannot create; public status discards credential-like fields',async()=>{
  for(const home of [{...ready,available:false,reason:'codex_runtime_required'},{...ready,logged_in:false,reason:'codex_login_required'}]){
    const f=fixture({read:()=>({...home,api_key:'must-not-retain',exception:'/private/auth.json'})});await f.controller.open();
    assert.equal(f.controller.phase,'unavailable');assert.equal(await f.controller.create(values),false);assert.equal(f.calls.length,1);
    assert.ok(!JSON.stringify(f.controller).includes('must-not-retain'));assert.ok(!JSON.stringify(f.controller).includes('/private/auth.json'));
  }
});

test('registration sends only intended setup fields and does not manufacture native execution proof',async()=>{
  const store=storeFixture(),f=fixture({pendingStore:store.store,create:body=>({...session,request_id:body.request_id,api_key:'must-discard'})});await f.controller.open();
  assert.equal(await f.controller.create({...values,api_key:'no-key',create_folder:true,approval_policy:'never'}),true);
  assert.deepEqual(f.calls.find(c=>c[0]==='create')[1],{request_id:ID,...values,create_folder:true});
  assert.deepEqual(f.controller.created,{agent_id:AGENT,name:session.name,scope:'office_owned_session'});
  assert.equal(f.controller.phase,'created');assert.equal(store.values.size,0);assert.equal(f.created.length,1);
  assert.ok(!JSON.stringify(f.controller).includes('must-discard'));
});

test('folder creation is explicit and never inferred from workspace text',async()=>{
  for(const choice of [false,true,'true',undefined]){const f=fixture();await f.controller.open();await f.controller.create({...values,create_folder:choice});assert.equal(f.calls.at(-1)[1].create_folder,choice===true?true:undefined);}
});

test('configured and catalog default models never become overrides; explicit choice is forwarded and invalid models are rejected',async()=>{
  for(const model of [undefined,'','selected-model']){
    const f=fixture();await f.controller.open();assert.equal(await f.controller.create({...values,model}),true);
    const body=f.calls.find(c=>c[0]==='create')[1];assert.equal(body.model,model||undefined);assert.equal(Object.hasOwn(body,'model'),Boolean(model));
  }
  for(const model of ['bad/model','bad model',{id:'selected-model'},'x'.repeat(129)]){
    const f=fixture();await f.controller.open();assert.equal(await f.controller.create({...values,model}),false);assert.equal(f.calls.length,1);assert.match(f.controller.error,/模型/);
  }
});

test('pending registration captures its explicit model and cannot be replayed with a changed choice',async()=>{
  const post=deferred(),store=storeFixture(),f=fixture({pendingStore:store.store,create:()=>post.promise});await f.controller.open();
  const draft={...values,model:'selected-model'},pending=f.controller.create(draft);draft.model='another-model';assert.equal(await f.controller.create(draft),false);
  post.reject(new Error('transport timeout'));await pending;assert.equal(await f.controller.create(draft),false);assert.equal(f.calls.filter(c=>c[0]==='create').length,1);assert.equal(f.calls.at(-1)[1].model,'selected-model');assert.deepEqual([...store.values.values()],[ID]);
  await f.controller.refresh();assert.deepEqual(f.calls.at(-1),['read',ID]);
});

test('uncertain POST retains UUID across reload and GET reconciles completion without a second POST',async()=>{
  const store=storeFixture(),f=fixture({pendingStore:store.store,create:()=>{throw new Error('network timeout');}});await f.controller.open();await f.controller.create(values);
  assert.equal(f.controller.phase,'unknown');assert.equal(store.store.load(),ID);assert.equal(await f.controller.create(values),false);
  const recovered=fixture({pendingStore:store.store,read:id=>({...ready,request:{request_id:id,state:'completed',response:session}})});await recovered.controller.open();
  assert.equal(recovered.controller.phase,'created');assert.equal(recovered.calls.length,1);assert.deepEqual(recovered.calls[0],['read',ID]);assert.equal(recovered.created.length,1);assert.equal(store.values.size,0);
});

test('unknown, missing, and unrelated receipts never authorize blind POST retries',async()=>{
  const store=storeFixture();store.store.save(ID);
  for(const receipt of [undefined,{request_id:ID,state:'unknown'},{request_id:ID.replace(/101$/,'102'),state:'not_found'},{request_id:ID,state:'completed',response:{...session,request_id:'different'}}]){
    const f=fixture({pendingStore:store.store,read:()=>({...ready,request:receipt})});await f.controller.open();assert.equal(f.controller.phase,'unknown');assert.equal(await f.controller.create(values),false);assert.equal(store.store.load(),ID);
  }
});

test('definitive missing or revoked receipts release old UUID but in-flight not_found cannot duplicate registration',async()=>{
  const post=deferred(),store=storeFixture(),f=fixture({pendingStore:store.store,create:()=>post.promise,read:id=>({...ready,request:id?{request_id:id,state:'not_found'}:undefined})});
  await f.controller.open();const pending=f.controller.create(values);await f.controller.refresh();assert.equal(f.controller.phase,'creating');assert.equal(await f.controller.create(values),false);assert.equal(store.store.load(),ID);
  post.reject(new Error('timeout'));await pending;await f.controller.refresh();assert.equal(f.controller.phase,'ready');assert.equal(store.store.load(),null);
  store.store.save(ID);const revoked=fixture({pendingStore:store.store,read:()=>({...ready,request:{request_id:ID,state:'revoked',reason:'agent_membership_changed'}})});await revoked.controller.open();assert.equal(revoked.controller.phase,'ready');assert.match(revoked.controller.error,/已登出/);assert.equal(store.store.load(),null);
});

test('double submit is fenced and a registration finishing after close never steals focus',async()=>{
  const post=deferred(),f=fixture({create:()=>post.promise});await f.controller.open();const first=f.controller.create(values);assert.equal(await f.controller.create(values),false);
  f.controller.close();post.resolve(session);assert.equal(await first,true);assert.equal(f.controller.phase,'closed');assert.equal(f.created.length,1);assert.equal(f.created[0].options.focus,false);assert.equal(f.calls.filter(c=>c[0]==='create').length,1);
});

test('close and reopen preserves pending identity; the earlier POST cannot focus the new dialog',async()=>{
  const post=deferred(),f=fixture({create:()=>post.promise});await f.controller.open();const pending=f.controller.create(values);f.controller.close();await f.controller.open();
  assert.equal(f.controller.phase,'creating');assert.equal(await f.controller.create(values),false);post.resolve(session);await pending;
  assert.equal(f.controller.phase,'created');assert.equal(f.created.length,1);assert.equal(f.created[0].options.focus,false);
});

test('a successful status receipt wins over the original late POST failure and notifies only once',async()=>{
  const post=deferred();let completed=false;const f=fixture({create:()=>post.promise,read:id=>completed?{...ready,request:{request_id:id,state:'completed',response:session}}:ready});
  await f.controller.open();const pending=f.controller.create(values);completed=true;await f.controller.refresh();assert.equal(f.controller.phase,'created');
  post.reject(new Error('transport failed after commit'));assert.equal(await pending,true);assert.equal(f.controller.phase,'created');assert.equal(f.created.length,1);assert.equal(f.controller.uncertain,false);
});

test('older preflight reads cannot overwrite current readiness, creation, or a re-opened dialog',async()=>{
  const first=deferred();let reads=0;const f=fixture({read:()=>reads++===0?first.promise:ready});const old=f.controller.open();f.controller.close();await f.controller.open();await f.controller.create(values);
  first.resolve({...ready,available:false});await old;assert.equal(f.controller.phase,'created');assert.equal(f.controller.home.available,true);
});

test('confirmed creation survives close/reopen plus a late POST timeout without invalidating the reopened GET',async()=>{
  const post=deferred(),reopened=deferred();let reads=0;
  const complete={...ready,request:{request_id:ID,state:'completed',response:session}};
  const f=fixture({create:()=>post.promise,read:()=>++reads===1?ready:reads===2?complete:reopened.promise});
  await f.controller.open();const pending=f.controller.create(values);await f.controller.refresh();assert.equal(f.controller.phase,'created');
  f.controller.close();const reopening=f.controller.open();post.reject(new Error('late transport timeout'));assert.equal(await pending,true);
  reopened.resolve(complete);await reopening;assert.equal(f.controller.phase,'created');assert.equal(f.controller.uncertain,false);assert.equal(f.created.length,1);
});

test('first-open status arriving after close stays closed and cannot notify or select an Agent',async()=>{
  const status=deferred(),f=fixture({read:()=>status.promise});const opening=f.controller.open();f.controller.close();status.resolve(ready);await opening;
  assert.equal(f.controller.phase,'closed');assert.equal(f.controller.home,null);assert.equal(f.created.length,0);assert.equal(f.controller.visible,false);
});

test('failed setup retains retryable intent, validates identity, and maps errors without echoing raw details',async()=>{
  const f=fixture({create:()=>{throw Object.assign(new Error('trace secret /private/auth.json'),{status:409,code:'name_in_use'});}});await f.controller.open();assert.equal(await f.controller.create(values),false);assert.equal(f.controller.phase,'ready');assert.match(f.controller.error,/名稱/);
  assert.equal(await f.controller.create({...values,name:' '}),false);assert.equal(await f.controller.create({...values,workspace:''}),false);assert.equal(f.calls.filter(c=>c[0]==='create').length,1);
  for(const invalid of [{...session,agent_id:'codex:original-app'},{...session,request_id:'unrelated'},{...session,mode:'background_queue'}]){const mismatch=fixture({create:()=>invalid});await mismatch.controller.open();await mismatch.controller.create(values);assert.equal(mismatch.controller.phase,'unknown');assert.equal(mismatch.created.length,0);}
  assert.ok(!codexBackgroundError(new Error('secret /private/auth.json')).includes('secret'));assert.match(codexBackgroundError({code:'codex_login_required'}),/登入 Codex/);
  assert.match(codexBackgroundError({code:'codex_binary_unavailable'}),/安裝/);assert.match(codexBackgroundError({code:'codex_rpc_timeout'}),/逾時/);assert.match(codexBackgroundError({code:'codex_process_unavailable'}),/未能啟動/);
});

test('a failed durable UUID save prevents POST and the only stored value is the request UUID',async()=>{
  const store=storeFixture();store.store.save(ID);assert.deepEqual([...store.values.values()],[ID]);assert.throws(()=>store.store.save('not-a-uuid'));
  const f=fixture({pendingStore:{load(){return null;},save(){throw new Error('storage blocked');},clear(){}}});await f.controller.open();assert.equal(await f.controller.create(values),false);assert.equal(f.calls.length,1);assert.match(f.controller.error,/分頁儲存/);
});

test('background command copy names independent execution and keeps original App and SDK labels intact',()=>{
  const capability={mode:'codex_background',native_ready:true,can_send:true,tool_policy:'read_only'};
  assert.match(receivingHint(capability),/App 可關閉/);assert.ok(!receivingHint(capability).includes('已接通'));assert.match(publicSyncHint(capability),/唯讀/);
  assert.match(receivingHint({...capability,native_ready:false}),/暫時不可用/);assert.match(publicSyncHint({mode:'cursor_local'}),/Cursor SDK/);
  assert.match(receivingHint({mode:'agent_inbox',reason:'cursor_local_background_unavailable'}),/無法自動喚醒原聊天/);
  const composer={capability,status:{dataset:{}}};for(const state of ['pending','sending','queued','running','completed','delivery_unknown']){CommandComposer.prototype.show.call(composer,{state,preview:'Synthetic public task'});assert.ok(!composer.status.textContent.includes('原聊天'));}
  CommandComposer.prototype.show.call(composer,{state:'waiting_user',preview:'Read a file'});assert.match(composer.status.textContent,/等待你批准/);
  CommandComposer.prototype.show.call(composer,{state:'failed',detail_code:'codex_model_unavailable',preview:'Synthetic public task'});assert.match(composer.status.textContent,/目前本機 Codex 執行環境無法使用這個模型/);assert.match(composer.status.textContent,/模型設定/);assert.match(composer.status.textContent,/不會自動切換/);
  for(const code of ['codex_authentication_required','codex_quota_exceeded','codex_rate_limited','codex_context_limit','codex_provider_unavailable','codex_turn_failed'])assert.ok(codexBackgroundFailureHint(code));assert.equal(codexBackgroundFailureHint('raw secret provider error'),'');
});

// Minimal semantic DOM exercises real modal events and form state without a browser.
class Element{
  constructor(tag,doc){this.tagName=tag.toUpperCase();this.doc=doc;this.children=[];this.attrs={};this.handlers=new Map();this.value='';this.hidden=false;this.disabled=false;this.checked=false;this.open=false;}
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
  showModal(){this.open=true;}close(){this.open=false;this.fire('close');}focus(){this.doc.activeElement=this;}reset(){for(const x of this.elements){x.value='';x.checked=false;}}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(x=>x!==this);}
  set innerHTML(html){const stack=[this],voids=new Set(['INPUT','BR']);for(const token of html.match(/<[^>]+>|[^<]+/g)||[]){if(token.startsWith('</')){stack.pop();continue;}if(token[0]!=='<')continue;const tag=token.match(/^<([\w-]+)/)?.[1];if(!tag)continue;const node=new Element(tag,this.doc);for(const m of token.slice(tag.length+1,-1).matchAll(/([\w-]+)(?:="([^"]*)")?/g))node.setAttribute(m[1],m[2]??'');stack.at(-1).append(node);if(!voids.has(node.tagName))stack.push(node);}}
}
function dom(){const doc={createElement:tag=>new Element(tag,doc)};doc.body=new Element('body',doc);return doc;}

test('Codex dialog never opens automatically or asks for a key and preserves drafts through failure and refresh',async t=>{
  const doc=dom(),events=new EventTarget(),calls=[];let home=ready;
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push({url,options});return options?.method==='POST'?{ok:false,status:409,json:async()=>({error:'name_in_use'})}:{ok:true,json:async()=>home};});
  const ui=initCodexBackgroundUI({document:doc,events,key:()=> 'fixture-owner',pendingStore:storeFixture().store});assert.equal(ui.dialog.open,false);assert.equal(calls.length,0);
  events.dispatchEvent(new Event('office:new-codex-conversation'));await wait();const form=ui.dialog.querySelector('form');assert.equal(form.elements.api_key,undefined);assert.equal(form.elements.model.value,'');assert.match(form.elements.model.options[0].textContent,/configured-model/);
  form.elements.name.value=values.name;form.elements.workspace.value='/my/draft';form.elements.create_folder.checked=true;form.elements.model.value='selected-model';await form.fire('submit');await wait();assert.match(ui.controller.error,/名稱/);assert.equal(JSON.parse(calls.find(c=>c.options?.method==='POST').options.body).model,'selected-model');
  home={...ready,workspace:'/different/default',models:[{id:'another-model',name:'New default',is_default:true}]};await ui.controller.refresh();assert.equal(form.elements.workspace.value,'/my/draft');assert.equal(form.elements.name.value,values.name);assert.equal(form.elements.create_folder.checked,true);assert.equal(form.elements.model.value,'selected-model');assert.ok(form.elements.model.options.some(option=>option.value==='selected-model'));
  await ui.dialog.fire('cancel');events.dispatchEvent(new Event('office:new-codex-conversation'));await wait();assert.equal(form.elements.workspace.value,'/my/draft');assert.equal(form.elements.name.value,values.name);assert.equal(form.elements.model.value,'selected-model');
  ui.dispose();const before=calls.length;events.dispatchEvent(new Event('office:new-codex-conversation'));await wait();assert.equal(calls.length,before);
});

test('chooser does not default to a platform, contacts only chosen API, and preserves Cursor setup',async t=>{
  const doc=dom(),events=new EventTarget(),calls=[],codexStore=storeFixture(),cursorStore=storeFixture();
  t.mock.method(globalThis,'fetch',async(url)=>{calls.push(url);return {ok:true,json:async()=>url.includes('/codex/')?ready:{sdk_available:true,credential_configured:false,workspace:ready.workspace,workspaces:[]}};});
  const ui=initNewConversationUI({document:doc,events,key:()=> 'fixture-owner',codexOptions:{pendingStore:codexStore.store},cursorOptions:{pendingStore:cursorSdkRequestStore(()=>cursorStore.storage,'http://fixture/office/')}});
  assert.equal(calls.length,0);events.dispatchEvent(new Event('office:new-conversation'));assert.equal(ui.dialog.open,true);assert.equal(calls.length,0);assert.equal(ui.codex.dialog.open,false);assert.equal(ui.cursor.dialog.open,false);
  const existing=[];events.addEventListener('office:invite-existing-agent',event=>existing.push({chooserOpen:ui.dialog.open,detail:event.detail}));
  await ui.dialog.querySelector('.office-platform-existing').fire('click');await wait();
  assert.deepEqual(existing,[{chooserOpen:false,detail:null}]);assert.equal(ui.codex.dialog.open,false);assert.equal(ui.cursor.dialog.open,false);assert.equal(calls.length,0);
  events.dispatchEvent(new Event('office:new-conversation'));await ui.dialog.fire('cancel');assert.equal(ui.dialog.open,false);assert.equal(calls.length,0);
  events.dispatchEvent(new Event('office:new-conversation'));
  await ui.dialog.querySelector('.office-platform-codex').fire('click');await wait();assert.equal(ui.dialog.open,false);assert.equal(ui.codex.dialog.open,true);assert.deepEqual(calls,['/api/office/codex/background/status']);
  await ui.codex.dialog.fire('cancel');events.dispatchEvent(new Event('office:new-conversation'));await ui.dialog.querySelector('.office-platform-cursor').fire('click');await wait();assert.equal(ui.cursor.dialog.open,true);assert.equal(ui.codex.dialog.open,false);assert.equal(calls.at(-1),'/api/office/cursor/local/status');assert.equal(ui.cursor.dialog.querySelector('form').elements.api_key.type,'password');
  ui.dispose();assert.equal(doc.body.children.length,0);events.dispatchEvent(new Event('office:new-conversation'));assert.equal(calls.length,2);
});

test('the visible invite launcher and floor entry share one chooser and legacy invitations require an explicit choice',()=>{
  const invite=readFileSync(new URL('./public/office/invite.js',import.meta.url),'utf8');
  const app=readFileSync(new URL('./public/office/app.js',import.meta.url),'utf8');
  const floor=readFileSync(new URL('./public/office/ui/ui.js',import.meta.url),'utf8');
  assert.match(invite,/launch\.addEventListener\('click',[^\n]*office:new-conversation/);
  assert.doesNotMatch(invite,/launch\.addEventListener\('click',\s*newInvitation/);
  assert.match(invite,/addEventListener\('office:invite-existing-agent',\s*newInvitation\)/);
  assert.equal((invite.match(/initNewConversationUI\(/g)||[]).length,1);
  assert.doesNotMatch(app,/initNewConversationUI/);
  assert.match(floor,/fresh\.addEventListener\('click',[^\n]*office:new-conversation/);
});

test('Codex POST uses the explicit creation allowlist and rejects neighboring unsafe routes',async t=>{
  const calls=[];t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>session};});
  assert.equal((await officePost('/api/office/codex/background/create','fixture-owner',values)).agent_id,AGENT);
  await assert.rejects(officePost('/api/office/codex/background/unsafe','fixture-owner',{}),/Invalid command endpoint/);assert.equal(calls.length,1);
});

test('current-runtime rejection blocks only an inherited model; explicit choice and refreshed runtime do not silently fallback',async()=>{
  let home={...ready,configured_model_requires_selection:true,configured_model_warning:true,known_unavailable_models:['configured-model']};
  const store=storeFixture(),f=fixture({read:()=>home,pendingStore:store.store});await f.controller.open();
  assert.equal(await f.controller.create(values),false);assert.equal(f.calls.length,1);assert.equal(store.values.size,0);assert.match(f.controller.error,/模型/);
  assert.equal(await f.controller.create({...values,model:'configured-model'}),true);assert.equal(f.calls.at(-1)[1].model,'configured-model');
  const fresh=fixture({read:()=>home});await fresh.controller.open();home={...ready,configured_model_requires_selection:false,configured_model_warning:true,known_unavailable_models:[],runtime_source:'desktop_app'};await fresh.controller.refresh();
  assert.equal(await fresh.controller.create(values),true);assert.equal(Object.hasOwn(fresh.calls.at(-1)[1],'model'),false);assert.equal(fresh.controller.home.runtime_source,'desktop_app');
});

test('Desktop runtime and model warning are visible without erasing drafts or blocking an explicit selection',async t=>{
  const doc=dom(),events=new EventTarget();let home={...ready,runtime_source:'desktop_app',configured_model_requires_selection:true,configured_model_warning:true,known_unavailable_models:['configured-model']};
  t.mock.method(globalThis,'fetch',async()=>({ok:true,json:async()=>home}));
  const ui=initCodexBackgroundUI({document:doc,events,key:()=> 'fixture-owner',pendingStore:storeFixture().store});events.dispatchEvent(new Event('office:new-codex-conversation'));await wait();
  const form=ui.dialog.querySelector('form'),submit=form.querySelector('button[type="submit"]'),refresh=ui.dialog.querySelector('.codex-background-refresh');form.elements.name.value='Keep draft';
  assert.match(ui.dialog.querySelector('.codex-background-status').textContent,/Desktop 內置執行器/);assert.equal(submit.disabled,true);assert.equal(refresh.hidden,false);assert.equal(ui.dialog.querySelector('.codex-model-warning').hidden,false);
  form.elements.model.value='selected-model';await form.elements.model.fire('change');assert.equal(submit.disabled,false);assert.equal(form.elements.name.value,'Keep draft');
  form.elements.model.value='';await form.elements.model.fire('change');home={...ready,runtime_source:'desktop_app'};await refresh.fire('click');assert.equal(submit.disabled,false);assert.equal(form.elements.model.value,'');assert.equal(form.elements.name.value,'Keep draft');ui.dispose();
});
