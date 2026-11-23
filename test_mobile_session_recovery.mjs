import test from 'node:test';
import assert from 'node:assert/strict';
import * as access from './public/office/mobile-access.js';

const reply=(body,status=200)=>({ok:status>=200&&status<300,status,json:async()=>body});
const session=token=>reply({authenticated:true,csrf_token:token,device:{device_id:token}});
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const settle=async()=>{for(let i=0;i<20;i++)await Promise.resolve();};
function target(){
  const listeners=new Map();
  return {listeners,addEventListener(name,fn){if(!listeners.has(name))listeners.set(name,new Set());listeners.get(name).add(fn);},
    removeEventListener(name,fn){listeners.get(name)?.delete(fn);},dispatchEvent(event){for(const fn of [...(listeners.get(event.type)||[])])fn(event);return true;},
    count(){return [...listeners.values()].reduce((n,set)=>n+set.size,0);}};
}
function fixture(t,fetcher){
  access.clearMobileSession();
  const events=target(),doc=target(),nodes=new Map(),timeouts=new Map(),intervals=new Map();let serial=0;
  const element=()=>({textContent:'',dataset:{},children:[],classList:{add(){},toggle(){}},append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;},addEventListener(){},setAttribute(){}});
  doc.visibilityState='visible';doc.documentElement=element();doc.cleared=false;
  doc.body={replaceChildren(){doc.cleared=true;nodes.clear();}};
  doc.createElement=element;
  doc.getElementById=id=>{if(doc.cleared)return null;if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);};
  doc.querySelector=selector=>selector.includes('office-access')?{content:'mobile'}:doc.getElementById('loading');
  const values={document:doc,window:events,location:{pathname:'/mobile/',search:'',hash:'',replace(path){this.replaced=path;}},history:{replaceState(){}},
    fetch:fetcher,CustomEvent:class{constructor(type){this.type=type;}},
    addEventListener:events.addEventListener.bind(events),removeEventListener:events.removeEventListener.bind(events),dispatchEvent:events.dispatchEvent.bind(events),
    setTimeout:(fn,ms)=>{const id=++serial;timeouts.set(id,{fn,ms});return id;},clearTimeout:id=>timeouts.delete(id),
    setInterval:(fn,ms)=>{const id=++serial;intervals.set(id,{fn,ms});return id;},clearInterval:id=>intervals.delete(id)};
  const prior=new Map();for(const [key,value] of Object.entries(values)){prior.set(key,Object.getOwnPropertyDescriptor(globalThis,key));Object.defineProperty(globalThis,key,{configurable:true,writable:true,value});}
  t.after(()=>{events.dispatchEvent({type:'pagehide'});access.clearMobileSession();for(const [key,value] of prior){if(value)Object.defineProperty(globalThis,key,value);else delete globalThis[key];}});
  return {events,doc,nodes,timeouts,intervals,async tick(){assert.equal(timeouts.size,1);const [id,item]=timeouts.entries().next().value;timeouts.delete(id);item.fn();await settle();}};
}

test('late successful session response cannot replace a new session after clear',async t=>{
  const old=deferred(),fresh=deferred();let calls=0;
  fixture(t,()=>++calls===1?old.promise:fresh.promise);
  const first=access.ensureMobileSession();const rejected=assert.rejects(first,/mobile_session_changed/);
  access.clearMobileSession();const second=access.ensureMobileSession();fresh.resolve(session('new'));await second;
  old.resolve(session('old'));await rejected;
  assert.equal((await access.ensureMobileSession()).csrf_token,'new');assert.equal(calls,2);
});

test('clearing between decoded session and assignment cannot resurrect cached authorization',async t=>{
  let calls=0;
  fixture(t,async()=>++calls===1?{ok:true,status:200,json:()=>({then(resolve){
    resolve({authenticated:true,csrf_token:'old',device:{device_id:'old'}});
    queueMicrotask(()=>access.clearMobileSession());
  }})}:session('new'));
  await assert.rejects(access.ensureMobileSession(),/mobile_session_changed/);
  assert.equal((await access.ensureMobileSession()).csrf_token,'new');assert.equal(calls,2);
});

