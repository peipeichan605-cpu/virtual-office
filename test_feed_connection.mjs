import test from 'node:test';
import assert from 'node:assert/strict';
import { connectFeed } from './public/office/feed.js';
import { mergeRunRows } from './public/office/office-api.js';
import { dialoguePlacement } from './public/office/scene/dialogue.js';
import { StateStore } from './public/office/lib/state.js';
import { withGroundFloor,GROUND_ID,selectionFloor,groundLayout } from './public/office/overview.js';

test('zoomed out dialogue stays under the toolbar with a scrollable log', () => {
  const result=dialoguePlacement(383,883,140);
  assert.equal(result.below,false);
  assert.ok(result.height>=80);
  assert.ok(383-140-result.height>=84);
});

test('an actor near the top keeps its dialogue visible below the same head anchor', () => {
  const result=dialoguePlacement(110,640,140);
  assert.equal(result.below,true);
  assert.ok(result.height>=80);
  assert.ok(110+140+result.height<=624);
});

test('a saved working row becomes completed while older history remains available', () => {
  const older={id:'old',started_at:'2026-01-01',status:'done'};
  const working={id:'active',started_at:'2026-01-02',status:'working'};
  const done={...working,status:'done',delivery_preview:'Delivered the report'};
  const result=mergeRunRows([working,older],[done]);
  assert.equal(result.length,2);
  assert.deepEqual(result[0],done);
  assert.deepEqual(result[1],older);
});

test('cached agents never imply live service; disconnect and reconnect track real health and history', async (t) => {
  const originals = new Map();
  const replace = (key, value) => {
    originals.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  };
  t.after(() => { for (const [key, descriptor] of originals) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor);
    else delete globalThis[key];
  } });
  const intervals = new Map(), streams = [], states = [];
  let localOnline = false, historyOnline = false, syncOK = true, status = 'working';
  const now = Math.floor(Date.now() / 1000);
  const line = (id, time, message) => ({ event: 'message', id, time, message: JSON.stringify(message) });
  const cached = [line('roster', now - 5, { roster: ['Selected test agent'] })];
  replace('localStorage', { getItem: () => JSON.stringify(cached), setItem() {}, removeItem() {} });
  replace('setInterval', (fn, ms) => { intervals.set(ms, fn); return ms; });
  replace('clearInterval', (id) => intervals.delete(id));
  replace('setTimeout', () => 1);
  replace('clearTimeout', () => {});
  replace('EventSource', class {
    constructor() { streams.push(this); }
    close() { this.closed = true; }
  });
  replace('fetch', async (url, options) => {
    if (url === '/api/office/work') {
      assert.equal(options.headers['X-Office-Key'], 'fixture-office');
      if (!localOnline) throw new TypeError('Service stopped');
      return { ok: true, json: async () => ({ agents: [{ id: 'codex:chosen', name: 'Selected test agent' }],
        finished: [{ id: 'fixture-run', agent_id: 'codex:chosen', name: 'Selected test agent', status: 'done', started_at: new Date().toISOString(), delivery_preview: 'Fixture delivery' }] }) };
    }
    if (url === '/health') {
      if (!localOnline) throw new TypeError('Service stopped');
      return { ok: true, json: async () => ({ ok: true, sync_ok: syncOK }) };
    }
    assert.match(url, /^http:\/\/127\.0\.0\.1:4318\/fixture-office\/json\?/);
    return { ok: historyOnline, text: async () => JSON.stringify(line(status, now + (status === 'done' ? 1 : 0), { bot: 'Selected test agent', status })) };
  });
  const session = await connectFeed({ topic: 'fixture-office', servers: ['http://127.0.0.1:4318'] }, (state) => states.push(state));
  const latest = () => states.at(-1);
  assert.equal(Object.keys(latest().bots).length, 1);
  assert.equal(latest().connection.status, 'offline');

  localOnline = true;
  await intervals.get(5000)();
  await streams[0].onopen();
  assert.equal(latest().connection.status, 'syncing');

  historyOnline = true;
  await streams[0].onopen();
  assert.equal(latest().connection.status, 'live');
  assert.equal(Object.values(latest().bots)[0].status, 'working');
  await intervals.get(2000)();
  assert.match(Object.values(latest().bots)[0].chat_url, /^\/office\/agent.html\?agent=codex%3Achosen#fixture-office$/);
  assert.equal(latest().finished[0].step, 'Fixture delivery');
  assert.equal(latest().finished[0].persisted, true);

  streams[0].onerror();
  assert.equal(latest().connection.status, 'syncing');
  localOnline = false;
  await intervals.get(5000)();
  assert.equal(latest().connection.status, 'offline');

  localOnline = true; status = 'done';
  await streams[0].onopen();
  await intervals.get(5000)();
  assert.equal(latest().connection.status, 'live');
  assert.equal(Object.values(latest().bots)[0].status, 'done');

  syncOK = false;
  await intervals.get(5000)();
  assert.equal(latest().connection.status, 'syncing');
  session.close();
  assert.equal(intervals.size, 0);
  assert.equal(streams[0].closed, true);
});


