import test from 'node:test';
import assert from 'node:assert/strict';
import {StateStore} from './public/office/lib/state.js';
import {withGroundFloor,groundLayout,GROUND_ID} from './public/office/overview.js';
import {requiredMainSeats,resolveSeats} from './public/office/seating.js';
import {withObservedSubagents,childVisualId,followerSlots,canManageAgent,childEvidenceLabel} from './public/office/subagent-view.js';
import {connectFeed} from './public/office/feed.js';
import {logoutAgent,chooseDesk} from './public/office/office-controls.js';
import {roleLabel} from './public/office/scene/dialogue.js';
import {floorTeamMetrics} from './public/office/ui/ui.js';

const root={id:'root',agent_id:'codex:root',name:'Parent',provider:'codex',floor:'project-2',desk:3,status:'working',effective_status:'working',color:'#4087E8',workspace:{id:'project',name:'Project'}};
const child=(id='one',extra={})=>({id:'child:codex:'+id,name:'Helper',provider:'codex',is_subagent:true,parent_agent_id:root.agent_id,root_agent_id:root.agent_id,depth:1,state:'working',step:'Reading source',...extra});
const state=()=>({bots:{root:{...root}},floors:[{id:'project-2',name:'Project · 2',bots:['root'],counts:{working:1},free_desks:19}],summary:{total:1,working:1},finished:[]});
const project=agents=>withObservedSubagents(state(),[{id:root.agent_id,name:root.name},...agents],id=>'/history?agent='+encodeURIComponent(id));

test('child evidence survives projection and cooperative reports never receive a native label',()=>{
  const reported=child('reported',{provider:'grok_bot',evidence:'parent_reported',source_kind:'parent_reported'});
  const native=child('native',{provider:'cursor',evidence:'native_hook',source_kind:'cursor_hooks'});
  const result=project([reported,native]);
  const reportBot=result.bots[childVisualId(reported)],nativeBot=result.bots[childVisualId(native)];
  assert.equal(reportBot.evidence,'parent_reported');assert.equal(childEvidenceLabel(reportBot),'Agent 合作回報');
  assert.equal(nativeBot.source_kind,'cursor_hooks');assert.equal(childEvidenceLabel(nativeBot),'原生事件同步');
  assert.equal(childEvidenceLabel({evidence:'unknown'}),'');
});

test('observed children keep stable identity despite duplicate names and nested parentage',()=>{
  const first=child(),nested=child('two',{parent_agent_id:first.id,depth:2}),other=child('three');
  const result=project([first,nested,other]);
  assert.equal(Object.keys(result.bots).length,4);
  assert.equal(result.bots[childVisualId(first)].floor,root.floor);
  assert.equal(result.bots[childVisualId(nested)].parent_agent_id,first.id);
  assert.equal(result.bots[childVisualId(nested)].parent_name,first.name);
  assert.equal(result.bots.root.child_count,3);
  for(const bot of Object.values(result.bots).filter(bot=>bot.is_subagent)){
    assert.equal(bot.desk,null);assert.equal(bot.ground_desk_id,null);
    assert.equal(bot.command_capability.can_send,false);
    assert.ok(bot.chat_url.includes(encodeURIComponent(bot.agent_id)));
    assert.notEqual(bot.id,root.id);
  }
  assert.equal(result.subagent_count,3);
  assert.deepEqual(result.summary,{total:1,working:1});
  assert.deepEqual(result.floors[0].counts,{working:1});
  assert.equal(result.floors[0].free_desks,19);
});

