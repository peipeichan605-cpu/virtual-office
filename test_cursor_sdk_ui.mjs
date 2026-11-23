import test from 'node:test';
import assert from 'node:assert/strict';
import {CursorSdkController,cursorSdkError,cursorSdkSetupMessage,CURSOR_SDK_SETUP_PROMPT,cursorSdkRequestStore,initCursorSdkUI} from './public/office/cursor-sdk-ui.js';
import {receivingHint,publicSyncHint,CommandComposer} from './public/office/commands.js';
import {officeGet,officePost} from './public/office/office-api.js';

const ID='00000000-0000-4000-8000-000000000101',AGENT='cursor:agent-00000000-0000-4000-8000-000000000201';
const ready={sdk_available:true,credential_configured:false,workspace:'/selected/project',workspaces:[{name:'Project',path:'/selected/project'}]};
const session={agent_id:AGENT,name:'SDK fixture',mode:'cursor_local',execution_verified:false};
const values={name:'SDK fixture',workspace:'/selected/project',api_key:'synthetic-private-key'};
const wait=()=>new Promise(resolve=>setImmediate(resolve));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
function storeFixture(){const values=new Map(),storage={getItem:k=>values.get(k)||null,setItem:(k,v)=>values.set(k,v),removeItem:k=>values.delete(k)};return {values,storage,store:cursorSdkRequestStore(()=>storage,'http://localhost:4321/office/')};}
function fixture(overrides={}){
  const calls=[],changes=[],created=[];let number=0;
  const controller=new CursorSdkController({pendingStore:overrides.pendingStore||storeFixture().store,transport:{read:async id=>{calls.push(['read',id]);return overrides.read?overrides.read(id):ready;},create:async body=>{calls.push(['create',{...body}]);return overrides.create?overrides.create(body):session;}},makeId:()=>number++?ID.replace(/101$/,'102'):ID,onChange:c=>changes.push(c.phase),onCreated:(s,options)=>created.push({session:s,options})});
  return {controller,calls,changes,created};
}

test('SDK unavailable or failed preflight never creates an Agent or accepts a key',async()=>{
  const f=fixture({read:()=>({...ready,sdk_available:false,api_key:'must-not-be-saved'})});await f.controller.open();
  assert.equal(f.controller.phase,'unavailable');assert.equal(await f.controller.create(values),false);
  assert.equal(f.calls.filter(c=>c[0]==='create').length,0);assert.ok(!JSON.stringify(f.controller).includes('must-not-be-saved'));
  const failure=fixture({read:()=>{throw Object.assign(new Error('private_office_required'),{status:403,code:'private_office_required'});}});await failure.controller.open();
  assert.equal(failure.controller.phase,'failed');assert.match(failure.controller.error,/私人 OFFICE/);assert.equal(await failure.controller.create(values),false);
});

test('setup diagnostics distinguish missing SDK, old Python, wrong version and Windows ABI',async()=>{
  const reasons=[['cursor_sdk_required',/未安裝 Cursor SDK/],['cursor_python_required',/Python 3.10/],['cursor_sdk_version_unsupported',/目前版本：2.0.0/],['cursor_sdk_platform_unsupported',/x64 Python/]];
  for(const [reason,expected] of reasons){
    const f=fixture({read:()=>({...ready,sdk_available:false,sdk_reason:reason,sdk_version:'2.0.0'})});await f.controller.open();
    assert.equal(f.controller.home.sdk_reason,reason);assert.match(cursorSdkSetupMessage(f.controller.home),expected);
    assert.equal(await f.controller.create(values),false);assert.equal(f.calls.length,1);
  }
  const f=fixture({read:()=>({...ready,sdk_available:false,sdk_reason:'private error /path/key',sdk_version:'secret-value: token'})});await f.controller.open();
  assert.equal(f.controller.home.sdk_reason,null);assert.equal(f.controller.home.sdk_version,null);
  assert.ok(!JSON.stringify(f.controller.home).includes('private error'));assert.ok(!cursorSdkSetupMessage(f.controller.home).includes('token'));
});

