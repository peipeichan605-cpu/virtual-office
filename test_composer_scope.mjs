import test from 'node:test';
import assert from 'node:assert/strict';
import {CommandComposer,publicSyncHint} from './public/office/commands.js';

const A='fixture:old-membership',B='fixture:new-membership';
const cap=(extra={})=>({mode:'background_queue',connected:true,same_session:true,can_send:true,persistent:true,native_ready:true,...extra});
const command=(agent,id,state='completed')=>({agent_id:agent,id,state,preview:'Synthetic public task',created_at:new Date().toISOString()});
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const turn=()=>new Promise(resolve=>setImmediate(resolve));
async function until(predicate){for(let i=0;i<60&&!predicate();i++)await turn();assert.equal(predicate(),true,'Expected fixture request to start');}
const environments=new WeakMap();

class Element{
  constructor(){this.children=[];this.handlers=new Map();this.dataset={};this.value='';this.textContent='';this.hidden=false;this.disabled=false;}
  append(...items){for(const item of items){this.children.push(item);item.parent=this;}}
  replaceChildren(...items){this.children=[];this.append(...items);}
  setAttribute(name,value){this[name]=value;}
  addEventListener(type,callback){this.handlers.set(type,callback);}
  async click(){return this.handlers.get('click')?.({});}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(item=>item!==this);}
}
function fixture(t){
  if(!environments.has(t)){
    const saved={document:globalThis.document,location:globalThis.location,sessionStorage:globalThis.sessionStorage};environments.set(t,saved);
    t.after(()=>{for(const [key,value] of Object.entries(saved))if(value===undefined)delete globalThis[key];else globalThis[key]=value;});
  }
  globalThis.document={createElement:()=>new Element()};globalThis.location={hash:'#fixture-owner'};
  const storage=new Map();globalThis.sessionStorage={getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)};
  const label={firstChild:{textContent:''}},hint=new Element(),form=new Element();form.querySelector=selector=>selector==='label'?label:hint;
  const c=Object.assign(Object.create(CommandComposer.prototype),{epoch:0,element:new Element(),form,input:new Element(),button:new Element(),status:new Element(),syncStatus:new Element(),approvalBox:new Element(),connect:new Element(),busy:false});
  c.bind(A,cap());return c;
}

test('same-name re-enrollment resets all old Agent state while same identity preserves its draft',t=>{
  const c=fixture(t);c.input.value='Draft for selected Agent';c.latest=command(A,'old');c.attempt={id:'old-attempt',text:c.input.value};c.uncertain=true;
  c.renderApprovals([{id:'old-approval',preview:'Synthetic approval',can_allow:true}]);c.renderPending([command(A,'old-pending','pending')]);
  c.bind(A,cap());assert.equal(c.input.value,'Draft for selected Agent');assert.equal(c.latest.id,'old');assert.equal(c.attempt.id,'old-attempt');
  c.bind(B,cap());assert.equal(c.input.value,'');assert.equal(c.latest,null);assert.equal(c.attempt,null);assert.equal(c.uncertain,false);
  assert.equal(c.approvalBox.children.length,0);assert.equal(c.pendingBox.children.length,0);assert.equal(c.pendingBox.hidden,true);assert.ok(!c.status.textContent.includes('old'));
});

test('a late send success cannot clear a new Agent draft, finish its busy state or replace its receipt',async t=>{
  const c=fixture(t),calls=[],responses=[deferred(),deferred()];
  t.mock.method(globalThis,'fetch',(url,options)=>{calls.push({url,body:JSON.parse(options.body)});return responses[calls.length-1].promise;});
  c.input.value='Task for A';const first=c.send();await until(()=>calls.length===1);
  c.bind(B,cap());c.input.value='Task for B';const second=c.send();await until(()=>calls.length===2);
  responses[0].resolve({ok:true,json:async()=>({command:command(A,calls[0].body.request_id)})});await first;
  assert.equal(c.busy,true);assert.equal(c.input.value,'Task for B');assert.equal(c.attempt.id,calls[1].body.request_id);assert.equal(c.latest,null);
  responses[1].resolve({ok:true,json:async()=>({command:command(B,calls[1].body.request_id)})});await second;
  assert.equal(c.busy,false);assert.equal(c.input.value,'');assert.equal(c.latest.agent_id,B);assert.deepEqual(calls.map(x=>x.body.agent_id),[A,B]);
});

test('a late send error is discarded after selecting a new identity',async t=>{
  const c=fixture(t),pending=deferred();let posted=false;
  t.mock.method(globalThis,'fetch',()=>{posted=true;return pending.promise;});
  c.input.value='Task for A';const first=c.send();await until(()=>posted);
  c.bind(B,cap());c.input.value='B draft';const status=c.status.textContent;
  pending.reject(new Error('Synthetic network interruption'));await first;
  assert.equal(c.status.textContent,status);assert.equal(c.input.value,'B draft');assert.equal(c.uncertain,false);assert.equal(c.busy,false);
});