test('children never allocate workspace floors or move the twenty-first selected member',()=>{
  const store=new StateStore(),config={};
  for(let i=0;i<21;i++){
    const name='Member '+i;config[name]={floor:'project'};store.apply({bot:name,status:'working'});
  }
  store.setBotsConfig(config);store.commit();
  const baseline=store.snapshot(),members=Object.values(baseline.bots);
  members.forEach((bot,index)=>bot.agent_id='selected:'+index);
  const last=members[20],children=Array.from({length:35},(_,i)=>child(String(i),{root_agent_id:last.agent_id,parent_agent_id:last.agent_id}));
  const result=withObservedSubagents(baseline,[{id:last.agent_id,name:last.name},...children],id=>id);
  assert.equal(result.floors.length,2);assert.equal(result.summary.total,21);
  assert.equal(result.bots[last.id].floor,last.floor);assert.equal(result.bots[last.id].desk,last.desk);
  for(const observed of children)assert.equal(result.bots[childVisualId(observed)].floor,last.floor);
  assert.equal(store.bots.size,21);assert.equal(store.members.get(last.floor).size,1);
});

test('G/F distinguishes transient children from selected members and never inflates saved seats',()=>{
  const result=withGroundFloor(project([child(),child('two')]));
  assert.equal(result.floors[0].id,GROUND_ID);assert.equal(result.floors[0].bots.length,3);
  assert.equal(result.floors[0].counts.working,1);assert.equal(result.summary.total,1);
  assert.equal(result.subagent_count,2);
  assert.equal(requiredMainSeats(Object.values(result.bots)),1);
  const placements=resolveSeats(Object.values(result.bots),groundLayout().seats);
  assert.equal(placements.size,1);assert.equal(placements.has('root'),true);
});

test('the floor team card reports selected-member work separately from child work and attention',()=>{
  const metrics=floorTeamMetrics([
    {status:'working'}, {status:'done'}, {status:'error'},
    {is_subagent:true,status:'working'}, {is_subagent:true,status:'working'},
    {is_subagent:true,status:'working',effective_status:'waiting_user'},
    {is_subagent:true,status:'done'},
  ]);
  assert.equal(metrics.members.total,3);assert.equal(metrics.members.working,1);assert.equal(metrics.members.errors,1);assert.equal(metrics.members.waiting,0);
  assert.equal(metrics.children.total,4);assert.equal(metrics.children.working,2);assert.equal(metrics.children.waiting,1);assert.equal(metrics.children.done,1);
  assert.equal(metrics.rosterLabel,'3 位成員 + 4 位子 Agent');
  assert.equal(metrics.childLabel,'子 Agent · 2 工作中 · 1 待確認 · 1 完成');
  const onlyChildren=floorTeamMetrics([{is_subagent:true,status:'working'}]);
  assert.equal(onlyChildren.members.working,0);assert.equal(onlyChildren.children.working,1);
  const noChildren=floorTeamMetrics([{status:'working'}]);
  assert.equal(noChildren.rosterLabel,'1 位成員');assert.equal(noChildren.childLabel,'');
});

test('children disappear with their root and a retired child cannot remain in a cached projection',()=>{
  const observed=project([child()]);
  const gone=withObservedSubagents(observed,[],id=>id);
  assert.equal(gone.subagent_count,0);assert.equal(Object.keys(gone.bots).length,1);
  assert.deepEqual(gone.floors[0].bots,['root']);
  const noRoot=withObservedSubagents({bots:{},floors:[],summary:{total:0}},[child()],id=>id);
  assert.equal(Object.keys(noRoot.bots).length,0);assert.equal(noRoot.subagent_count,0);
});

test('follower reservations survive siblings retiring or arriving and translate with a moving root',()=>{
  const bounds={minX:-10,maxX:10,minZ:-8,maxZ:8},roots=new Map([[root.agent_id,{pos:{x:0,y:.42,z:0},yaw:0}]]);
  const children=[child(),child('two')].map(agent=>({...agent,id:childVisualId(agent),agent_id:agent.id}));
  const first=followerSlots(children,roots,bounds);
  assert.equal(first.size,2);
  const arriving=child('aaa'),bots=[{...arriving,id:childVisualId(arriving),agent_id:arriving.id},...children];
  const second=followerSlots(bots.reverse(),roots,bounds,first);
  for(const child of children)assert.deepEqual(second.get(child.id),first.get(child.id));
  const movedRoots=new Map([[root.agent_id,{pos:{x:2,y:0,z:1},yaw:0}]]);
  const moved=followerSlots(children,movedRoots,bounds,second);
  for(const child of children){assert.equal(moved.get(child.id).x,first.get(child.id).x+2);assert.equal(moved.get(child.id).z,first.get(child.id).z+1);assert.equal(moved.get(child.id).y,0);}
});