test('first setup requires a key but saved per-install account data is reusable without reposting it',async()=>{
  const first=fixture();await first.controller.open();assert.equal(await first.controller.create({...values,api_key:''}),false);assert.equal(first.calls.length,1);
  const f=fixture({read:()=>({...ready,credential_configured:true})});await f.controller.open();assert.equal(await f.controller.create({...values,api_key:''}),true);
  const body=f.calls.find(c=>c[0]==='create')[1];assert.equal(body.request_id,ID);assert.ok(!('api_key' in body));assert.equal(body.workspace,values.workspace);
  assert.equal(f.controller.phase,'created');assert.equal(f.controller.created.scope,'office_owned_session');assert.ok(!('execution_verified' in f.controller.created),'creation must not manufacture execution proof');
});

test('only an explicit folder-creation choice is submitted and no folder action is inferred from its name',async()=>{
  for(const selected of [false,true]){
    const f=fixture();await f.controller.open();await f.controller.create({...values,create_folder:selected});
    const body=f.calls.find(call=>call[0]==='create')[1];assert.equal(body.create_folder,selected?true:undefined);
  }
});

test('automatic requests omit folder fields, while an explicitly blank custom path is rejected',async()=>{
  const f=fixture();await f.controller.open();
  const input={name:values.name,api_key:values.api_key,create_folder:true};
  assert.equal(await f.controller.create({...input,workspace:'  '}),false);
  assert.equal(f.calls.length,1);
  assert.equal(await f.controller.create(input),true);
  assert.deepEqual(f.calls[1][1],{name:values.name,api_key:values.api_key,request_id:ID});
});

test('creation snapshots do not retain credentials or fields returned accidentally by a server',async()=>{
  const f=fixture({create:()=>({...session,api_key:'server-echo-must-be-discarded'})});await f.controller.open();await f.controller.create(values);
  assert.ok(!JSON.stringify(f.controller).includes(values.api_key));assert.ok(!JSON.stringify(f.controller).includes('server-echo'));
  assert.equal(f.created[0].session.agent_id,AGENT);assert.equal(f.created[0].options.focus,true);
});

test('unknown create result blocks repeat creation and GET-only reconciliation reuses its durable identity',async()=>{
  let recovered=false;
  const f=fixture({create:()=>{throw new Error('Synthetic interruption');},read:id=>id?{...ready,creation:{request_id:id,state:recovered?'completed':'pending',...session}}:ready});
  await f.controller.open();await f.controller.create(values);assert.equal(f.controller.phase,'unknown');assert.equal(f.controller.uncertain,true);
  assert.equal(await f.controller.create(values),false);await f.controller.refresh();assert.equal(f.controller.phase,'unknown');
  recovered=true;await f.controller.refresh();assert.equal(f.controller.phase,'created');assert.equal(f.controller.created.agent_id,AGENT);
  assert.equal(f.calls.filter(c=>c[0]==='create').length,1);assert.deepEqual(f.calls.filter(c=>c[0]==='read').map(c=>c[1]),[null,ID,ID]);
  assert.ok(!JSON.stringify(f.controller).includes(values.api_key));
});

test('reloaded controller restores only an opaque UUID and reconciles the same native creation by GET',async()=>{
  const memory=storeFixture(),first=fixture({pendingStore:memory.store,create:()=>{throw new Error('Synthetic response loss');}});
  await first.controller.open();await first.controller.create(values);assert.deepEqual([...memory.values.values()],[ID]);
  assert.ok(!JSON.stringify([...memory.values]).includes(values.api_key));assert.ok(!JSON.stringify([...memory.values]).includes(values.workspace));
  const reloaded=fixture({pendingStore:memory.store,read:id=>({...ready,creation:{request_id:id,state:'completed',...session}})});
  await reloaded.controller.open();assert.deepEqual(reloaded.calls,[['read',ID]]);assert.equal(reloaded.controller.phase,'created');assert.equal(reloaded.controller.created.agent_id,AGENT);assert.equal(memory.values.size,0);
});