test('an old same-Agent GET cannot replace a newly acknowledged pending command with an old completion',async t=>{
  const c=fixture(t),oldRead=deferred(),post=deferred();let request;
  t.mock.method(globalThis,'fetch',(url,options)=>{if(options.method==='POST'){request=JSON.parse(options.body);return post.promise;}return oldRead.promise;});
  c.latest=command(A,'old-completed');const reading=c.refresh();c.input.value='New task';const sending=c.send();await until(()=>!!request);
  post.resolve({ok:true,json:async()=>({command:command(A,request.request_id,'pending')})});await sending;
  assert.equal(c.latest.id,request.request_id);assert.equal(c.latest.state,'pending');
  oldRead.resolve({ok:true,json:async()=>({capability:cap(),commands:[command(A,'old-completed')],approvals:[]})});await reading;
  assert.equal(c.latest.id,request.request_id);assert.equal(c.latest.state,'pending');assert.ok(!c.status.textContent.includes('已完成'));
});

test('an old same-Agent GET cannot restore a pending command after successful cancellation',async t=>{
  const c=fixture(t),oldRead=deferred(),post=deferred();let reads=0;
  t.mock.method(globalThis,'fetch',(url,options)=>{
    if(options.method==='POST')return post.promise;
    if(++reads===1)return oldRead.promise;
    return Promise.resolve({ok:true,json:async()=>({capability:cap(),commands:[command(A,'to-cancel','cancelled')],approvals:[]})});
  });
  c.renderPending([command(A,'to-cancel','pending')]);const reading=c.refresh(),cancel=c.pendingBox.children[1].children[1].click();
  post.resolve({ok:true,json:async()=>({command:command(A,'to-cancel','cancelled')})});await cancel;
  oldRead.resolve({ok:true,json:async()=>({capability:cap(),commands:[command(A,'to-cancel','pending')],approvals:[]})});await reading;
  assert.equal(c.latest.state,'cancelled');assert.equal(c.pendingBox.hidden,true);
});

test('an old same-Agent GET cannot restore an approval that was just answered',async t=>{
  const c=fixture(t),oldRead=deferred(),post=deferred(),approval={id:'approval-a',preview:'Synthetic approval',can_allow:true};
  t.mock.method(globalThis,'fetch',(url,options)=>options.method==='POST'?post.promise:oldRead.promise);
  c.renderApprovals([approval]);const reading=c.refresh(),answer=c.approvalBox.children[0].children[2].children[1].click();
  post.resolve({ok:true,json:async()=>({accepted:true})});await answer;assert.equal(c.approvalBox.children.length,0);
  oldRead.resolve({ok:true,json:async()=>({capability:cap(),commands:[command(A,'old')],approvals:[approval]})});await reading;
  assert.equal(c.approvalBox.children.length,0);
});

test('late command-list success and error cannot restore a prior Agent after rebinding',async t=>{
  for(const fail of [false,true]){
    const c=fixture(t),pending=deferred();t.mock.method(globalThis,'fetch',()=>pending.promise);
    c.latest=command(A,'old');const active=c.refresh();c.bind(B,cap());c.input.value='B draft';const status=c.status.textContent;
    if(fail)pending.reject(new Error('Synthetic network interruption'));
    else pending.resolve({ok:true,json:async()=>({capability:cap(),commands:[command(A,'old')],approvals:[]})});
    await active;assert.equal(c.agent,B);assert.equal(c.latest,null);assert.equal(c.status.textContent,status);assert.equal(c.input.value,'B draft');assert.equal(c.refreshing,false);
  }
});

test('late cancellation success and error never mutate a newly selected Agent',async t=>{
  for(const fail of [false,true]){
    const c=fixture(t),pending=deferred(),calls=[];t.mock.method(globalThis,'fetch',(url,options)=>{calls.push(JSON.parse(options.body));return pending.promise;});
    c.renderPending([command(A,'old-pending','pending')]);const button=c.pendingBox.children[1].children[1];
    const active=button.click();c.bind(B,cap());c.input.value='B draft';const status=c.status.textContent;
    if(fail)pending.reject(new Error('Synthetic interruption'));else pending.resolve({ok:true,json:async()=>({command:command(A,'old-pending','cancelled')})});
    await active;assert.equal(c.agent,B);assert.equal(c.latest,null);assert.equal(c.status.textContent,status);assert.equal(c.input.value,'B draft');assert.equal(calls[0].agent_id,A);
    await button.click();assert.equal(calls.length,1,'A detached old button must not POST for B');
  }
});

