import test from 'node:test';
import assert from 'node:assert/strict';
import {DialogueBubble,publicEvidenceLabel,mergeMessages} from './public/office/scene/dialogue.js';

class Node {
  constructor(tag='div'){
    this.tag=tag;this.children=[];this.dataset={};this.style={};this.attributes={};this.textContent='';
    this.scrollHeight=100;this.scrollTop=0;this.clientHeight=100;this.offsetHeight=30;
    this.classList={toggle(){},remove(){}};this.parts=new Map();
  }
  setAttribute(key,value){this.attributes[key]=value;}
  append(...nodes){this.children.push(...nodes);}
  insertBefore(node,before){
    this.children=this.children.filter(item=>item!==node);
    const at=this.children.indexOf(before);this.children.splice(at<0?this.children.length:at,0,node);
  }
  querySelector(selector){
    if(!selector.startsWith('.'))return this.children.find(node=>node.tag===selector)||null;
    if(!this.parts.has(selector))this.parts.set(selector,new Node());return this.parts.get(selector);
  }
  replaceChildren(...nodes){this.children=[...nodes];}
  contains(){return false;}
  focus(){}
  remove(){}
}
function fixture(t){
  const previous={document:globalThis.document,location:globalThis.location};
  globalThis.document={createElement:tag=>new Node(tag)};globalThis.location={hash:'#fixture-office'};
  const pending=[];
  t.mock.method(globalThis,'fetch',(url)=>new Promise((resolve,reject)=>pending.push({url,resolve:body=>resolve({ok:true,json:async()=>body}),reject})));
  const reader=Object.assign(Object.create(DialogueBubble.prototype),{
    bot:bot('A'),open:true,teamOpen:false,children:[],rows:[],cursor:0,before:null,pending:false,
    requestEpoch:0,pendingRequest:null,signature:'',nodes:new Map(),selected:true,
    element:new Node(),detail:new Node(),peek:new Node(),family:new Node(),team:new Node(),
    notice:new Node(),older:new Node(),list:new Node(),move:new Node(),logout:new Node(),status:new Node(),
    composer:{element:new Node(),bind(){},refresh(){},dispose(){this.disposed=true;}},
  });
  t.after(()=>{
    reader.disable();
    for(const [key,value] of Object.entries(previous)){if(value===undefined)delete globalThis[key];else globalThis[key]=value;}
  });
  return {reader,pending};
}
const bot=(id,extra={})=>({agent_id:id,name:'Agent '+id,status:'working',command_capability:{},...extra});
const row=(seq,text)=>({seq,event_id:'event-'+seq,role:'assistant',phase:'commentary',text,at:'2026-11-10T10:00:00Z'});
const response=(messages=[],cursor=0,before=null)=>({messages,cursor,before});

const scoped=(event_id,seq,text,phase='final',agent_id='cursor:selected',run_id='native-turn')=>
  ({event_id,seq,text,role:'assistant',phase,agent_id,run_id,at:'2026-11-10T10:00:00Z'});

test('a revised stable bubble replaces its earlier public prefix and an already-visible native final',()=>{
  const previous=[scoped('bubble',1,'Preparing','commentary'),scoped('native-stop',2,'Delivered')];
  const merged=mergeMessages(previous,[scoped('bubble',3,'Delivered')]);
  assert.deepEqual(merged.map(row=>[row.event_id,row.phase,row.text]),[['bubble','final','Delivered']]);
});

test('late older pages cannot reintroduce an identical terminal envelope',()=>{
  const latest=scoped('bubble',5,'Delivered');
  const merged=mergeMessages([latest],[scoped('native-stop',2,'Delivered'),scoped('bubble',1,'Preparing','commentary')]);
  assert.deepEqual(merged,[latest]);
});

test('equal final words remain separate across Agents or native turns, and repeated progress is retained',()=>{
  const rows=[scoped('a',1,'Done'),scoped('b',2,'Done','final','cursor:other'),
    scoped('c',3,'Done','final','cursor:selected','other-turn'),
    scoped('d',4,'Checking','commentary'),scoped('e',5,'Checking','commentary')];
  assert.deepEqual(mergeMessages([],rows),rows);
});

test('unknown final scope and exact distinct bodies are not guessed to be duplicates',()=>{
  const unknown={...scoped('unknown',1,'Done'),run_id:undefined};
  const known=scoped('known',2,'Done');
  const changed=scoped('changed',3,'Done.');
  assert.deepEqual(mergeMessages([unknown],[known,changed]),[unknown,known,changed]);
});

test('an open dialogue removes the superseded native final DOM node after stable-bubble finalization',async t=>{
  const {reader,pending}=fixture(t);
  reader.rows=[scoped('bubble',1,'Preparing','commentary'),scoped('native-stop',2,'Delivered')];
  reader.cursor=2;
  const bubble=new Node('article'),native=new Node('article');let removed=false;
  bubble.append(new Node('small'),new Node('p'));
  native.remove=()=>{removed=true;reader.list.children=reader.list.children.filter(node=>node!==native);};
  reader.nodes.set('bubble',bubble);reader.nodes.set('native-stop',native);
  reader.list.children=[bubble,native];
  const refresh=reader.refresh();pending[0].resolve(response([scoped('bubble',3,'Delivered')],3));await refresh;
  assert.equal(removed,true);assert.equal(reader.nodes.has('native-stop'),false);
  assert.deepEqual(reader.list.children,[bubble]);
  assert.equal(bubble.querySelector('p').textContent,'Delivered');
  assert.deepEqual(reader.rows.map(row=>row.event_id),['bubble']);
});