test('unknown reload receipt remains locked, and only an explicit absent receipt releases its old identity',async()=>{
  const memory=storeFixture();memory.store.save(ID);
  const f=fixture({pendingStore:memory.store,read:id=>({...ready,creation:{request_id:id,state:'unknown'}})});await f.controller.open();assert.equal(f.controller.phase,'unknown');assert.equal(await f.controller.create(values),false);
  const fresh=fixture({pendingStore:memory.store,read:id=>({...ready,creation:{request_id:id,state:'not_found'}})});await fresh.controller.open();assert.equal(fresh.controller.phase,'ready');assert.equal(fresh.controller.uncertain,false);assert.equal(memory.values.size,0);assert.equal(fresh.calls.filter(c=>c[0]==='create').length,0);
});

test('a revoked creation receipt preserves logout and never focuses or resurrects the old SDK Agent',async()=>{
  const memory=storeFixture();memory.store.save(ID);
  const f=fixture({pendingStore:memory.store,read:id=>({...ready,creation:{request_id:id,state:'revoked',reason:'agent_membership_changed',...session}})});
  await f.controller.open();assert.equal(f.controller.phase,'ready');assert.equal(f.controller.created,null);assert.equal(f.controller.uncertain,false);assert.equal(f.created.length,0);assert.equal(memory.values.size,0);assert.match(f.controller.error,/已登出/);
  assert.equal(f.calls.filter(call=>call[0]==='create').length,0);
});

test('unavailable storage fails before any POST and installation namespaces do not share creation identities',async()=>{
  const denied={load(){return null;},save(){throw new Error('Synthetic storage denial');},clear(){}};
  const f=fixture({pendingStore:denied});await f.controller.open();await f.controller.create(values);assert.equal(f.calls.filter(c=>c[0]==='create').length,0);assert.match(f.controller.error,/保存建立編號/);
  const memory=storeFixture(),other=cursorSdkRequestStore(()=>memory.storage,'http://localhost:9999/office/');memory.store.save(ID);assert.equal(other.load(),null);assert.equal(memory.store.load(),ID);
});

test('a confirmed failed creation can be corrected; unknown identity conflicts cannot start another session',async()=>{
  const f=fixture({create:()=>{throw Object.assign(new Error('cursor_creation_unknown'),{status:409,code:'cursor_creation_unknown'});},read:id=>id?{...ready,creation:{request_id:id,state:'failed',reason:'invalid_workspace'}}:ready});
  await f.controller.open();await f.controller.create(values);await f.controller.refresh();assert.equal(f.controller.phase,'ready');assert.equal(f.controller.requestId,null);assert.equal(f.controller.uncertain,false);
  const conflict=fixture({create:()=>{throw Object.assign(new Error('request_id_conflict'),{status:409,code:'request_id_conflict'});}});
  await conflict.controller.open();await conflict.controller.create(values);assert.equal(conflict.controller.phase,'unknown');assert.equal(await conflict.controller.create(values),false);
});

test('closing a dialog fences old preflight responses, and late creates refresh without stealing a new view',async()=>{
  const response=deferred(),f=fixture({read:()=>response.promise});const opening=f.controller.open();f.controller.close();response.resolve(ready);await opening;
  assert.equal(f.controller.phase,'closed');assert.equal(f.controller.home,null);
  const post=deferred(),other=fixture({create:()=>post.promise});await other.controller.open();const pending=other.controller.create(values);other.controller.close();await other.controller.open();
  assert.equal(other.controller.phase,'creating');post.resolve(session);await pending;
  assert.equal(other.controller.phase,'ready');assert.equal(other.created[0].options.focus,false);assert.equal(other.calls.filter(c=>c[0]==='create').length,1);
});

