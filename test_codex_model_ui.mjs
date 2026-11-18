import test from 'node:test';
import assert from 'node:assert/strict';
import {CodexModelController,codexModelRequestStore,codexModelSelectionBlocked,codexModelError,openCodexModelDialog} from './public/office/codex-model-ui.js';
import {initAgentManagement} from './public/office/agent-management.js';
import {officeGet,officePost} from './public/office/office-api.js';

const ID='00000000-0000-4000-8000-000000000111',AGENT='codex:bg-00000000-0000-4000-8000-000000000211',OTHER='codex:bg-00000000-0000-4000-8000-000000000212';
const current={agent_id:AGENT,name:'Fixture',current_model:'configured-model',actual_model:'last-verified-model',can_change:true};
const ready={current_agent:current,models:[{id:'selected-model',name:'Selected model',is_default:true}]};
const receipt=(id=ID,model='selected-model')=>({request_id:id,agent_id:AGENT,model,model_saved:true,same_session:true,native_thread_preserved:true,pending_next_instruction:true,execution_verified:false});
const wait=()=>new Promise(resolve=>setImmediate(resolve));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
function storageFixture(){const values=new Map(),storage={getItem:k=>values.get(k)||null,setItem:(k,v)=>values.set(k,v),removeItem:k=>values.delete(k)};return {values,storage,store:codexModelRequestStore(()=>storage,'fixture-origin',AGENT)};}
function fixture(options={}){
  const calls=[],saved=[];let count=0;
  const controller=new CodexModelController({agent:AGENT,pendingStore:options.store||storageFixture().store,makeId:()=>count++?ID.replace(/111$/,'112'):ID,
    transport:{read:async(agent,id)=>{calls.push(['read',agent,id]);return options.read?options.read(agent,id):ready;},update:async body=>{calls.push(['update',{...body}]);return options.update?options.update(body):receipt(body.request_id,body.model);}},onSaved:detail=>saved.push(detail)});
  return {controller,calls,saved};
}

test('model setting preserves target and sends one explicit model without instruction or creation fields',async()=>{
  const store=storageFixture(),f=fixture({store:store.store,update:body=>({...receipt(body.request_id,body.model),api_key:'must-not-retain'})});await f.controller.open();
  assert.equal(await f.controller.save('selected-model'),true);
  assert.deepEqual(f.calls.at(-1),['update',{request_id:ID,agent_id:AGENT,model:'selected-model'}]);
  assert.deepEqual(f.saved,[{agent_id:AGENT,model:'selected-model'}]);assert.equal(f.controller.phase,'saved');assert.equal(store.values.size,0);
  assert.equal(f.controller.saved.execution_verified,false);assert.ok(!JSON.stringify(f.controller).includes('must-not-retain'));
});

test('missing, unrelated, retired and busy membership cannot change model or retain private status fields',async()=>{
  for(const agent of [null,{...current,agent_id:OTHER},{...current,can_change:false,reason:'codex_model_change_busy'}]){
    const f=fixture({read:()=>({...ready,current_agent:agent,api_key:'must-discard',raw_error:'/private/auth.json'})});await f.controller.open();
    assert.equal(f.controller.phase,'unavailable');assert.equal(await f.controller.save('selected-model'),false);assert.equal(f.calls.length,1);assert.ok(!JSON.stringify(f.controller).includes('must-discard'));assert.ok(!JSON.stringify(f.controller).includes('/private/auth.json'));
  }
});

test('model choice stays explicit and valid; historic rejection warns but does not prevent owner choice',async()=>{
  for(const invalid of ['',null,{},'model/path','model with space','x'.repeat(129)]){const f=fixture();await f.controller.open();assert.equal(await f.controller.save(invalid),false);assert.equal(f.calls.length,1);}
  const f=fixture({read:()=>({...ready,known_unavailable_models:['selected-model']})});await f.controller.open();assert.equal(await f.controller.save('selected-model'),true);
  assert.equal(codexModelSelectionBlocked({configured_model_requires_selection:true},''),true);
  assert.equal(codexModelSelectionBlocked({configured_model_requires_selection:true,known_unavailable_models:['selected-model']},'selected-model'),false);
  assert.equal(codexModelSelectionBlocked({configured_model_warning:true},''),false);
});

