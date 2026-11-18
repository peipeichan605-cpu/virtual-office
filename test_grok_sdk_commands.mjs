import test from 'node:test';
import assert from 'node:assert/strict';
import {CommandComposer,receivingHint,receivingSetupControl,publicSyncHint,queueHint} from './public/office/commands.js';

const agent='grok_bot:sdk-selected-session';
const capability=(extra={})=>({mode:'grok_sdk',connected:true,same_session:true,persistent:true,can_send:true,native_ready:true,execution_verified:false,...extra});

test('Grok SDK capabilities never fall through to the Webhook connection assistant',()=>{
  const cap=capability();
  assert.match(receivingHint(cap,false,agent),/Grok Bot SDK/);
  assert.doesNotMatch(receivingHint(cap,false,agent),/已連接|已驗證|Webhook/);
  assert.deepEqual(receivingSetupControl(cap,agent),{hidden:true,label:'查看 Grok SDK 設定'});
  assert.deepEqual(receivingSetupControl(capability({native_ready:false,reason:'grok_sdk_login_required'}),agent),{hidden:false,label:'重新登入 Grok'});
  assert.match(receivingHint(capability({native_ready:false,reason:'grok_sdk_login_expired'}),false,agent),/重新登入/);
  assert.match(receivingHint(capability({can_send:false,reason:'dispatch_disabled'}),false,agent),/已停用/);
  const old={mode:'grok_webhook',persistent:true,native_ready:true,can_send:true};
  assert.equal(receivingHint(old,false,agent),'入口已設定 · 尚未測試');
  assert.deepEqual(receivingSetupControl(old,agent),{hidden:false,label:'測試連接'});
});

test('SDK public-sync copy distinguishes configured observation, recovery, gaps and login expiry',()=>{
  const sync={mode:'grok_sdk_observer',scope:'selected_conversation',state:'configured',configured:true,automatic:true,manual_app_automatic:true};
  assert.match(publicSyncHint(capability({public_sync:sync})),/同一 Grok Bot 聊天/);
  assert.match(publicSyncHint(capability({public_sync:sync})),/已設定/);
  assert.doesNotMatch(publicSyncHint(capability({public_sync:sync})),/實測成功|已完成|Webhook|合作上報/);
  assert.match(publicSyncHint(capability()),/準備中/);
  assert.match(publicSyncHint(capability({public_sync:{...sync,state:'recovering'}})),/重新連接/);
  assert.match(publicSyncHint(capability({public_sync:{...sync,history_gap:true}})),/尚未完整/);
  assert.match(publicSyncHint(capability({public_sync:{...sync,has_gaps:true}})),/尚未完整/);
  assert.match(publicSyncHint(capability({public_sync:{...sync,state:'unavailable'}})),/暫時不可用/);
  assert.match(publicSyncHint(capability({public_sync:sync,reason:'grok_sdk_authentication_required'})),/重新登入/);
});

class Element{
  constructor(){this.children=[];this.handlers=new Map();this.dataset={};this.value='';this.textContent='';this.hidden=false;this.disabled=false;}
  append(...items){for(const item of items){this.children.push(item);item.parent=this;}}
  replaceChildren(...items){this.children=[];this.append(...items);}
  setAttribute(name,value){this[name]=value;}
  addEventListener(type,callback){this.handlers.set(type,callback);}
  click(){return this.handlers.get('click')?.({});}
}
function fixture(t){
  const previous={document:globalThis.document,window:globalThis.window};
  t.after(()=>{for(const [key,value] of Object.entries(previous))if(value===undefined)delete globalThis[key];else globalThis[key]=value;});
  const events=[];globalThis.document={createElement:()=>new Element()};globalThis.window={dispatchEvent:event=>events.push(event)};
  const label={firstChild:{textContent:''}},hint=new Element(),form=new Element();form.querySelector=selector=>selector==='label'?label:hint;
  const composer=Object.assign(Object.create(CommandComposer.prototype),{epoch:0,element:new Element(),form,input:new Element(),button:new Element(),status:new Element(),syncStatus:new Element(),approvalBox:new Element(),busy:false});
  return {composer,label,events};
}

test('SDK composer labels the selected native chat and routes reconnect only to the SDK dialog',t=>{
  const {composer:c,label,events}=fixture(t);
  c.bind(agent,capability({native_ready:false,reason:'grok_sdk_login_required'}));
  assert.match(label.firstChild.textContent,/Grok App 的同一 Bot／聊天/);
  assert.doesNotMatch(c.status.textContent,/Webhook|連接助手|複製/);
  assert.equal(c.connect.hidden,false);c.connect.click();
  assert.equal(events.length,1);assert.equal(events[0].type,'office:new-grok-sdk-conversation');assert.equal(events[0].detail.agent_id,agent);
  c.bind(agent,{mode:'grok_webhook',persistent:true,native_ready:true,can_send:true});c.connect.click();
  assert.equal(events[1].type,'office-inbox-setup','The same control follows its current adapter after rebinding');
});

test('SDK status receipts do not claim completion and never ask to open the App for a delayed queue',()=>{
  const c={capability:capability(),status:{dataset:{}}};
  const statuses=new Map();
  for(const state of ['pending','sending','queued','running','completed','delivery_unknown']){
    CommandComposer.prototype.show.call(c,{state,created_at:new Date(Date.now()-120000).toISOString(),preview:'Synthetic public task'});
    statuses.set(state,c.status.textContent);assert.doesNotMatch(c.status.textContent,/Webhook|Bot 已回報/);
  }
  assert.match(statuses.get('queued'),/入口已接收/);assert.match(statuses.get('queued'),/等待原聊天開始/);
  assert.doesNotMatch(statuses.get('queued'),/已交付|已完成|請開啟/);
  assert.match(statuses.get('completed'),/已交付回覆/);assert.match(statuses.get('delivery_unknown'),/未確認/);
  assert.equal(queueHint({state:'queued',created_at:new Date(0).toISOString()},Date.now(),'grok_sdk'),'');
});

test('an unavailable SDK keeps the send controls disabled and prior history remains separate',t=>{
  const {composer:c}=fixture(t);
  c.bind(agent,capability({can_send:false,native_ready:false}));
  assert.equal(c.form.hidden,true);assert.equal(c.button.disabled,true);assert.match(c.status.textContent,/既有 Bot、聊天及紀錄保留/);
  const saved={state:'completed',preview:'Previously delivered result'};c.show(saved);
  c.bind(agent,capability({can_send:false,native_ready:false,reason:'grok_sdk_login_required'}));
  assert.equal(c.latest,saved);assert.match(c.status.textContent,/Previously delivered result/);assert.match(c.syncStatus.textContent,/重新登入/);
});