test('clearing between session completion and bootstrap continuation cannot report ready',async t=>{
  const states=[];
  const f=fixture(t,async()=>({ok:true,status:200,json:()=>({then(resolve){
    resolve({authenticated:true,csrf_token:'old',device:{device_id:'old'}});
    queueMicrotask(()=>queueMicrotask(()=>access.clearMobileSession()));
  }})}));
  await assert.rejects(access.waitForMobileSession({onStatus:state=>states.push(state)}),/mobile_session_changed/);
  assert.equal(states.includes('ready'),false);assert.equal(f.timeouts.size,0);assert.equal(f.events.count(),0);
});

test('clearing between decoded API body and return discards private data',async t=>{
  fixture(t,async()=>({ok:true,status:200,json:()=>({then(resolve){
    resolve({private_fixture:'must not return'});queueMicrotask(()=>access.clearMobileSession());
  }})}));
  await assert.rejects(access.mobileRequest('/api/mobile/work'),/mobile_session_changed/);
});

test('late old 401 JSON cannot erase the newer session or its visible content',async t=>{
  const json=deferred();let calls=0;
  const f=fixture(t,async()=>++calls===1?{ok:false,status:401,json:()=>json.promise}:session('new'));
  const first=access.ensureMobileSession();const rejected=assert.rejects(first,/mobile_session_changed/);await settle();
  access.clearMobileSession();await access.ensureMobileSession();json.resolve({error:'mobile_session_revoked'});await rejected;
  assert.equal(f.doc.cleared,false);assert.equal(location.replaced,undefined);
  assert.equal((await access.ensureMobileSession()).csrf_token,'new');assert.equal(calls,2);
});

test('old finally does not discard the newer shared in-flight session request',async t=>{
  const old=deferred(),fresh=deferred();let calls=0;
  fixture(t,()=>++calls===1?old.promise:fresh.promise);
  const first=access.ensureMobileSession();const rejected=assert.rejects(first,/mobile_session_changed/);
  access.clearMobileSession();const second=access.ensureMobileSession();old.resolve(session('old'));await rejected;
  const third=access.ensureMobileSession();assert.equal(calls,2);
  fresh.resolve(session('new'));assert.equal((await second).csrf_token,'new');assert.equal((await third).csrf_token,'new');
});

test('session bootstrap retries a transient GET and cleans every retry listener after success',async t=>{
  let calls=0;const methods=[],states=[];
  const f=fixture(t,async(path,options)=>{calls++;methods.push(options.method||'GET');if(calls===1)throw new TypeError('offline');return session('ok');});
  const ready=access.waitForMobileSession({onStatus:state=>states.push(state)});await settle();
  assert.deepEqual([...f.timeouts.values()].map(x=>x.ms),[1000]);
  await f.tick();assert.equal((await ready).csrf_token,'ok');
  assert.deepEqual(methods,['GET','GET']);assert.deepEqual(states,['connecting','retrying','connecting','ready']);
  assert.equal(f.timeouts.size,0);assert.equal(f.events.count(),0);assert.equal(f.doc.count(),0);
});

for(const [description,incomplete] of [
  ['truncated JSON',()=>new Response('{"authenticated":',{status:200})],
  ['incomplete session',()=>reply({authenticated:true})],
])test('a successful response with '+description+' retries without requesting new pairing',async t=>{
  let calls=0;
  const f=fixture(t,async()=>++calls===1?incomplete():session('recovered'));
  const ready=access.waitForMobileSession();
  await settle();
  assert.equal(f.doc.cleared,false);assert.equal(location.replaced,undefined);
  assert.equal(f.timeouts.size,1);
  await f.tick();assert.equal((await ready).csrf_token,'recovered');
  assert.equal(f.doc.cleared,false);assert.equal(location.replaced,undefined);
});