test('larger follower teams remain in bounds and avoid roots, furniture and one another',()=>{
  const bounds={minX:-9.5,maxX:9.5,minZ:-7.5,maxZ:7.5};
  const roots=new Map([[root.agent_id,{pos:{x:8,y:0,z:6},yaw:0}],['other',{pos:{x:5,y:0,z:4},yaw:0}]]);
  const furniture=[{pos:{x:6.4,z:5.1}},null];
  const children=Array.from({length:32},(_,i)=>{const agent=child(String(i));return {...agent,agent_id:agent.id,id:childVisualId(agent)};});
  const slots=followerSlots(children,roots,bounds,new Map(),furniture),all=[...slots.values()];
  assert.equal(slots.size,32);
  for(let i=0;i<all.length;i++){
    const at=all[i];assert.ok(at.x>=bounds.minX+.65&&at.x<=bounds.maxX-.65&&at.z>=bounds.minZ+.65&&at.z<=bounds.maxZ-.65);
    for(const root of roots.values())assert.ok(Math.hypot(at.x-root.pos.x,at.z-root.pos.z)>=1.2);
    assert.ok(Math.hypot(at.x-6.4,at.z-5.1)>=.8);
    for(let j=0;j<i;j++)assert.ok(Math.hypot(at.x-all[j].x,at.z-all[j].z)>=1.05);
  }
});

test('a selected child from a crowded family receives a standing slot before stable sibling reservations',()=>{
  const bounds={minX:-9.5,maxX:9.5,minZ:-7.5,maxZ:7.5};
  const roots=new Map([[root.agent_id,{pos:{x:8,y:0,z:6},yaw:0}]]);
  const children=Array.from({length:128},(_,i)=>{const agent=child(String(i).padStart(3,'0'));return {...agent,agent_id:agent.id,id:childVisualId(agent)};});
  const before=followerSlots(children,roots,bounds),missing=children.find(bot=>!before.has(bot.id));
  assert.ok(missing,'fixture must be crowded enough to exhaust ordinary slots');
  const selected=followerSlots(children,roots,bounds,before,[],missing.id);
  assert.ok(selected.has(missing.id));
  const again=followerSlots(children,roots,bounds,selected,[],missing.id);
  assert.deepEqual(again.get(missing.id),selected.get(missing.id));
  assert.ok(selected.size<=children.length);
});

test('observation is read-only through exported controls and delegated prompts identify their author',async()=>{
  const bot={agent_id:'child:codex:one',is_subagent:true,name:'Child'};
  assert.equal(canManageAgent(bot),false);assert.equal(canManageAgent(root),true);
  // No document or network exists in this test: guarded calls must return early.
  logoutAgent(bot);await chooseDesk(bot);
  assert.equal(roleLabel({role:'user'},bot),'主 Agent 指派');
  assert.equal(roleLabel({role:'user'},root),'你');
});