test('same-view older status reads cannot overwrite newer readiness or an acknowledged creation',async()=>{
  const responses=[deferred(),deferred()];let reads=0;const f=fixture({read:()=>responses[reads++].promise});
  const first=f.controller.open(),second=f.controller.refresh();responses[1].resolve(ready);await second;
  await f.controller.create(values);assert.equal(f.controller.phase,'created');
  responses[0].resolve({...ready,sdk_available:false});await first;assert.equal(f.controller.phase,'created');assert.equal(f.controller.home.sdk_available,true);
});

test('SDK receipts must identify a Cursor session and backend errors cannot echo secrets or exception details',async()=>{
  const f=fixture({create:()=>({agent_id:'generic:wrong-target',api_key:values.api_key})});await f.controller.open();await f.controller.create(values);
  assert.equal(f.controller.phase,'unknown');assert.equal(f.created.length,0);
  assert.ok(!cursorSdkError(new Error('traceback secret-key /private/auth.json')).includes('secret-key'));
  assert.match(cursorSdkError({code:'platform_auth_required'}),/帳戶驗證/);
});

test('SDK display text distinguishes its configured session and preserves original Desktop-only limits',()=>{
  const sdk={mode:'cursor_local',native_ready:true,can_send:true};
  assert.match(receivingHint(sdk),/純對話/);assert.ok(!receivingHint(sdk).includes('已驗證'));
  assert.match(receivingHint({...sdk,native_ready:false}),/暫時不可用/);
  assert.match(publicSyncHint(sdk),/OFFICE 管理/);
  assert.match(receivingHint({mode:'agent_inbox',reason:'cursor_local_background_unavailable'}),/無法自動喚醒原聊天/);
  const composer={capability:sdk,status:{dataset:{}}};
  CommandComposer.prototype.show.call(composer,{state:'running',preview:'Synthetic public task'});assert.match(composer.status.textContent,/Cursor SDK 執行中/);
  CommandComposer.prototype.show.call(composer,{state:'delivery_unknown',preview:'Synthetic public task'});assert.match(composer.status.textContent,/SDK 執行結果未確認/);
});

// A small semantic DOM fixture exercises the actual dialog event wiring.
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
  set innerHTML(html){
    const stack=[this],voids=new Set(['INPUT','BR']);
    for(const token of html.match(/<[^>]+>|[^<]+/g)||[]){
      if(token.startsWith('</')){stack.pop();continue;}
      if(token[0]!=='<')continue;
      const tag=token.match(/^<([\w-]+)/)?.[1];if(!tag)continue;
      const node=new Element(tag,this.doc);
      for(const m of token.slice(tag.length+1,-1).matchAll(/([\w-]+)(?:="([^"]*)")?/g))node.setAttribute(m[1],m[2]??'');
      stack.at(-1).append(node);if(!voids.has(node.tagName))stack.push(node);
    }
  }
}
function dom(){const doc={createElement:tag=>new Element(tag,doc)};doc.body=new Element('body',doc);return doc;}

test('missing SDK exposes one-time setup guidance and copy does not install, post or create an Agent',async t=>{
  const doc=dom(),events=new EventTarget(),calls=[],copied=[];let home={...ready,sdk_available:false,sdk_reason:'cursor_sdk_required'};
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>home};});
  const ui=initCursorSdkUI({document:doc,events,key:()=> 'fixture-owner',pendingStore:storeFixture().store,copyText:async text=>copied.push(text)});
  events.dispatchEvent(new Event('office:new-conversation'));await wait();
  assert.equal(ui.dialog.querySelector('.cursor-sdk-setup').hidden,false);assert.match(ui.dialog.querySelector('.cursor-sdk-status').textContent,/未安裝/);
  await ui.dialog.querySelector('.cursor-sdk-copy-setup').fire('click');
  assert.deepEqual(copied,[CURSOR_SDK_SETUP_PROMPT]);assert.match(ui.dialog.querySelector('.cursor-sdk-setup-notice').textContent,/已複製/);
  assert.equal(calls.length,1);assert.ok(!calls[0].options?.method);assert.ok(!copied[0].includes('fixture-owner'));
  home=ready;await ui.dialog.querySelector('.cursor-sdk-refresh').fire('click');
  assert.equal(ui.dialog.querySelector('.cursor-sdk-setup').hidden,true);assert.equal(ui.dialog.querySelector('form').hidden,false);
  assert.equal(calls.length,2);assert.ok(calls.every(c=>c.options?.method!=='POST'));ui.dispose();
});