test('an incomplete renewal response cannot dispatch the requested command or log the phone out',async t=>{
  const calls=[];let failed=true;
  const f=fixture(t,async path=>{
    calls.push(path);
    if(path==='/api/mobile/session')return failed?needsRenewal():session('recovered');
    if(path==='/api/mobile/renew'){failed=false;return reply({authenticated:true});}
    throw new Error('The command must not have been sent');
  });
  await assert.rejects(access.mobileRequest('/api/mobile/commands',{method:'POST',body:{text:'test'}}),/mobile_invalid_session_response/);
  assert.deepEqual(calls,['/api/mobile/session','/api/mobile/renew']);
  assert.equal(f.doc.cleared,false);assert.equal(location.replaced,undefined);
  assert.equal((await access.ensureMobileSession()).csrf_token,'recovered');
});

test('slow retry pauses while hidden and resumes on foreground activity',async t=>{
  let calls=0,online=false;const states=[];
  const f=fixture(t,async()=>{calls++;if(!online)throw new TypeError('offline');return session('ok');});
  const ready=access.waitForMobileSession({onStatus:state=>states.push(state)});await settle();
  const delays=[];for(let i=0;i<4;i++){delays.push([...f.timeouts.values()][0].ms);await f.tick();}
  assert.equal(calls,5);assert.deepEqual(delays,[1000,2000,4000,8000]);assert.equal(f.timeouts.size,1);assert.equal([...f.timeouts.values()][0].ms,30000);
  f.doc.visibilityState='hidden';f.doc.dispatchEvent({type:'visibilitychange'});f.events.dispatchEvent({type:'online'});await settle();assert.equal(calls,5);assert.equal(f.timeouts.size,0);assert.equal(states.at(-1),'paused');
  online=true;f.doc.visibilityState='visible';f.doc.dispatchEvent({type:'visibilitychange'});
  assert.equal((await ready).csrf_token,'ok');assert.equal(calls,6);assert.equal(f.timeouts.size,0);
});

test('a recovered private route succeeds on the slow visible probe without browser events',async t=>{
  let calls=0,online=false,completed=0;
  const f=fixture(t,async()=>{calls++;if(!online)throw new TypeError('private route unavailable');return session('ok');});
  const ready=access.waitForMobileSession().then(value=>{completed++;return value;});await settle();
  for(let i=0;i<4;i++)await f.tick();
  assert.equal(calls,5);assert.equal(completed,0);assert.equal([...f.timeouts.values()][0].ms,30000);
  // No online, visibilitychange or pageshow event accompanies this recovery.
  online=true;await f.tick();assert.equal((await ready).csrf_token,'ok');
  assert.equal(calls,6);assert.equal(completed,1);assert.equal(f.timeouts.size,0);assert.equal(f.events.count(),0);assert.equal(f.doc.count(),0);
});

for(const action of ['abort','pagehide'])test(`${action} cancels the slow probe and all startup listeners`,async t=>{
  let calls=0;const f=fixture(t,async()=>{calls++;throw new TypeError('offline');});
  const controller=new AbortController(),ready=access.waitForMobileSession({signal:controller.signal});
  const rejected=assert.rejects(ready,/mobile_session_changed/);await settle();
  for(let i=0;i<4;i++)await f.tick();
  assert.equal(calls,5);assert.equal([...f.timeouts.values()][0].ms,30000);
  if(action==='abort')controller.abort();else f.events.dispatchEvent({type:'pagehide',persisted:false});
  await rejected;assert.equal(f.timeouts.size,0);assert.equal(f.events.count(),0);assert.equal(f.doc.count(),0);
  f.events.dispatchEvent({type:'online'});await settle();assert.equal(calls,5);
});