test('selected local agents sync during mailbox limits and delayed feed cannot undo them', async (t) => {
  const originals = new Map();
  const replace = (key, value) => {
    originals.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  };
  t.after(() => { for (const [key, descriptor] of originals) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key];
  } });
  const intervals = new Map(), streams = [], states = [];
  const now = Date.now();
  let online = true, workOnline = true, status = 'working', changed = new Date(now - 4 * 3600e3).toISOString();
  const row = (id, message) => ({ event: 'message', id, time: now / 1000, message: JSON.stringify(message) });
  replace('localStorage', { getItem: () => JSON.stringify([
    row('old-roster', { roster: ['Selected', 'Not enrolled'] }),
    row('old-status', { bot: 'Selected', status: 'done' }),
  ]), setItem() {}, removeItem() {} });
  replace('setInterval', (fn, ms) => { intervals.set(ms, fn); return ms; });
  replace('clearInterval', (id) => intervals.delete(id));
  replace('setTimeout', () => 1); replace('clearTimeout', () => {});
  replace('EventSource', class { constructor() { streams.push(this); } close() {} });
  replace('fetch', async (url) => {
    if (!online) throw new Error('Local service stopped');
    if (url === '/health') return { ok: true, json: async () => ({ ok: true, local_ok: true, sync_ok: false }) };
    if (url === '/api/office/work') {
      if (!workOnline) throw new Error('Local snapshot unavailable');
      return { ok: true, json: async () => ({ local_ok: true, history_ok: true,
        agents: [{ id: 'codex:chosen', name: 'Selected', provider: 'codex', state: status, state_changed_at: changed }], finished: [] }) };
    }
    return { ok: false, status: 429 };
  });
  const session = await connectFeed({ topic: 'fixture-office', servers: ['http://127.0.0.1:4318'] }, (state) => states.push(state));
  const latest = () => states.at(-1), bot = () => Object.values(latest().bots)[0];
  assert.equal(latest().connection.status, 'live');
  assert.equal(latest().connection.label, '本機即時同步');
  assert.equal(Object.keys(latest().bots).length, 1);
  assert.equal(bot().effective_status, 'working'); // Four-hour jobs are not silently made idle.
  assert.equal(latest().floors[0].id, GROUND_ID);
  const events = bot().today.events;
  streams[0].onmessage({ data: JSON.stringify(row('late-done', { bot: 'Selected', status: 'done' })) });
  streams[0].onmessage({ data: JSON.stringify(row('intruder', { bot: 'Not enrolled', status: 'working' })) });
  streams[0].onmessage({ data: JSON.stringify(row('old-groups', { groups: { Selected: 'Codex' } })) });
  await intervals.get(2000)();
  assert.equal(latest().floors[0].id, GROUND_ID);
  assert.equal(bot().status, 'working');
  assert.equal(bot().today.events, events);
  assert.equal(Object.keys(latest().bots).length, 1);
  status = 'done'; changed = new Date(now).toISOString();
  await intervals.get(2000)();
  assert.equal(bot().status, 'done');
  const doneEvents = bot().today.events;
  await intervals.get(2000)();
  assert.equal(bot().today.events, doneEvents);
  workOnline = false; await intervals.get(2000)();
  assert.equal(latest().connection.status, 'syncing');
  workOnline = true; await intervals.get(2000)();
  assert.equal(latest().connection.status, 'live');
  online = false; await intervals.get(5000)();
  assert.equal(latest().connection.status, 'offline');
  session.close();
});