test('clipboard denial leaves a manually copyable setup prompt without claiming success',async t=>{
  const doc=dom(),events=new EventTarget();
  t.mock.method(globalThis,'fetch',async()=>({ok:true,json:async()=>({...ready,sdk_available:false,sdk_reason:'cursor_python_required'})}));
  const ui=initCursorSdkUI({document:doc,events,key:()=> 'fixture-owner',pendingStore:storeFixture().store,copyText:async()=>{throw new Error('denied');}});
  events.dispatchEvent(new Event('office:new-conversation'));await wait();await ui.dialog.querySelector('.cursor-sdk-copy-setup').fire('click');
  assert.equal(ui.dialog.querySelector('.cursor-sdk-setup-prompt').value,CURSOR_SDK_SETUP_PROMPT);
  assert.match(ui.dialog.querySelector('.cursor-sdk-setup-notice').textContent,/手動複製/);
  assert.ok(!ui.dialog.querySelector('.cursor-sdk-setup-notice').textContent.includes('已複製'));ui.dispose();
});

test('new dialog opens only on an explicit action and masks/clears its key while preserving failure intent',async t=>{
  const doc=dom(),events=new EventTarget(),calls=[],post=deferred();
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    calls.push({url,options});return options?.method==='POST'?post.promise:{ok:true,json:async()=>ready};
  });
  const ui=initCursorSdkUI({document:doc,events,key:()=> 'fixture-owner',pendingStore:storeFixture().store});assert.equal(ui.dialog.open,false);assert.equal(calls.length,0);
  events.dispatchEvent(new Event('office:new-conversation'));await wait();assert.equal(ui.dialog.open,true);assert.equal(ui.controller.phase,'ready');
  const form=ui.dialog.querySelector('form');assert.equal(form.elements.api_key.type,'password');form.elements.name.value=values.name;form.elements.api_key.value=values.api_key;
  assert.equal(form.elements.known.value,'');assert.equal(form.elements.workspace.disabled,true);assert.equal(form.elements.workspace.required,false);
  await form.fire('submit');assert.equal(form.elements.api_key.value,'');assert.equal(calls.length,2);
  const body=JSON.parse(calls[1].options.body);
  assert.equal(body.api_key,values.api_key);assert.ok(!('workspace' in body));assert.ok(!('create_folder' in body));
  post.resolve({ok:false,status:401,json:async()=>({error:'platform_auth_required'})});await wait();assert.equal(ui.controller.phase,'ready');assert.match(ui.controller.error,/帳戶驗證/);
  form.elements.api_key.value='another-synthetic-key';await ui.dialog.fire('cancel');assert.equal(form.elements.api_key.value,'');assert.equal(ui.dialog.open,false);
  ui.dispose();const before=calls.length;events.dispatchEvent(new Event('office:new-conversation'));await wait();assert.equal(calls.length,before);assert.equal(ui.dialog.open,false);
});