test('hiding a page clears retries and does not start the app from a late successful response',async t=>{
  const pending=deferred();let completed=false,calls=0;
  const f=fixture(t,()=>{calls++;return pending.promise;});
  const ready=access.waitForMobileSession().then(value=>{completed=true;return value;});
  f.doc.visibilityState='hidden';f.doc.dispatchEvent({type:'visibilitychange'});pending.resolve(session('ok'));await settle();
  assert.equal(completed,false);assert.equal(f.timeouts.size,0);
  f.doc.visibilityState='visible';f.doc.dispatchEvent({type:'visibilitychange'});await ready;
  assert.equal(calls,1);assert.equal(completed,true);
});

test('terminal authentication failure clears visible content and cannot restart on online events',async t=>{
  let calls=0;const f=fixture(t,async()=>{calls++;return reply({error:'mobile_session_revoked'},401);});
  await assert.rejects(access.waitForMobileSession(),error=>error.status===401);
  assert.equal(f.doc.cleared,true);assert.equal(location.replaced,'/mobile/pair');assert.equal(f.timeouts.size,0);
  f.events.dispatchEvent({type:'online'});f.doc.dispatchEvent({type:'visibilitychange'});await settle();
  assert.equal(calls,1);assert.equal(f.events.count(),0);assert.equal(f.doc.count(),0);
});

test('scope denial is terminal and does not silently retry session reads',async t=>{
  let calls=0;const f=fixture(t,async()=>{calls++;return reply({error:'mobile_forbidden'},403);});
  await assert.rejects(access.waitForMobileSession(),error=>error.status===403);
  assert.equal(calls,1);assert.equal(f.timeouts.size,0);assert.equal(f.events.count(),0);
});

test('owner policy change fences an in-flight request without locking or replaying and the next read recovers',async t=>{
  const calls=[];let phase='changed';
  const f=fixture(t,async(path,options)=>{
    calls.push({path,method:options.method||'GET'});
    if(path==='/api/mobile/session')return session('same-csrf');
    if(phase==='changed')return reply({error:'mobile_scope_changed'},409);
    if(phase==='revoked')return reply({error:'mobile_auth_required'},401);
    return reply({agents:[{id:'newly-visible'}]});
  });
  await access.ensureMobileSession();
  await assert.rejects(access.mobileRequest('/api/mobile/commands',{method:'POST',body:{agent_id:'a',text:'fixture'}}),
    error=>error.status===409&&error.code==='mobile_scope_changed');
  assert.equal(f.doc.cleared,false);assert.equal(location.replaced,undefined);
  assert.equal(calls.filter(call=>call.method==='POST').length,1);
  assert.equal(f.timeouts.size,0);
  phase='ready';assert.deepEqual((await access.mobileRequest('/api/mobile/work')).agents,[{id:'newly-visible'}]);
  assert.equal((await access.ensureMobileSession()).csrf_token,'same-csrf');
  phase='revoked';await assert.rejects(access.mobileRequest('/api/mobile/work'),error=>error.status===401);
  assert.equal(f.doc.cleared,true);assert.equal(location.replaced,'/mobile/pair');
});

test('pagehide and abort stop bootstrap without retaining timers or listeners',async t=>{
  const f=fixture(t,async()=>{throw new TypeError('offline');});
  const ready=access.waitForMobileSession();const rejected=assert.rejects(ready,/mobile_session_changed/);await settle();
  f.events.dispatchEvent({type:'pagehide'});await rejected;
  assert.equal(f.timeouts.size,0);assert.equal(f.events.count(),0);assert.equal(f.doc.count(),0);
  const controller=new AbortController();controller.abort();
  await assert.rejects(access.waitForMobileSession({signal:controller.signal}),/mobile_session_changed/);
  assert.equal(f.events.count(),0);assert.equal(f.doc.count(),0);
});

test('late private API data after lock is discarded instead of resurfacing',async t=>{
  const pending=deferred();const f=fixture(t,path=>path==='/api/mobile/session'?Promise.resolve(session('ok')):pending.promise);
  await access.ensureMobileSession();
  const read=access.mobileRequest('/api/mobile/work');const rejected=assert.rejects(read,/mobile_session_changed/);
  access.lockMobileAccess();pending.resolve(reply({agents:[{name:'should not render'}]}));await rejected;
  assert.equal(f.doc.cleared,true);assert.equal(location.replaced,'/mobile/pair');
});