test('same workspace joins providers; moves preserve identity, history and updated floor labels', async(t)=>{
  const originals=new Map(),intervals=new Map(),states=[];
  const replace=(key,value)=>{originals.set(key,Object.getOwnPropertyDescriptor(globalThis,key));Object.defineProperty(globalThis,key,{configurable:true,writable:true,value});};
  t.after(()=>{for(const [key,descriptor] of originals)if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key];});
  const a={id:'workspace-alpha',name:'Project · alpha'},b={id:'workspace-beta',name:'Project · beta'};
  let agents=[{id:'codex:a',name:'Codex A',provider:'codex',workspace:a},{id:'grok_bot:b',name:'Grok B',provider:'grok_bot',workspace:a},{id:'crouton:c',name:'Crouton C',provider:'crouton',workspace:b}].map(agent=>({...agent,state:'working',state_changed_at:'2026-01-01T00:00:00Z'}));
  replace('localStorage',{getItem:()=>null,setItem(){},removeItem(){}});replace('setInterval',(fn,ms)=>{intervals.set(ms,fn);return ms;});replace('clearInterval',id=>intervals.delete(id));replace('setTimeout',()=>1);replace('clearTimeout',()=>{});
  replace('fetch',async(url)=>({ok:true,json:async()=>url==='/health'?{ok:true,local_ok:true}:{local_ok:true,feed_enabled:false,agents,finished:[{id:'saved',name:'Grok B',agent_id:'grok_bot:b',status:'done',delivery_preview:'Saved delivery'}]}}));
  const session=await connectFeed({topic:'workspace-fixture',servers:[]},state=>states.push(state));
  const latest=()=>states.at(-1),bot=name=>Object.values(latest().bots).find(b=>b.name===name);
  assert.equal(latest().floors.length,3);assert.equal(bot('Codex A').floor,bot('Grok B').floor);assert.notEqual(bot('Codex A').floor,bot('Crouton C').floor);
  assert.equal(latest().floors[0].bots.length,3);assert.equal(latest().summary.total,3);
  const oldId=bot('Grok B').id,events=bot('Grok B').today.events;
  agents=agents.map(agent=>agent.id==='grok_bot:b'?{...agent,workspace:b}:agent);await intervals.get(2000)();
  assert.equal(bot('Grok B').floor,bot('Crouton C').floor);assert.equal(bot('Grok B').id,oldId);assert.equal(bot('Grok B').today.events,events);assert.equal(latest().finished[0].step,'Saved delivery');
  b.name='Renamed project';await intervals.get(2000)();assert.equal(latest().floors.find(f=>f.id===b.id).name,'Renamed project');session.close();
});

test('a crowded workspace continues on named floors without overlapping desks',()=>{
  const store=new StateStore();store.setFloorsConfig({floors:[{id:'workspace-project',name:'Project'}]});const cfg={};
  for(let i=0;i<23;i++){cfg['Agent '+i]={floor:'workspace-project'};store.apply({bot:'Agent '+i,status:'idle'});}
  store.setBotsConfig(cfg);store.commit();const state=store.snapshot();assert.equal(state.floors.length,2);
  assert.deepEqual(state.floors.map(f=>f.name),['Project','Project · 2']);
  assert.equal(new Set(Object.values(state.bots).map(b=>b.floor+':'+b.desk)).size,23);
});