test('A→B→A rejects the first A response and its finally cannot clear the current pending request',async t=>{
  const {reader,pending}=fixture(t);
  const first=reader.refresh();
  reader.update(bot('B'),true);reader.setOpen(true);
  reader.update(bot('A'),true);reader.setOpen(true);
  assert.equal(pending.length,3);
  const current=reader.pendingRequest;
  pending[0].resolve(response([row(900,'stale A')],900,899));await first;
  assert.equal(reader.cursor,0);assert.deepEqual(reader.rows,[]);assert.equal(reader.pending,true);
  assert.equal(reader.pendingRequest,current);
  pending[1].resolve(response([row(800,'stale B')],800));
  pending[2].resolve(response([row(3,'current A')],3,2));
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(reader.cursor,3);assert.deepEqual(reader.rows.map(x=>x.text),['current A']);
  assert.equal(reader.pending,false);assert.equal(reader.before,2);
});

test('closing and reopening detaches an old read without blocking the new view',async t=>{
  const {reader,pending}=fixture(t);
  const old=reader.refresh();reader.setOpen(false);reader.setOpen(true);
  assert.equal(pending.length,2);const current=reader.pendingRequest;
  reader.notice.textContent='current view';
  pending[0].reject(new Error('old network error'));await old;
  assert.equal(reader.notice.textContent,'current view');assert.equal(reader.pendingRequest,current);assert.equal(reader.pending,true);
  pending[1].resolve(response([row(2,'new public text')],2));
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(reader.rows[0].text,'new public text');assert.equal(reader.pending,false);
});

test('retirement ignores a pending success and disposes the command composer',async t=>{
  const {reader,pending}=fixture(t);
  reader.notice.textContent='retained notice';const old=reader.refresh();reader.disable();
  pending[0].resolve(response([row(8,'retired text')],8));await old;
  assert.equal(reader.cursor,0);assert.deepEqual(reader.rows,[]);assert.equal(reader.notice.textContent,'retained notice');
  assert.equal(reader.pending,false);assert.equal(reader.composer.disposed,true);assert.equal(reader.element.inert,true);
});

test('ordinary same-Agent updates do not invalidate an in-flight response',async t=>{
  const {reader,pending}=fixture(t);
  const active=reader.refresh(),ticket=reader.pendingRequest;
  reader.update(bot('A',{command_capability:{public_sync:{mode:'cooperative',scope:'reported_runs',state:'cooperative'}}}),true);
  assert.equal(reader.pendingRequest,ticket);assert.equal(pending.length,1);
  pending[0].resolve(response([row(4,'accepted')],4,3));await active;
  assert.equal(reader.rows[0].text,'accepted');assert.equal(reader.cursor,4);assert.equal(reader.before,3);
  assert.equal(reader.notice.textContent,'Agent 合作回報 · 已遮蓋敏感內容');
});

test('older history pagination preserves cursor, row order and scroll position',async t=>{
  const {reader,pending}=fixture(t);
  reader.rows=[row(10,'current')];reader.cursor=10;reader.before=9;reader.list.scrollTop=40;
  const older=reader.refresh(true);
  assert.match(pending[0].url,/before=9/);assert.doesNotMatch(pending[0].url,/after=/);
  pending[0].resolve(response([row(8,'older')],8,7));await older;
  assert.equal(reader.cursor,10);assert.equal(reader.before,7);assert.deepEqual(reader.rows.map(x=>x.seq),[8,10]);
  assert.equal(reader.list.scrollTop,40);assert.equal(reader.pending,false);
});

test('source labels describe coverage without treating dispatch verification as an App observer',()=>{
  const root=sync=>bot('root',{command_capability:{public_sync:sync,execution_verified:true}});
  assert.equal(publicEvidenceLabel(root(undefined)),'公開回報');
  assert.equal(publicEvidenceLabel(root({mode:'cooperative',scope:'reported_runs',state:'cooperative'})),'Agent 合作回報');
  assert.equal(publicEvidenceLabel(root({mode:'native_run_observer',scope:'office_dispatched_runs',state:'limited'})),'OFFICE 工作同步');
  for(const mode of ['native_observer','native_hook']){
    const sync={mode,scope:'selected_conversation',state:'configured',configured:true};
    assert.equal(publicEvidenceLabel(root(sync)),'原生公開同步');
    assert.equal(publicEvidenceLabel(root({...sync,configured:false})),'公開回報');
    assert.equal(publicEvidenceLabel(root({...sync,state:'setup_required'})),'公開回報');
  }
  assert.equal(publicEvidenceLabel({is_subagent:true,evidence:'parent_reported'}),'Agent 合作回報');
  assert.equal(publicEvidenceLabel({is_subagent:true,evidence:'native_hook'}),'原生事件同步');
});