test('persisted pagehide pauses bootstrap and pageshow resumes without a reload',async t=>{
  let calls=0,online=false;const f=fixture(t,async()=>{calls++;if(!online)throw new TypeError('offline');return session('ok');});
  const ready=access.waitForMobileSession();await settle();assert.equal(f.timeouts.size,1);
  f.events.dispatchEvent({type:'pagehide',persisted:true});assert.equal(f.timeouts.size,0);
  f.events.dispatchEvent({type:'online'});await settle();assert.equal(calls,1);
  online=true;f.events.dispatchEvent({type:'pageshow',persisted:true});await ready;
  assert.equal(calls,2);assert.equal(f.events.count(),0);
});

test('history page installs its controls and polling after initial network recovery, then stops on auth lock',async t=>{
  let sessionCalls=0,historyCalls=0;
  const f=fixture(t,async path=>{
    if(path==='/api/mobile/session'){if(++sessionCalls===1)throw new TypeError('offline');return session('ok');}
    assert.match(path,/^\/api\/mobile\/history/);historyCalls++;
    return reply({agent:{id:'fixture',name:'Fixture Agent',provider:'codex'},runs:[],next:null});
  });
  location.pathname='/mobile/agent';location.search='?agent=fixture';
  const page=await import('./public/office/agent.js?session-recovery-test');await settle();
  assert.equal(f.intervals.size,0);assert.match(f.doc.getElementById('connection').textContent,/重試/);
  await f.tick();await page.agentPageReady;await settle();
  assert.equal(historyCalls,1);assert.equal(f.intervals.size,1);assert.equal(f.doc.getElementById('name').textContent,'Fixture Agent');
  f.doc.visibilityState='hidden';f.doc.dispatchEvent({type:'visibilitychange'});assert.equal(f.intervals.size,0);
  f.doc.visibilityState='visible';f.doc.dispatchEvent({type:'visibilitychange'});await settle();assert.equal(f.intervals.size,1);assert.equal(historyCalls,2);
  f.events.dispatchEvent({type:'pagehide',persisted:true});assert.equal(f.intervals.size,0);
  f.events.dispatchEvent({type:'pageshow',persisted:true});await settle();assert.equal(f.intervals.size,1);assert.equal(historyCalls,3);
  access.lockMobileAccess();assert.equal(f.intervals.size,0);assert.equal(f.doc.cleared,true);
});

test('3D startup runs exactly once after session recovery and never after a terminal denial',async t=>{
  let calls=0,loads=0;
  const f=fixture(t,async()=>{if(++calls===1)throw new TypeError('offline');return session('ok');});
  const doc=document;globalThis.document=undefined;
  const entry=await import('./public/office/mobile-entry.js?session-recovery-test');globalThis.document=doc;
  const ready=entry.startMobileOffice({doc,load:async()=>{loads++;}});await settle();
  await f.tick();await ready;assert.equal(loads,1);
  f.events.dispatchEvent({type:'online'});f.doc.dispatchEvent({type:'visibilitychange'});await settle();assert.equal(loads,1);
  access.clearMobileSession();globalThis.fetch=async()=>reply({error:'mobile_forbidden'},403);
  await entry.startMobileOffice({doc,load:async()=>{loads++;}});assert.equal(loads,1);assert.match(doc.getElementById('loading').textContent,/未獲准/);
});