test('G/F contains the entire roster once, keeps workspace homes and never changes totals or history',()=>{
  const state={bots:{a:{id:'a',floor:'project',status:'working',agent_id:'codex:a',latest_message:{text:'Progress'},chat_url:'/office/agent.html'},b:{id:'b',floor:'workspace-unassigned',status:'idle'},c:{id:'c',floor:'workspace-unassigned-2',status:'error'}},summary:{total:3,working:1,idle:1,error:1},finished:[{id:'delivery'}],floors:[{id:'project',order:1,bots:['a']},{id:'workspace-unassigned',bots:['b']},{id:'workspace-unassigned-2',bots:['c']}]};
  const result=withGroundFloor(state),again=withGroundFloor(result);
  assert.deepEqual(result.floors[0].bots,['a','b','c']);assert.equal(result.floors.length,2);assert.equal(result.floors[0].order,0);
  assert.equal(result.bots.a.floor,'project');assert.equal(result.bots.b.floor,GROUND_ID);assert.equal(state.bots.b.floor,'workspace-unassigned');
  assert.equal(result.summary,state.summary);assert.equal(result.finished,state.finished);assert.equal(result.bots.a.latest_message.text,'Progress');assert.equal(result.floors[0].counts.working,1);assert.deepEqual(result,again);
  assert.equal(selectionFloor(result,GROUND_ID,'a'),GROUND_ID);assert.equal(selectionFloor(result,'another','a'),'project');
});

test('G/F remains available with no agents and its larger hall can seat every selected agent',()=>{
  const state=withGroundFloor({bots:{},floors:[],summary:{total:0},finished:[]});assert.equal(state.floors.length,1);assert.deepEqual(state.floors[0].bots,[]);
  for(const count of [0,1,9,21,80,400]){
    const layout=groundLayout(count);assert.ok(layout.width*layout.depth>=3*16.4*12.4);assert.ok(layout.seats.length>=count);
    assert.equal(new Set(layout.seats.map(s=>s.x+':'+s.z)).size,layout.seats.length);
    assert.ok(layout.seats.every(s=>s.x>layout.minX+2&&s.x<layout.maxX-2&&s.z>layout.minZ+2&&s.z<layout.maxZ-2));
  }
});

test('cached synthetic green Grok looks cannot replace the original default, but live reported looks remain editable',async(t)=>{
  const originals=new Map();const replace=(key,value)=>{originals.set(key,Object.getOwnPropertyDescriptor(globalThis,key));Object.defineProperty(globalThis,key,{configurable:true,writable:true,value});};
  t.after(()=>{for(const [key,value] of originals)if(value)Object.defineProperty(globalThis,key,value);else delete globalThis[key];});
  const streams=[],states=[],name='Original Grok Fixture',now=Math.floor(Date.now()/1000);
  const row=(id,time,look)=>({id,time,event:'message',message:JSON.stringify({looks:{[name]:look}})});
  const legacy={shape:'sphere',color:'#6CAC72',accessory:'glasses'};
  replace('localStorage',{getItem:()=>JSON.stringify([row('old-synthetic',now-10,legacy)]),setItem(){},removeItem(){}});
  replace('setInterval',()=>1);replace('clearInterval',()=>{});replace('setTimeout',()=>1);replace('clearTimeout',()=>{});
  replace('EventSource',class{constructor(){streams.push(this);}close(){}});
  replace('fetch',async(url)=>{
    if(url==='/api/office/work')return {ok:true,json:async()=>({local_ok:true,history_ok:true,feed_enabled:true,agents:[{id:'grok_bot:fixture',provider:'grok_bot',name,state:'idle',state_changed_at:new Date().toISOString()}],finished:[]})};
    if(url==='/health')return {ok:true,json:async()=>({ok:true,local_ok:true,sync_ok:true})};
    return {ok:true,text:async()=>''};
  });
  const session=await connectFeed({topic:'fixture-grok',servers:['http://127.0.0.1:4318']},state=>states.push(state));
  const bot=()=>Object.values(states.at(-1).bots)[0];
  assert.deepEqual(bot().look,{shape:'sphere',color:'#FC6A14',accessory:'stem'});assert.equal(bot().look_origin,'platform-default');
  streams[0].onmessage({data:JSON.stringify(row('fresh-custom',now+1,{shape:'flower',color:'#FF309B',accessory:'none'}))});
  assert.equal(bot().look.shape,'flower');assert.equal(bot().look_origin,'reported');
  streams[0].onmessage({data:JSON.stringify(row('fresh-green',now+2,legacy))});
  assert.deepEqual(bot().look,legacy);assert.equal(bot().look_origin,'reported');session.close();
});
