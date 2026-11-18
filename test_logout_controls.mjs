import test from 'node:test';
import assert from 'node:assert/strict';
import {logoutAgent} from './public/office/office-controls.js';

const A='cursor:fixture-a',B='cursor:fixture-b';
const TOKEN='a'.repeat(64),NEW_TOKEN='b'.repeat(64);
const unknown=(token=TOKEN)=>({error:'agent_has_unconfirmed_commands',logout_token:token,unconfirmed_commands:1});
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
class Element{
  constructor(tag='div'){this.tag=tag;this.children=[];this.handlers=new Map();this.textContent='';this.disabled=false;this.open=false;}
  append(...items){for(const item of items){item.parent=this;this.children.push(item);}}
  setAttribute(name,value){this[name]=value;}
  addEventListener(type,handler){const list=this.handlers.get(type)||[];list.push(handler);this.handlers.set(type,list);}
  emit(type,event={}){return Promise.all((this.handlers.get(type)||[]).map(handler=>handler(event)));}
  showModal(){this.open=true;}
  close(){this.open=false;return this.emit('close');}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(item=>item!==this);}
  focus(){this.focused=true;}
}
function fixture(t){
  const previous={document:globalThis.document,window:globalThis.window,location:globalThis.location,CustomEvent:globalThis.CustomEvent};
  const body=new Element('body'),events=[],calls=[];
  globalThis.document={body,createElement:tag=>new Element(tag)};
  globalThis.window={dispatchEvent:event=>events.push({type:event.type,detail:event.detail})};
  globalThis.location={hash:'#fixture-owner'};
  globalThis.CustomEvent=class{constructor(type,options={}){this.type=type;this.detail=options.detail;}};
  t.after(()=>{for(const [name,value] of Object.entries(previous))if(value===undefined)delete globalThis[name];else globalThis[name]=value;});
  t.mock.method(globalThis,'fetch',(url,options)=>{const request=deferred();calls.push({url,options,body:JSON.parse(options.body),...request});return request.promise;});
  const open=(bot={agent_id:A,name:'Fixture'})=>{logoutAgent(bot);const dialog=body.children.at(-1);return {dialog,description:dialog.children[1],notice:dialog.children[2],cancel:dialog.children[3].children[0],submit:dialog.children[3].children[1]};};
  const respond=(index,data,status=200)=>calls[index].resolve({ok:status>=200&&status<300,status,json:async()=>data});
  return {body,calls,events,open,respond};
}

test('unknown work requires a second explicit acknowledgement; it never retries or changes target',async t=>{
  const f=fixture(t),bot={agent_id:A,name:'Fixture'},ui=f.open(bot);
  const first=ui.submit.emit('click');assert.equal(f.calls.length,1);assert.deepEqual(f.calls[0].body,{agent_id:A});
  f.respond(0,unknown(),409);await first;
  assert.equal(f.calls.length,1);assert.equal(ui.dialog.open,true);assert.equal(ui.submit.disabled,false);assert.equal(ui.cancel.disabled,false);
  assert.match(ui.description.textContent,/未知結果與交付紀錄會保留/);assert.match(ui.description.textContent,/不會自動重送/);assert.match(ui.description.textContent,/不代表原平台的工作已停止/);assert.equal(ui.submit.textContent,'仍要登出，保留紀錄');
  bot.agent_id=B;const second=ui.submit.emit('click');assert.deepEqual(f.calls[1].body,{agent_id:A,acknowledge_unconfirmed:true,logout_token:TOKEN});
  f.respond(1,{logged_out:true,agent_id:A,history_preserved:true,native_session_stopped:false});await second;
  assert.equal(ui.dialog.open,false);assert.deepEqual(f.events,[{type:'office:refresh',detail:undefined},{type:'office:agent-logged-out',detail:{agent_id:A}}]);
});

test('cancelling after an uncertainty warning does not acknowledge or submit another logout',async t=>{
  const f=fixture(t),ui=f.open(),first=ui.submit.emit('click');f.respond(0,unknown(),409);await first;
  await ui.cancel.emit('click');await ui.submit.emit('click');assert.equal(f.calls.length,1);assert.equal(f.events.length,0);assert.equal(f.body.children.length,0);
});

test('concurrent clicks are fenced and close remains possible while the first request is pending',async t=>{
  const f=fixture(t),ui=f.open(),first=ui.submit.emit('click');await ui.submit.emit('click');assert.equal(f.calls.length,1);assert.equal(ui.cancel.disabled,false);
  await ui.cancel.emit('click');const notice=ui.notice.textContent;f.respond(0,unknown(),409);await first;
  assert.equal(ui.notice.textContent,notice);assert.equal(ui.dialog.open,false);assert.equal(f.body.children.length,0);assert.equal(f.calls.length,1);
});