for(const [reason,error,expected] of [
  ['session',Object.assign(new Error('private-detail'),{status:400}),/手機登入狀態/],
  ['module',new TypeError('Failed to fetch dynamically imported module: https://private.invalid/sensitive-path'),/載入辦公室的畫面檔案/],
  ['WebGL',new Error('Error creating WebGL context.'),/啟動 3D 顯示/],
  ['unknown',new TypeError('private-detail'),/畫面未能啟動/],
])test(`mobile startup exposes a safe manual retry for ${reason} failure`,async t=>{
  let requests=0,reloads=0,loads=0;
  const f=fixture(t,async()=>{requests++;return reason==='session'?reply({error:'invalid_request'},error.status):session('ok');});
  const doc=f.doc,panel=doc.createElement('div'),label=doc.getElementById('loading');
  label.parentElement=panel;
  doc.createElement=()=>target();
  location.reload=()=>{reloads++;};
  globalThis.document=undefined;
  const entry=await import('./public/office/mobile-entry.js?manual-retry');
  globalThis.document=doc;
  await entry.startMobileOffice({doc,load:async()=>{loads++;throw error;}});
  assert.match(label.textContent,expected);assert.doesNotMatch(label.textContent,/private|sensitive-path/);
  assert.equal(panel.children.length,1);assert.equal(panel.children[0].type,'button');
  assert.equal(reloads,0);assert.equal(f.timeouts.size,0);assert.equal(loads,reason==='session'?0:1);
  panel.children[0].dispatchEvent({type:'click'});assert.equal(reloads,1);assert.equal(requests,reason==='module'?2:1);
});

test('the private asset cooldown reloads once per failed startup and resets after success',async t=>{
  const storage=new Map();
  const previous=Object.getOwnPropertyDescriptor(globalThis,'sessionStorage');
  Object.defineProperty(globalThis,'sessionStorage',{configurable:true,value:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)}});
  t.after(()=>{if(previous)Object.defineProperty(globalThis,'sessionStorage',previous);else delete globalThis.sessionStorage;});
  const paths=[];
  const f=fixture(t,async(path,options)=>{
    paths.push(path);
    if(path==='/api/mobile/session')return session('fixture-secret');
    assert.equal(path,'/office/mobile-entry.js');assert.equal(options.cache,'no-store');
    return {status:429,headers:{get:key=>key==='Retry-After'?'7':null}};
  });
  const doc=f.doc,panel=doc.createElement('div'),label=doc.getElementById('loading');label.parentElement=panel;
  let reloads=0;location.reload=()=>{reloads++;};doc.createElement=()=>target();
  globalThis.document=undefined;
  const entry=await import('./public/office/mobile-entry.js?cooldown');globalThis.document=doc;
  const failed=()=>entry.startMobileOffice({doc,load:async()=>{throw new TypeError('Failed to fetch dynamically imported module: private-host');}});
  await failed();assert.match(label.textContent,/7 秒後會自動/);assert.doesNotMatch(label.textContent,/private-host|fixture-secret/);
  assert.deepEqual([...storage.values()],['1']);assert.equal(panel.children.at(-1).disabled,true);
  assert.deepEqual([...f.timeouts.values()].map(task=>task.ms),[7000]);
  assert.equal(reloads,0);await f.tick();assert.equal(reloads,1);
  await failed();assert.match(label.textContent,/等候約 7 秒/);await f.tick();
  assert.equal(reloads,1);assert.equal(panel.children.at(-1).disabled,false);assert.equal(f.timeouts.size,0);
  await entry.startMobileOffice({doc,load:async()=>{}});
  assert.equal(storage.size,0);assert.deepEqual(paths,['/api/mobile/session','/office/mobile-entry.js','/office/mobile-entry.js']);
  await failed();access.clearMobileSession();await f.tick();assert.equal(reloads,1);
});

test('logout during a rejected 3D import cannot show stale retry controls',async t=>{
  const f=fixture(t,async()=>session('valid'));
  const doc=f.doc;globalThis.document=undefined;
  const entry=await import('./public/office/mobile-entry.js?after-logout');globalThis.document=doc;
  await entry.startMobileOffice({doc,load:async()=>{access.lockMobileAccess();throw new TypeError('Failed to fetch dynamically imported module');}});
  assert.equal(doc.cleared,true);assert.equal(location.replaced,'/mobile/pair');
});