test('uncertain model save persists only scoped UUID and GET reconciles it without replay',async()=>{
  const store=storageFixture(),f=fixture({store:store.store,update:()=>{throw new Error('timeout');}});await f.controller.open();await f.controller.save('selected-model');
  assert.equal(f.controller.phase,'unknown');assert.deepEqual([...store.values.values()],[ID]);assert.equal(await f.controller.save('other-model'),false);
  const otherStore=codexModelRequestStore(()=>store.storage,'fixture-origin',OTHER);assert.equal(otherStore.load(),null);
  const recovered=fixture({store:store.store,read:(agent,id)=>({...ready,request:{request_id:id,operation:'model_change',state:'completed',response:receipt(id)}})});await recovered.controller.open();
  assert.deepEqual(recovered.calls,[['read',AGENT,ID]]);assert.equal(recovered.controller.phase,'saved');assert.equal(store.store.load(),null);assert.equal(recovered.saved.length,1);
});

test('only authoritative matching terminal receipts release uncertain UUIDs',async()=>{
  for(const request of [undefined,{request_id:ID,state:'unknown',operation:'model_change'},{request_id:ID,state:'not_found',operation:'creation'},{request_id:ID.replace(/111$/,'999'),state:'not_found'},{request_id:ID,state:'completed',operation:'model_change',response:{...receipt(),agent_id:OTHER}}]){
    const store=storageFixture();store.store.save(ID);const f=fixture({store:store.store,read:()=>({...ready,request})});await f.controller.open();assert.equal(f.controller.phase,'unknown');assert.equal(store.store.load(),ID);assert.equal(await f.controller.save('selected-model'),false);
  }
  for(const request of [{request_id:ID,state:'not_found'},{request_id:ID,state:'failed',operation:'model_change',reason:'codex_model_change_not_applied'},{request_id:ID,state:'superseded',operation:'model_change',reason:'codex_model_change_superseded'}]){
    const store=storageFixture();store.store.save(ID);const f=fixture({store:store.store,read:()=>({...ready,request})});await f.controller.open();assert.equal(f.controller.phase,'ready');assert.equal(store.store.load(),null);assert.equal(f.calls.length,1);
  }
});

test('in-flight missing receipt cannot permit double save; exact completed receipt wins over late timeout',async()=>{
  const pending=deferred();let state='not_found';const f=fixture({update:()=>pending.promise,read:(agent,id)=>({...ready,request:id?{request_id:id,state,...(state==='completed'?{operation:'model_change',response:receipt(id)}:{})}:undefined})});await f.controller.open();const saving=f.controller.save('selected-model');
  await f.controller.refresh();assert.equal(f.controller.phase,'saving');assert.equal(await f.controller.save('another-model'),false);
  state='completed';await f.controller.refresh();assert.equal(f.controller.phase,'saved');pending.reject(new Error('late timeout'));assert.equal(await saving,true);assert.equal(f.controller.phase,'saved');assert.equal(f.saved.length,1);
});

test('superseded or revoked receipt wins over a delayed old POST and does not misreport saved model',async()=>{
  for(const state of ['superseded','revoked']){
    const pending=deferred();let terminal=false;const f=fixture({update:()=>pending.promise,read:(agent,id)=>terminal?{...ready,current_agent:{...current,current_model:'newer-model'},request:{request_id:id,state,operation:'model_change',reason:state==='revoked'?'agent_membership_changed':'codex_model_change_superseded'}}:ready});
    await f.controller.open();const saving=f.controller.save('selected-model');f.controller.close();terminal=true;await f.controller.open();assert.equal(f.controller.uncertain,false);assert.equal(f.controller.requestId,null);
    pending.resolve(receipt());assert.equal(await saving,false);assert.equal(f.saved.length,0);assert.equal(f.controller.saved,null);assert.equal(f.controller.home.current_agent.current_model,'newer-model');assert.equal(f.controller.phase,'ready');
  }
});

