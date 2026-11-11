import test from 'node:test';
import assert from 'node:assert/strict';
import {resolveSeats,requiredMainSeats,nearestDesk,deskLabel} from './public/office/seating.js';
import {officePost} from './public/office/office-api.js';

const seats=Array.from({length:36},(_,i)=>({id:'gf-main-'+String(i+1).padStart(3,'0'),x:i*4,z:0}));
seats.push({id:'gf-office-1-001',x:-12,z:-12},{id:'gf-ceo-001',x:-21.5,z:-11});

test('explicit saved seats survive unrelated moves and unordered snapshots',()=>{
  const bots=[{id:'visual-b',agent_id:'codex:b',ground_desk_id:'gf-main-002'},{id:'visual-a',agent_id:'codex:a',ground_desk_id:'gf-office-1-001'}];
  for(const rows of [bots,[...bots].reverse()]){
    const assigned=resolveSeats(rows,seats);assert.equal(assigned.get('visual-b').id,'gf-main-002');assert.equal(assigned.get('visual-a').id,'gf-office-1-001');
  }
  assert.equal(requiredMainSeats([{ground_desk_id:'gf-main-240'}]),240);
  assert.equal(requiredMainSeats([]),0);
  assert.equal(deskLabel('gf-office-1-002'),'辦公室 1 · 2 號工作台');
  assert.equal(deskLabel('gf-main-007'),'休息區 · 7 號休息位');assert.equal(deskLabel(null),'');assert.equal(deskLabel('gf-ceo-001'),'CEO Room · 行政工作位');
});

test('the CEO position is explicit and survives snapshot ordering',()=>{
  const bots=[{id:'ceo',agent_id:'generic:ceo',ground_desk_id:'gf-ceo-001'},{id:'guest',agent_id:'generic:guest'}];
  for(const rows of [bots,[...bots].reverse()]){const assigned=resolveSeats(rows,seats);assert.equal(assigned.get('ceo').id,'gf-ceo-001');assert.ok(assigned.get('guest').id.startsWith('gf-main-'));}
});

test('cached collisions or missing assignments never overlap avatars or auto-fill offices',()=>{
  const bots=[{id:'c',agent_id:'grok:c',ground_desk_id:'gf-main-002'},{id:'a',agent_id:'codex:a',ground_desk_id:'gf-main-002'},{id:'b',agent_id:'codex:b'}];
  const assigned=resolveSeats(bots,seats);
  assert.equal(new Set([...assigned.values()].map(s=>s.id)).size,3);
  assert.equal(assigned.get('a').id,'gf-main-002');assert.equal(assigned.get('b').id,'gf-main-001');assert.equal(assigned.get('c').id,'gf-main-003');
  assert.ok([...assigned.values()].every(s=>s.id.startsWith('gf-main-')));
});

test('drop snapping stays near an actual desk instead of pulling an outside drop into a room',()=>{
  assert.equal(nearestDesk({x:-11.8,z:-12.2},seats)?.id,'gf-office-1-001');
  assert.equal(nearestDesk({x:-6,z:-6},seats),null);
});

test('a shared sofa selects the nearest individual cushion, preserving occupied-seat checks',()=>{
  const cushions=[-1,0,1].map((x,i)=>({id:'gf-main-'+String(i+1).padStart(3,'0'),x,z:2}));
  assert.equal(nearestDesk({x:.9,z:2.2},cushions,Infinity).id,'gf-main-003');
  assert.equal(nearestDesk({x:-.9,z:2.2},cushions,Infinity).id,'gf-main-001');
});

test('membership and seating mutations use only local owner endpoints',async t=>{
  const calls=[];t.mock.method(globalThis,'fetch',async(path,options)=>{calls.push({path,options});return {ok:true,json:async()=>({accepted:true})};});
  for(const path of ['/api/office/seating','/api/office/agents/logout'])assert.equal((await officePost(path,'fixture-owner',{agent_id:'selected'})).accepted,true);
  for(const path of ['/api/agents/connect','/api/office/shutdown','https://example.invalid/api/office/seating'])await assert.rejects(officePost(path,'fixture-owner',{}));
  assert.equal(calls.length,2);assert.ok(calls.every(c=>c.options.headers['X-Office-Key']==='fixture-owner'&&c.options.credentials==='same-origin'));
});