test('a ready callback clearing authorization cannot resolve session startup',async t=>{
  const f=fixture(t,async()=>session('ok'));
  await assert.rejects(access.waitForMobileSession({onStatus:state=>{if(state==='ready')access.clearMobileSession();}}),/mobile_session_changed/);
  assert.equal(f.timeouts.size,0);assert.equal(f.events.count(),0);
});

for(const action of ['clear','lock'])test(`${action} queued at ready status prevents 3D initialization`,async t=>{
  let loads=0,updates=0;const f=fixture(t,async()=>session('ok'));
  const label=f.doc.getElementById('loading');
  Object.defineProperty(label,'textContent',{set(){if(++updates===2)queueMicrotask(()=>action==='lock'?access.lockMobileAccess():access.clearMobileSession());}});
  const doc=document;globalThis.document=undefined;
  const entry=await import('./public/office/mobile-entry.js?ready-guard-'+action);globalThis.document=doc;
  await entry.startMobileOffice({doc,load:async()=>{loads++;}});
  assert.equal(loads,0);assert.equal(f.doc.cleared,action==='lock');assert.equal(f.timeouts.size,0);
});

for(const action of ['clear','lock'])test(`${action} queued at ready status prevents history initialization`,async t=>{
  let calls=0,updates=0;const f=fixture(t,async path=>{calls++;assert.equal(path,'/api/mobile/session');return session('ok');});
  location.pathname='/mobile/agent';location.search='?agent=fixture';
  const label=f.doc.getElementById('connection');
  Object.defineProperty(label,'textContent',{set(){if(++updates===2)queueMicrotask(()=>action==='lock'?access.lockMobileAccess():access.clearMobileSession());}});
  const page=await import('./public/office/agent.js?ready-guard-'+action);await page.agentPageReady;await settle();
  assert.equal(calls,1);assert.equal(f.intervals.size,0);assert.equal(f.doc.cleared,action==='lock');
});

test('desktop history retains owner authentication and starts without a mobile session request',async t=>{
  const calls=[];
  const f=fixture(t,async(path,options)=>{calls.push({path,options});return reply({agent:{id:'fixture',name:'Desktop Agent',provider:'codex'},runs:[],next:null});});
  f.doc.querySelector=()=>null;location.pathname='/office/agent.html';location.search='?agent=fixture';location.hash='#fixture-owner-key';
  const page=await import('./public/office/agent.js?desktop-recovery-test');await page.agentPageReady;await settle();
  assert.equal(calls.length,1);assert.match(calls[0].path,/^\/api\/office\/history/);
  assert.equal(calls[0].options.headers['X-Office-Key'],'fixture-owner-key');assert.equal(f.intervals.size,1);
  assert.equal(f.doc.getElementById('back').href,'/office/#fixture-owner-key');assert.equal(f.doc.getElementById('name').textContent,'Desktop Agent');
});