test('old settled POST cannot mutate a newer explicit save after superseded receipt',async()=>{
  const old=deferred(),newer=deferred();let terminal=false,posts=0;
  const f=fixture({update:()=>posts++===0?old.promise:newer.promise,read:(agent,id)=>terminal?{...ready,request:{request_id:id,state:'superseded',operation:'model_change',reason:'codex_model_change_superseded'}}:ready});
  await f.controller.open();const first=f.controller.save('selected-model');terminal=true;await f.controller.refresh();const second=f.controller.save('another-model');
  old.resolve(receipt());assert.equal(await first,false);assert.equal(f.controller.phase,'saving');assert.equal(f.saved.length,0);assert.equal(f.controller.inFlight.id,ID.replace(/111$/,'112'));
  newer.resolve(receipt(ID.replace(/111$/,'112'),'another-model'));assert.equal(await second,true);assert.deepEqual(f.saved,[{agent_id:AGENT,model:'another-model'}]);
});

test('success after close is retained without reopening; stale GET cannot replace a new result',async()=>{
  const first=deferred(),post=deferred();let reads=0;const f=fixture({read:()=>reads++===0?first.promise:ready,update:()=>post.promise});const opening=f.controller.open();f.controller.close();await f.controller.open();
  const saving=f.controller.save('selected-model');f.controller.close();post.resolve(receipt());await saving;first.resolve({...ready,current_agent:null});await opening;
  assert.equal(f.controller.phase,'closed');assert.equal(f.controller.visible,false);assert.equal(f.saved.length,1);assert.equal(f.controller.home.current_agent.agent_id,AGENT);
});

test('wrong POST model, session proof or Agent receipt remains uncertain; exact known rejection is recoverable',async()=>{
  for(const response of [{...receipt(),model:'different-model'},{...receipt(),agent_id:OTHER},{...receipt(),same_session:false},{...receipt(),native_thread_preserved:false}]){
    const f=fixture({update:()=>response});await f.controller.open();assert.equal(await f.controller.save('selected-model'),false);assert.equal(f.controller.phase,'unknown');assert.equal(f.saved.length,0);
  }
  const f=fixture({update:()=>{throw Object.assign(new Error('raw secret'),{status:409,code:'codex_model_change_busy'});}});await f.controller.open();await f.controller.save('selected-model');assert.equal(f.controller.phase,'ready');assert.match(f.controller.error,/工作/);assert.ok(!f.controller.error.includes('secret'));
});

test('storage failure prevents model update and errors never echo arbitrary provider content',async()=>{
  const f=fixture({store:{load(){return null;},save(){throw new Error('quota');},clear(){}}});await f.controller.open();assert.equal(await f.controller.save('selected-model'),false);assert.equal(f.calls.length,1);assert.match(f.controller.error,/分頁儲存/);
  assert.ok(!codexModelError({code:'private path /auth.json'}).includes('/auth.json'));
});