test('live feed projects children after root allocation and retains child history links after retirement',async(t)=>{
  const originals=new Map(),intervals=new Map(),states=[];
  const replace=(key,value)=>{originals.set(key,Object.getOwnPropertyDescriptor(globalThis,key));Object.defineProperty(globalThis,key,{configurable:true,writable:true,value});};
  t.after(()=>{for(const [key,value] of originals)if(value)Object.defineProperty(globalThis,key,value);else delete globalThis[key];});
  replace('localStorage',{getItem:()=>null,setItem(){},removeItem(){}});
  replace('setInterval',(fn,ms)=>{intervals.set(ms,fn);return ms;});replace('clearInterval',id=>intervals.delete(id));replace('setTimeout',()=>1);replace('clearTimeout',()=>{});
  const parent={id:root.agent_id,name:'Helper',provider:'codex',workspace:{id:'project',name:'Project'},state:'working',state_changed_at:new Date().toISOString()};
  let agents=[parent,child(),child('two')],finished=[];
  replace('fetch',async(url)=>({ok:true,json:async()=>url==='/health'?{ok:true,local_ok:true}:{agents,finished,local_ok:true,feed_enabled:false}}));
  const session=await connectFeed({topic:'fixture-child-office',servers:[]},state=>states.push(state));
  const latest=()=>states.at(-1);
  assert.equal(Object.keys(latest().bots).length,3);assert.equal(latest().summary.total,1);
  assert.equal(latest().floors.length,2);assert.equal(latest().floors[0].counts.working,1);
  const id=childVisualId(child()),bot=latest().bots[id];
  assert.equal(bot.agent_id,child().id);assert.equal(bot.parent_name,'Helper');assert.equal(bot.floor,'project');
  assert.equal(latest().subagent_count,2);assert.equal(bot.command_capability.can_send,false);
  agents=[parent,{...child(),state:'done'}];await intervals.get(2000)();
  assert.equal(latest().bots[id].status,'done');assert.equal(latest().subagent_count,1);
  finished=[{id:'delivery',agent_id:child().id,name:'Helper',status:'done',is_subagent:true,delivery_preview:'Child delivered'}];
  agents=[parent];await intervals.get(2000)();
  assert.equal(latest().bots[id],undefined);assert.equal(latest().subagent_count,0);
  assert.equal(latest().finished[0].bot,id);assert.ok(latest().finished[0].url.includes(encodeURIComponent(child().id)));
  assert.equal(latest().summary.total,1);session.close();
});

test('child-source verification failure downgrades the shared badge without losing roots and recovers live',async(t)=>{
  const originals=new Map(),intervals=new Map(),states=[];
  const replace=(key,value)=>{originals.set(key,Object.getOwnPropertyDescriptor(globalThis,key));Object.defineProperty(globalThis,key,{configurable:true,writable:true,value});};
  t.after(()=>{for(const [key,value] of originals)if(value)Object.defineProperty(globalThis,key,value);else delete globalThis[key];});
  replace('localStorage',{getItem:()=>null,setItem(){},removeItem(){}});
  replace('setInterval',(fn,ms)=>{intervals.set(ms,fn);return ms;});replace('clearInterval',id=>intervals.delete(id));replace('setTimeout',()=>1);replace('clearTimeout',()=>{});
  const parent={id:root.agent_id,name:root.name,provider:'codex',workspace:root.workspace,state:'working',state_changed_at:new Date().toISOString()};
  let childrenOK=false,rootOK=true;
  replace('fetch',async(url)=>({ok:true,json:async()=>url==='/health'?{ok:true,local_ok:true}:{agents:[parent,child()],finished:[],local_ok:rootOK,subagents_ok:childrenOK,feed_enabled:false}}));
  const session=await connectFeed({topic:'fixture-child-verification',servers:[]},state=>states.push(state));
  const latest=()=>states.at(-1);
  assert.equal(latest().connection.status,'syncing');
  assert.equal(latest().connection.label,'主 Agent 已同步 · 子 Agent 狀態待確認');
  assert.equal(latest().subagents_ok,false);assert.equal(latest().summary.total,1);
  assert.equal(Object.keys(latest().bots).length,2);
  const member=Object.values(latest().bots).find(bot=>!bot.is_subagent),identity=member.id;
  assert.equal(member.status,'working');
  childrenOK=true;await intervals.get(2000)();
  assert.equal(latest().connection.status,'live');assert.equal(latest().connection.label,'本機即時同步');
  assert.equal(latest().subagents_ok,true);assert.equal(latest().bots[identity].status,'working');
  childrenOK=false;rootOK=false;await intervals.get(2000)();
  assert.equal(latest().connection.status,'syncing');assert.equal(latest().connection.label,'同步中・狀態待確認');
  assert.equal(latest().bots[identity].status,'working');session.close();
});