test('known active work remains blocked and cannot inherit an earlier uncertainty acknowledgement',async t=>{
  const f=fixture(t),ui=f.open(),first=ui.submit.emit('click');f.respond(0,unknown(),409);await first;
  const acknowledged=ui.submit.emit('click');f.respond(1,{error:'agent_has_active_commands'},409);await acknowledged;
  assert.match(ui.notice.textContent,/工作仲未結束/);assert.equal(ui.submit.textContent,'確認登出');assert.equal(ui.dialog.open,true);
  const retry=ui.submit.emit('click');assert.deepEqual(f.calls[2].body,{agent_id:A});f.respond(2,{error:'agent_has_active_commands'},409);await retry;assert.equal(f.events.length,0);
});

test('a late successful receipt refreshes the original identity without closing another dialog',async t=>{
  const f=fixture(t),old=f.open(),request=old.submit.emit('click');await old.cancel.emit('click');const current=f.open({agent_id:B,name:'Current'});
  f.respond(0,{logged_out:true,agent_id:A});await request;
  assert.equal(current.dialog.open,true);assert.equal(f.body.children.length,1);assert.equal(f.events.at(-1).detail.agent_id,A);
});

test('a mismatched logout receipt is never presented as success',async t=>{
  const f=fixture(t),ui=f.open(),request=ui.submit.emit('click');f.respond(0,{logged_out:true,agent_id:B});await request;
  assert.equal(ui.dialog.open,true);assert.equal(f.events.length,0);assert.equal(ui.submit.disabled,false);assert.match(ui.notice.textContent,/暫時未能保存/);
});

test('observed children and unregistered identities have no logout mutation',t=>{
  const f=fixture(t);logoutAgent({agent_id:A,name:'Child',is_subagent:true});logoutAgent({name:'Unregistered'});
  assert.equal(f.body.children.length,0);assert.equal(f.calls.length,0);
});

test('a changed uncertain-command challenge needs another explicit click and never reuses the stale token',async t=>{
  const f=fixture(t),ui=f.open(),first=ui.submit.emit('click');f.respond(0,unknown(),409);await first;
  const stale=ui.submit.emit('click');assert.equal(f.calls[1].body.logout_token,TOKEN);f.respond(1,unknown(NEW_TOKEN),409);await stale;
  assert.equal(f.calls.length,2);assert.equal(ui.dialog.open,true);assert.equal(ui.submit.textContent,'仍要登出，保留紀錄');
  assert.ok(!ui.description.textContent.includes(NEW_TOKEN));assert.ok(!ui.notice.textContent.includes(NEW_TOKEN));
  const fresh=ui.submit.emit('click');assert.deepEqual(f.calls[2].body,{agent_id:A,acknowledge_unconfirmed:true,logout_token:NEW_TOKEN});
  f.respond(2,{logged_out:true,agent_id:A});await fresh;assert.equal(ui.dialog.open,false);
});

test('missing or malformed challenges fail closed without sending an acknowledgement',async t=>{
  const f=fixture(t);
  for(const token of [undefined,'bad',TOKEN.slice(1),TOKEN.toUpperCase(),{token:TOKEN}]){
    const ui=f.open(),index=f.calls.length,first=ui.submit.emit('click');f.respond(index,{error:'agent_has_unconfirmed_commands',logout_token:token},409);await first;
    assert.equal(ui.submit.textContent,'重新核對');assert.match(ui.notice.textContent,/未能核對/);
    const retry=ui.submit.emit('click');assert.deepEqual(f.calls[index+1].body,{agent_id:A});
    f.respond(index+1,{error:'agent_has_active_commands'},409);await retry;await ui.cancel.emit('click');
  }
  assert.equal(f.events.length,0);
});

test('a closed confirmation does not transfer its token to a new dialog for the same Agent',async t=>{
  const f=fixture(t),old=f.open(),first=old.submit.emit('click');f.respond(0,unknown(),409);await first;await old.cancel.emit('click');
  const next=f.open(),request=next.submit.emit('click');assert.deepEqual(f.calls[1].body,{agent_id:A});
  f.respond(1,{logged_out:true,agent_id:A});await request;assert.equal(next.dialog.open,false);
});

test('a changed membership scope invalidates the dialog until the owner reopens logout',async t=>{
  const f=fixture(t),ui=f.open(),first=ui.submit.emit('click');f.respond(0,unknown(),409);await first;
  const acknowledged=ui.submit.emit('click');f.respond(1,{error:'logout_scope_changed'},409);await acknowledged;
  assert.equal(ui.submit.disabled,true);assert.match(ui.notice.textContent,/身份或工作狀態已改變/);assert.match(ui.notice.textContent,/關閉視窗/);
  await ui.submit.emit('click');assert.equal(f.calls.length,2);assert.equal(f.events.length,0);
  await ui.cancel.emit('click');const current=f.open(),retry=current.submit.emit('click');assert.deepEqual(f.calls[2].body,{agent_id:A});
  f.respond(2,unknown(NEW_TOKEN),409);await retry;assert.equal(current.submit.disabled,false);assert.equal(f.calls.length,3);
});