const needsRenewal=()=>reply({error:'mobile_session_renewal_required'},401);
test('expired remembered session renews automatically without clearing UI or another pairing',async t=>{
  const calls=[];const f=fixture(t,async(path,options)=>{calls.push({path,options});return path==='/api/mobile/session'?needsRenewal():session('renewed');});
  const value=await access.ensureMobileSession();assert.equal(value.csrf_token,'renewed');assert.equal(f.doc.cleared,false);assert.equal(location.replaced,undefined);
  assert.deepEqual(calls.map(c=>c.path),['/api/mobile/session','/api/mobile/renew']);
  assert.equal(calls[1].options.method,'POST');assert.equal(calls[1].options.body,'{}');assert.equal(calls[1].options.headers['X-Mobile-Renew'],'1');
});
test('concurrent stale safe reads share one renewal and retry their reads only',async t=>{
  const renew=deferred();let renewed=false,renewCalls=0,reads=0;
  fixture(t,async path=>{if(path==='/api/mobile/renew'){renewCalls++;return renew.promise;}reads++;return renewed?reply({ok:true}):needsRenewal();});
  const one=access.mobileRequest('/api/mobile/work'),two=access.mobileRequest('/api/mobile/messages');await settle();assert.equal(renewCalls,1);
  renewed=true;renew.resolve(session('fresh'));assert.deepEqual(await Promise.all([one,two]),[{ok:true},{ok:true}]);assert.equal(reads,4);
});
test('command preflight renews expired access before sending the command once',async t=>{
  const calls=[];fixture(t,async(path,options)=>{calls.push({path,method:options.method||'GET'});
    if(path==='/api/mobile/session')return needsRenewal();if(path==='/api/mobile/renew')return session('fresh');return reply({command:{id:'once'}});});
  const result=await access.mobileRequest('/api/mobile/commands',{method:'POST',body:{text:'fixture'}});
  assert.equal(result.command.id,'once');assert.deepEqual(calls.map(c=>c.path),['/api/mobile/session','/api/mobile/renew','/api/mobile/commands']);
});
test('command raced by session expiry is never replayed after renewal',async t=>{
  const calls=[];fixture(t,async(path,options)=>{calls.push(path);if(path==='/api/mobile/session')return session('old');if(path==='/api/mobile/renew')return session('fresh');return needsRenewal();});
  await assert.rejects(access.mobileRequest('/api/mobile/commands',{method:'POST',body:{text:'fixture'}}),e=>e.code==='mobile_session_renewed'&&e.status===409);
  assert.equal(calls.filter(x=>x==='/api/mobile/commands').length,1);assert.equal((await access.ensureMobileSession()).csrf_token,'fresh');
});
test('revocation wins during renewal and private data is never retried',async t=>{
  const calls=[];const f=fixture(t,async path=>{calls.push(path);return path==='/api/mobile/renew'?reply({error:'mobile_auth_required'},401):needsRenewal();});
  await assert.rejects(access.mobileRequest('/api/mobile/work'),e=>e.status===401);assert.deepEqual(calls,['/api/mobile/work','/api/mobile/renew']);assert.equal(f.doc.cleared,true);
});
test('late renewal cannot resurrect a session after clear',async t=>{
  const renewal=deferred();fixture(t,path=>path==='/api/mobile/session'?Promise.resolve(needsRenewal()):renewal.promise);
  const old=access.ensureMobileSession(),rejected=assert.rejects(old,/mobile_session_changed/);await settle();access.clearMobileSession();renewal.resolve(session('old'));await rejected;
  globalThis.fetch=async()=>session('new');assert.equal((await access.ensureMobileSession()).csrf_token,'new');
});
test('explicit remember refreshes authentication, has CSRF, and discards older private responses',async t=>{
  const old=deferred(),calls=[];fixture(t,async(path,options)=>{calls.push({path,options});
    if(path==='/api/mobile/work')return old.promise;if(path==='/api/mobile/session')return session('before');return session('remembered');});
  const read=access.mobileRequest('/api/mobile/work'),discarded=assert.rejects(read,/mobile_session_changed/);
  const remembered=await access.rememberMobileDevice(true);assert.equal(remembered.csrf_token,'remembered');
  const post=calls.find(x=>x.path==='/api/mobile/remember');assert.equal(post.options.headers['X-Mobile-CSRF'],'before');assert.equal(post.options.body,'{"remember_device":true}');
  old.resolve(reply({private:'old'}));await discarded;
});
test('logout refreshes current session and uses CSRF even after remembered state changes in another tab',async t=>{
  let sessionCalls=0;const calls=[];const f=fixture(t,async(path,options)=>{calls.push({path,options});
    if(path==='/api/mobile/session')return reply({authenticated:true,csrf_token:'csrf',device:{remembered:++sessionCalls===1}});return reply({revoked:true});});
  await access.ensureMobileSession();await access.logoutMobileDevice();assert.equal(sessionCalls,2);
  const logout=calls.find(x=>x.path==='/api/mobile/logout');assert.equal(logout.options.headers['X-Mobile-CSRF'],'csrf');assert.equal(logout.options.headers['X-Mobile-Renew'],undefined);assert.equal(f.doc.cleared,true);
});