test('folder picker submits only the active choice and always reopens in automatic mode',async t=>{
  const doc=dom(),events=new EventTarget(),bodies=[];
  const home={...ready,credential_configured:true,workspace:'/previous/folder'};
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    if(options?.method!=='POST')return {ok:true,json:async()=>home};
    bodies.push(JSON.parse(options.body));
    return {ok:false,status:400,json:async()=>({error:'name_in_use'})};
  });
  const ui=initCursorSdkUI({document:doc,events,key:()=> 'fixture-owner',pendingStore:storeFixture().store});
  const open=async()=>{events.dispatchEvent(new Event('office:new-conversation'));await wait();};
  await open();
  const form=ui.dialog.querySelector('form'),{known,workspace,create_folder,api_key}=form.elements;
  const select=async value=>{known.value=value;await known.fire('change');};
  const submit=async()=>{await form.fire('submit');await wait();return bodies.at(-1);};
  form.elements.name.value=values.name;
  assert.deepEqual(known.options.map(o=>o.value),['',values.workspace,home.workspace,'custom']);
  await select('custom');
  assert.equal(form.querySelector('.cursor-sdk-path').hidden,false);
  assert.equal(form.querySelector('.cursor-sdk-create-folder').hidden,false);
  assert.equal(workspace.required,true);
  workspace.value='C:\\Users\\Example\\My Work';create_folder.checked=true;
  let body=await submit();
  assert.equal(body.workspace,workspace.value);assert.equal(body.create_folder,true);
  await select(values.workspace);
  assert.equal(form.querySelector('.cursor-sdk-path').hidden,true);
  assert.equal(form.querySelector('.cursor-sdk-create-folder').hidden,true);
  assert.equal(workspace.disabled,true);assert.equal(create_folder.disabled,true);
  body=await submit();assert.equal(body.workspace,values.workspace);assert.ok(!('create_folder' in body));
  await select('');api_key.value='hidden-autofill';
  body=await submit();assert.deepEqual(Object.keys(body).sort(),['name','request_id']);
  await select(home.workspace);await ui.dialog.fire('cancel');await open();
  assert.equal(known.value,'');assert.equal(workspace.required,false);
  ui.dispose();
});

test('only the SDK creation route is added to the POST allowlist and HTTP auth metadata is preserved',async t=>{
  const calls=[];t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push(url);return url.endsWith('/status')?{ok:false,status:403}:{ok:true,json:async()=>session};});
  assert.equal((await officePost('/api/office/cursor/local/create','fixture-owner',{name:'fixture'})).agent_id,AGENT);
  await assert.rejects(officePost('/api/office/cursor/local/unsafe','fixture-owner',{}),/Invalid command endpoint/);assert.equal(calls.length,1);
  await assert.rejects(officeGet('/api/office/cursor/local/status','fixture-owner'),error=>error.status===403&&error.code==='private_office_required');
});

test('unavailable SDK hides setup form and cannot post an autofilled key; saved account setup hides its key field',async t=>{
  const doc=dom(),events=new EventTarget(),calls=[];let home={...ready,sdk_available:false};
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>home};});
  const ui=initCursorSdkUI({document:doc,events,key:()=> 'fixture-owner',pendingStore:storeFixture().store});events.dispatchEvent(new Event('office:new-conversation'));await wait();
  const form=ui.dialog.querySelector('form');assert.equal(form.hidden,true);form.elements.api_key.value='synthetic-autofill';await form.fire('submit');assert.equal(calls.length,1);assert.equal(form.elements.api_key.value,'');
  ui.controller.close();ui.dialog.close();home={...ready,credential_configured:true};events.dispatchEvent(new Event('office:new-conversation'));await wait();
  assert.equal(ui.dialog.querySelector('.cursor-sdk-key').hidden,true);assert.equal(ui.dialog.querySelector('.cursor-sdk-saved').hidden,false);assert.equal(form.elements.api_key.required,false);
  await ui.dialog.querySelector('.cursor-sdk-change-key').fire('click');assert.equal(ui.dialog.querySelector('.cursor-sdk-key').hidden,false);ui.dispose();
});