// Minimal semantic DOM: verifies the actual dialog does not silently adopt the catalog default.
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
  showModal(){this.open=true;}close(){this.open=false;this.fire('close');}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(x=>x!==this);}
  set innerHTML(html){const stack=[this],voids=new Set(['INPUT','BR']);for(const token of html.match(/<[^>]+>|[^<]+/g)||[]){if(token.startsWith('</')){stack.pop();continue;}if(token[0]!=='<')continue;const tag=token.match(/^<([\w-]+)/)?.[1];if(!tag)continue;const node=new Element(tag,this.doc);for(const m of token.slice(tag.length+1,-1).matchAll(/([\w-]+)(?:="([^"]*)")?/g))node.setAttribute(m[1],m[2]??'');stack.at(-1).append(node);if(!voids.has(node.tagName))stack.push(node);}}
}
function dom(){const doc={createElement:tag=>new Element(tag,doc)};doc.body=new Element('body',doc);return doc;}

test('model dialog keeps current model and manual choice, permits refresh, and reuses pending identity',async t=>{
  const doc=dom(),events=new EventTarget(),calls=[],store=storageFixture();let home=ready;
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>home};});
  const options={document:doc,events,key:()=> 'fixture-owner',pendingStore:store.store};assert.equal(openCodexModelDialog('codex:app-native',options),null);
  const ui=openCodexModelDialog(AGENT,options);await wait();const form=ui.dialog.querySelector('form'),select=form.elements.model;
  assert.equal(select.value,'configured-model');assert.ok(select.options.some(option=>option.value==='configured-model'));assert.equal(ui.dialog.querySelector('.codex-model-refresh').hidden,false);
  select.value='selected-model';await select.fire('change');home={...ready,models:[{id:'different-default',is_default:true}],current_agent:{...current,current_model:'new-configured'}};await ui.controller.refresh();assert.equal(select.value,'selected-model');assert.ok(select.options.some(option=>option.value==='selected-model'));
  await ui.dialog.fire('cancel');const again=openCodexModelDialog(AGENT,options);assert.equal(again,ui);await wait();assert.equal(select.value,'selected-model');assert.equal(calls.every(c=>!c.options.method),true);
  events.dispatchEvent(new CustomEvent('office:agent-logged-out',{detail:{agent_id:AGENT}}));assert.equal(ui.dialog.open,false);ui.dispose();assert.equal(doc.body.children.length,0);
});

test('history management requires current selected non-child identity and ignores stale history after logout',async()=>{
  const doc=dom(),events=new EventTarget(),holder=doc.createElement('div'),leaves=[],models=[];
  const ui=initAgentManagement(holder,AGENT,{document:doc,events,logout:bot=>leaves.push(bot),model:id=>models.push(id)}),[settings,leave]=holder.children;
  for(const agent of [{id:AGENT,name:'Historical'},{id:AGENT,name:'Child',selected:true,is_subagent:true},{id:OTHER,name:'Other',selected:true}]){ui.update(agent);await leave.fire('click');assert.equal(holder.hidden,true);}
  const member={id:AGENT,name:'Current',selected:true};ui.update(member);assert.equal(holder.hidden,false);assert.equal(settings.hidden,false);await settings.fire('click');await leave.fire('click');assert.deepEqual(models,[AGENT]);assert.deepEqual(leaves,[{agent_id:AGENT,name:'Current'}]);
  events.dispatchEvent(new CustomEvent('office:agent-logged-out',{detail:{agent_id:AGENT}}));ui.update(member);await leave.fire('click');await settings.fire('click');assert.equal(holder.hidden,true);assert.equal(leaves.length,1);assert.equal(models.length,1);ui.dispose();assert.equal(holder.children.length,0);
});

test('API model route is explicit and logout challenge parser never forwards malformed tokens',async t=>{
  const calls=[];let data=receipt(),status=200;
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push({url,options});return {ok:status===200,status,json:async()=>data};});
  await officePost('/api/office/codex/background/model','fixture-owner',{request_id:ID,agent_id:AGENT,model:'selected-model'});await assert.rejects(officePost('/api/office/codex/background/model/unsafe','fixture-owner',{}));assert.equal(calls.length,1);
  status=409;for(const token of ['a'.repeat(64),'A'.repeat(64),'x'.repeat(64),{},'a'.repeat(63),null]){data={error:'agent_has_unconfirmed_commands',logout_token:token};await assert.rejects(officePost('/api/office/agents/logout','fixture-owner',{agent_id:AGENT}),error=>{assert.equal(error.code,'agent_has_unconfirmed_commands');assert.equal(error.logoutToken,token==='a'.repeat(64)?token:undefined);return true;});}
  data={error:'codex_background_agent_not_selected',private_detail:'/secret/auth.json'};await assert.rejects(officeGet('/api/office/codex/background/status','fixture-owner'),error=>{assert.equal(error.code,'codex_background_agent_not_selected');assert.ok(!error.message.includes('/secret'));return true;});
});