test('late approval success and error use the captured Agent and leave the new view untouched',async t=>{
  for(const fail of [false,true]){
    const c=fixture(t),pending=deferred(),calls=[];t.mock.method(globalThis,'fetch',(url,options)=>{calls.push(JSON.parse(options.body));return pending.promise;});
    c.renderApprovals([{id:'old-approval',preview:'Synthetic approval',can_allow:true}]);
    const card=c.approvalBox.children[0],preview=card.children[1],button=card.children[2].children[1];
    const active=button.click();c.bind(B,cap());c.renderApprovals([{id:'new-approval',preview:'B approval',can_allow:true}]);const status=c.status.textContent,signature=c.approvalSignature;
    if(fail)pending.reject(new Error('Synthetic interruption'));else pending.resolve({ok:true,json:async()=>({accepted:true})});
    await active;assert.equal(preview.textContent,'Synthetic approval');assert.equal(c.status.textContent,status);assert.equal(c.approvalSignature,signature);assert.equal(calls[0].agent_id,A);
    await button.click();assert.equal(calls.length,1);
  }
});

test('a failed approval restores controls only from a fresh GET and never automatically repeats the POST',async t=>{
  for(const stillPending of [true,false]){
    const c=fixture(t),approval={id:'same-approval',preview:'Synthetic approval',can_allow:true};let posts=0,gets=0;
    t.mock.method(globalThis,'fetch',async(url,options)=>{
      if(options.method==='POST'){posts++;throw new Error('Synthetic lost response');}
      gets++;return {ok:true,json:async()=>({capability:cap(),commands:[],approvals:stillPending?[approval]:[]})};
    });
    c.renderApprovals([approval]);const oldButton=c.approvalBox.children[0].children[2].children[1];
    await oldButton.click();assert.equal(posts,1);assert.equal(gets,0);assert.equal(oldButton.disabled,true);assert.equal(c.approvalSignature,null);
    await c.refresh();assert.equal(posts,1,'Reading the authoritative pending list must not resend an approval');assert.equal(gets,1);
    if(stillPending){const newButton=c.approvalBox.children[0].children[2].children[1];assert.notEqual(newButton,oldButton);assert.equal(newButton.disabled,false);}
    else assert.equal(c.approvalBox.children.length,0);
  }
});

test('same-Agent route replacement or dispatch disable invalidates in-flight command refresh',async t=>{
  for(const next of [cap({mode:'grok_webhook',connection_check:{route_revision:'route-b'}}),cap({can_send:false,reason:'dispatch_disabled'})]){
    const c=fixture(t),pending=deferred();c.bind(A,cap({mode:'grok_webhook',connection_check:{route_revision:'route-a'}}));
    t.mock.method(globalThis,'fetch',()=>pending.promise);const active=c.refresh();c.bind(A,next);
    pending.resolve({ok:true,json:async()=>({capability:cap({mode:'grok_webhook',connection_check:{route_revision:'route-a'}}),commands:[command(A,'old')],approvals:[]})});
    await active;assert.deepEqual(c.capability,next);assert.equal(c.latest,null);
  }
});

test('retired or logged-out composer ignores pending replies and clears its draft',async t=>{
  const c=fixture(t),pending=deferred();let posted=false;t.mock.method(globalThis,'fetch',()=>{posted=true;return pending.promise;});
  c.input.value='Selected Agent task';const active=c.send();await until(()=>posted);c.dispose();
  pending.resolve({ok:true,json:async()=>({command:command(A,'old')})});await active;
  assert.equal(c.agent,null);assert.equal(c.input.value,'');assert.equal(c.latest,null);assert.equal(c.form.hidden,true);assert.equal(c.syncStatus.textContent,'');
});

test('public synchronization describes coverage without turning dispatch proof into native App observation',()=>{
  const proof={connection_check:{verified:true,received:true,replied:true}};
  assert.equal(publicSyncHint(proof),'App 公開同步：能力待確認');
  const described=public_sync=>publicSyncHint({...proof,public_sync});
  assert.equal(described({mode:'cooperative',state:'cooperative',scope:'reported_runs'}),'App 直接工作：需 Agent 合作上報；沒有原生自動同步');
  assert.equal(described({mode:'native_run_observer',scope:'office_dispatched_runs',configured:true}),'公開對話：只同步 OFFICE 派發的工作');
  assert.equal(described({mode:'native_hook',scope:'selected_conversation',state:'configured',configured:true,manual_app_automatic:true}),'原聊天公開同步已設定');
  assert.equal(described({mode:'native_hook',scope:'selected_conversation',state:'setup_required',configured:false,manual_app_automatic:true}),'App 公開同步：尚待完成設定');
  assert.equal(described({mode:'unsupported',state:'unsupported',scope:'none'}),'App 直接工作：尚未有可用的公開同步入口');
  assert.ok(!described({mode:'native_observer',scope:'selected_conversation',configured:true,manual_app_automatic:true}).includes('已驗證'));
});

test('work reported from the App remains visibly pending and cancellable before dispatch',t=>{
  const c=fixture(t),pending={...command(A,'held','pending'),detail_code:'waiting_for_reported_work'};
  c.show(pending);c.renderPending([pending]);
  assert.equal(c.status.textContent,'等待這位 Agent 在 App 的工作結束；指示尚未送出');
  assert.equal(c.pendingBox.hidden,false);assert.equal(c.pendingBox.children[1].children[1].textContent,'取消');
});
