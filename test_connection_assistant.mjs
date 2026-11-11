import test from 'node:test';
import assert from 'node:assert/strict';
import {
  GrokConnectionController,GrokConnectionAssistant,grokConnectionView,grokProofVerified,
  grokAssistantEnabled,pairingDetailsReady,normalizePairingKey,
} from './public/office/grok-connection.js';

const AGENT='grok_bot:fixture-selected';
const OTHER='grok_bot:fixture-other';
const TEST='00000000-0000-4000-8000-000000000101';
const OLD_TEST='00000000-0000-4000-8000-000000000099';
const NEXT_TEST='00000000-0000-4000-8000-000000000102';
const ROUTE_A='fixture-route-revision-A';
const ROUTE_B='fixture-route-revision-B';
const DETAILS={url:'https://example.invalid/fixture/webhook',key:'fixture-pairing-value'};
const capability=(persistent=false,extra={})=>({can_send:true,native_ready:persistent,persistent,mode:persistent?'grok_webhook':'agent_inbox',...extra});
const target=(agent=AGENT,cap=capability())=>({provider:'grok_bot',agent_id:agent,state:'connected',conversation:cap});
const snapshot=(cap=capability(true),check={state:'ready_to_test',can_test:true})=>({capability:cap,check});
const queued=(id=TEST)=>snapshot(capability(true),{test_id:id,state:'queued',can_test:false,verified:false,received:false,replied:false});
const proof=(id=TEST)=>({test_id:id,state:'verified',verified:true,received:true,replied:true,can_test:true});
const routeCheck=(revision,check={state:'ready_to_test',can_test:true})=>({...check,route_revision:revision});
const routeCapability=(revision,check={state:'ready_to_test',can_test:true})=>capability(true,{connection_check:routeCheck(revision,check)});
const routeSnapshot=(revision,check={state:'ready_to_test',can_test:true})=>snapshot(routeCapability(revision,check),routeCheck(revision,check));
const turn=()=>new Promise(resolve=>setImmediate(resolve));
function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};}

function fixture(overrides={}){
  const calls=[],server=new Map();
  const transport={
    async read(agent){calls.push(['read',agent]);return overrides.read?overrides.read(agent,server.get(agent)):server.get(agent);},
    async save(agent,details){calls.push(['save',agent]);const result=overrides.save?await overrides.save(agent,details):{connected:true};server.set(agent,snapshot());return result;},
    async start(agent,id){calls.push(['start',agent,id]);return overrides.start?overrides.start(agent,id):queued(id);},
  };
  let ids=0;
  const controller=new GrokConnectionController({transport,makeId:()=>{ids++;return TEST;}});
  const select=selected=>{
    server.set(selected.agent_id,snapshot(selected.conversation,selected.conversation?.connection_check||{state:selected.conversation?.persistent?'ready_to_test':'unconfigured',can_test:!!selected.conversation?.persistent}));
    controller.bind(selected);
  };
  return {calls,controller,ids:()=>ids,select,async open(selected){select(selected);controller.setVisible(true);await turn();calls.length=0;}};
}

test('only an actual received-and-replied check produces connected status, and offline proof stays historical',()=>{
  const ready=capability(true);
  assert.equal(grokConnectionView(ready).state,'ready_to_test');
  assert.equal(grokProofVerified(ready),false);
  for(const incomplete of [
    {state:'queued',test_id:TEST},
    {state:'waiting_reply',test_id:TEST,received:true,replied:false,verified:false},
    {...proof(),received:false}, {...proof(),replied:false}, {...proof(),verified:false},
  ]){
    assert.equal(grokProofVerified(ready,incomplete),false);
    assert.notEqual(grokConnectionView(ready,incomplete).title,'已連接');
  }
  assert.equal(grokProofVerified(ready,proof()),true);
  assert.equal(grokConnectionView(ready,proof()).state,'verified');
  assert.equal(grokConnectionView(capability(true,{native_ready:false}),proof()).state,'waiting_platform');
  assert.equal(grokConnectionView(capability(true,{can_send:false}),proof()).state,'waiting_platform');
  assert.equal(grokConnectionView(ready,proof(),{uncertain:true}).state,'unconfirmed');
  assert.equal(grokConnectionView(ready,proof(),{saveUncertain:true}).state,'unconfirmed');
  assert.equal(grokConnectionView(capability(true,{reason:'dispatch_disabled'}),proof()).state,'disabled');
});

test('pairing accepts manual HTTPS data while keeping copied bearer formatting out of the saved value',()=>{
  assert.equal(pairingDetailsReady(DETAILS),true);
  assert.equal(pairingDetailsReady({...DETAILS,url:'http://example.invalid/webhook'}),false);
  assert.equal(pairingDetailsReady({...DETAILS,url:'https://user:password@example.invalid/webhook'}),false);
  assert.equal(pairingDetailsReady({...DETAILS,key:'  '}),false);
  assert.equal(normalizePairingKey(' Authorization: Bearer fixture-pairing-value '),DETAILS.key);
});

test('a public test reply waits for normal terminal confirmation instead of claiming connection',()=>{
  const partial={state:'waiting_reply',test_id:TEST,received:true,replied:true,verified:false,can_test:false};
  const view=grokConnectionView(capability(true),partial);
  assert.equal(view.title,'已收到回覆 · 正在確認完成');
  assert.equal(view.state,'waiting_reply');
  assert.equal(grokProofVerified(capability(true),partial),false);
  assert.ok(view.message.includes('正常完成回報'));
});

test('offline backend contract preserves historical verification without claiming a live connection or a busy Agent',()=>{
  const offline=capability(true,{native_ready:false,reason:'native_unavailable'});
  const historical={test_id:OLD_TEST,state:'unconfirmed',verified:false,reason:'native_unavailable',verified_at:1794312000.125,received:true,replied:true,can_test:false};
  assert.equal(grokConnectionView(offline,historical).state,'waiting_platform');
  assert.equal(grokConnectionView(offline,historical).title,'已驗證 · 等待平台恢復');
  assert.equal(grokConnectionView(offline,{...historical,verified_at:'2026-11-10T12:00:00Z'}).title,'已驗證 · 等待平台恢復');
  for(const verified_at of [null,'','not-a-timestamp',0,-1,NaN,Infinity])assert.equal(grokConnectionView(offline,{...historical,verified_at}).title,'入口已設定 · 等待平台恢復');
  const untested={state:'ready_to_test',can_test:false,reason:'native_unavailable'};
  const view=grokConnectionView(offline,untested);
  assert.equal(view.state,'waiting_platform');
  assert.equal(view.title,'入口已設定 · 等待平台恢復');
  assert.ok(!view.message.includes('工作仍未結束'));
});

function clipboardFixture(pending){
  const instance=Object.create(GrokConnectionAssistant.prototype),focus=[],renders=[];
  const field=name=>({value:'',focus:()=>focus.push(name)});
  Object.assign(instance,{identity:AGENT,generation:1,details:{url:'',key:''},notice:{textContent:''},
    controller:{epoch:1,visible:true,busy:false,target:target(),capability:capability()},
    inputs:{url:[field('url')],key:[field('key')]},clipboard:()=>pending.promise,render:()=>renders.push(true)});
  return {instance,focus,renders};
}
function changeClipboardTarget(instance,change){
  if(change==='disabled'){
    instance.controller.epoch++;
    instance.controller.capability=capability(false,{can_send:false,reason:'dispatch_disabled'});
    instance.controller.target=target(AGENT,instance.controller.capability);
  }else{
    instance.controller.capability=capability(true);
    instance.controller.target=target(AGENT,instance.controller.capability);
    instance.clearDetails();
  }
}

test('a delayed clipboard value cannot refill fields after the same Agent is disabled or its route is saved',async()=>{
  for(const change of ['disabled','saved']){
    const pending=deferred(),{instance,focus,renders}=clipboardFixture(pending);
    const active=instance.paste('key');changeClipboardTarget(instance,change);
    pending.resolve('fixture-pairing-value');await active;
    assert.deepEqual(instance.details,{url:'',key:''});assert.equal(instance.inputs.key[0].value,'');
    assert.deepEqual(focus,[]);assert.deepEqual(renders,[]);assert.equal(instance.notice.textContent,'');
  }
});

test('a rejected clipboard read cannot focus stale fields after same-Agent permission or route changes',async()=>{
  for(const change of ['disabled','saved']){
    const pending=deferred(),{instance,focus,renders}=clipboardFixture(pending);
    const active=instance.paste('key');changeClipboardTarget(instance,change);
    pending.reject(new Error('fixture clipboard unavailable'));await active;
    assert.deepEqual(instance.details,{url:'',key:''});assert.deepEqual(focus,[]);
    assert.deepEqual(renders,[]);assert.equal(instance.notice.textContent,'');
  }
});

test('explicit connect saves once, reads permission to test, then submits one caller UUID',async()=>{
  const f=fixture({save:async(agent,details)=>{
    assert.equal(agent,AGENT);assert.deepEqual(details,DETAILS);
  }});
  await f.open(target());
  assert.deepEqual(f.calls,[]);
  assert.equal(await f.controller.connectAndTest(DETAILS),true);
  assert.deepEqual(f.calls,[['save',AGENT],['read',AGENT],['start',AGENT,TEST]]);
  assert.equal(f.ids(),1);
  assert.equal(f.controller.check.test_id,TEST);
  assert.equal(f.controller.canStart(DETAILS),false);
  assert.notEqual(grokConnectionView(f.controller.capability,f.controller.check).state,'verified');
  await f.controller.connectAndTest(DETAILS);
  assert.equal(f.calls.filter(row=>row[0]==='start').length,1);
});

test('saved routes skip credential transmission and busy routes do not automatically launch a test',async()=>{
  const saved=capability(true,{connection_check:{state:'ready_to_test',can_test:true}});
  const f=fixture();await f.open(target(AGENT,saved));
  assert.equal(await f.controller.connectAndTest({}),true);
  assert.deepEqual(f.calls,[['start',AGENT,TEST]]);
  const busy=fixture({read:async(agent,current)=>current.capability.persistent?snapshot(capability(true),{state:'ready_to_test',can_test:false}):current});
  await busy.open(target());
  assert.equal(await busy.controller.connectAndTest(DETAILS),false);
  assert.deepEqual(busy.calls,[['save',AGENT],['read',AGENT]]);
  assert.equal(busy.ids(),0);
  assert.equal(busy.controller.canStart(DETAILS),false);
});

test('unknown test delivery cannot be cleared by an older check or silently create a new test',async()=>{
  let starts=0;
  const f=fixture({read:async()=>snapshot(capability(true),proof(OLD_TEST)),start:async(agent,id)=>{
    starts++;if(starts===1)throw new Error('fixture uncertain response');return queued(id);
  }});
  await f.open(target(AGENT,capability(true,{connection_check:{state:'ready_to_test',can_test:true}})));
  assert.equal(await f.controller.connectAndTest({}),false);
  assert.equal(f.controller.pendingId,TEST);assert.equal(f.controller.uncertain,true);
  assert.equal(f.controller.check.test_id,OLD_TEST);
  assert.equal(grokConnectionView(f.controller.capability,f.controller.check,{uncertain:f.controller.uncertain}).state,'unconfirmed');
  await f.controller.connectAndTest({});
  assert.equal(starts,1);assert.equal(f.ids(),1);
  const before=f.calls.length;f.controller.setVisible(false);f.controller.setVisible(true);await turn();
  assert.deepEqual(f.calls.slice(before),[['read',AGENT]]);
  assert.equal(f.controller.uncertain,true);
  await f.controller.resolve();
  assert.deepEqual(f.calls.filter(row=>row[0]==='start'),[['start',AGENT,TEST],['start',AGENT,TEST]]);
  assert.equal(f.ids(),1);assert.equal(f.controller.uncertain,false);
});

test('unknown credential save recovers with GET only and requires an explicit test before showing green',async()=>{
  let configured=false;
  const f=fixture({save:async()=>{configured=true;throw new Error('fixture lost save receipt');},read:async()=>configured?snapshot(capability(true),proof(OLD_TEST)):snapshot(capability(),{state:'unconfigured',can_test:false})});
  await f.open(target());
  assert.equal(await f.controller.connectAndTest(DETAILS),false);
  assert.deepEqual(f.calls,[['save',AGENT],['read',AGENT]]);
  assert.equal(f.controller.saveUncertain,true);
  assert.equal(grokConnectionView(f.controller.capability,f.controller.check,{saveUncertain:f.controller.saveUncertain}).state,'unconfirmed');
  f.controller.setVisible(false);f.controller.setVisible(true);await turn();
  assert.equal(f.calls.filter(row=>row[0]==='save').length,1);
  assert.equal(f.calls.filter(row=>row[0]==='start').length,0);
  assert.equal(await f.controller.connectAndTest({}),true);
  assert.equal(f.calls.filter(row=>row[0]==='save').length,1);
  assert.deepEqual(f.calls.filter(row=>row[0]==='start'),[['start',AGENT,TEST]]);
  assert.equal(f.controller.saveUncertain,false);
});

test('closing and reopening a controller restores persisted proof with read-only requests',async()=>{
  const f=fixture({read:async()=>snapshot(capability(true),proof())});
  f.select(target(AGENT,capability(true)));
  f.controller.setVisible(true);await turn();
  f.controller.setVisible(false);await f.controller.refresh();
  assert.deepEqual(f.calls,[['read',AGENT]]);
  f.controller.setVisible(true);await turn();
  assert.deepEqual(f.calls,[['read',AGENT],['read',AGENT]]);
  assert.equal(grokConnectionView(f.controller.capability,f.controller.check).state,'verified');
});

test('closing during a credential save prevents later testing until the user reopens and acts',async()=>{
  const pending=deferred(),f=fixture({save:async()=>pending.promise});
  await f.open(target());const active=f.controller.connectAndTest(DETAILS);
  f.controller.setVisible(false);pending.resolve({connected:true});
  assert.equal(await active,false);
  assert.deepEqual(f.calls,[['save',AGENT]]);
  assert.equal(f.ids(),0);
  assert.equal(await f.controller.connectAndTest(DETAILS),false);
  f.controller.setVisible(true);await turn();
  assert.deepEqual(f.calls,[['save',AGENT],['read',AGENT]]);
  assert.equal(await f.controller.connectAndTest({}),true);
  assert.deepEqual(f.calls,[['save',AGENT],['read',AGENT],['start',AGENT,TEST]]);
});

test('authoritative same-Agent capability invalidates old proof while active test identity stays fenced',async()=>{
  const completed=fixture();
  await completed.open(target(AGENT,capability(true,{connection_check:proof(OLD_TEST)})));
  assert.equal(grokConnectionView(completed.controller.capability,completed.controller.check).state,'verified');
  completed.select(target(AGENT,capability(false,{connection_check:{state:'unconfigured',can_test:false}})));
  assert.equal(grokConnectionView(completed.controller.capability,completed.controller.check).state,'unconfigured');
  assert.equal(completed.controller.check?.test_id,undefined);
  const active=fixture();
  await active.open(target(AGENT,capability(true,{connection_check:{state:'ready_to_test',can_test:true}})));
  await active.controller.connectAndTest({});
  active.select(target(AGENT,capability(true,{connection_check:proof(OLD_TEST)})));
  assert.equal(active.controller.pendingId,TEST);
  assert.equal(active.controller.check.test_id,TEST);
  assert.notEqual(grokConnectionView(active.controller.capability,active.controller.check).state,'verified');
  assert.equal(active.calls.filter(row=>row[0]==='start').length,1);
});

test('resolved matching tests release their fence so later route invalidation can replace cached results',async()=>{
  for(const result of [
    proof(),
    {test_id:TEST,state:'unconfirmed',verified:false,verified_at:'2026-11-10T12:00:00Z',received:true,replied:true,reason:'native_unavailable'},
    {test_id:TEST,state:'failed',verified:false,received:false,replied:false,can_retry:true,can_test:true},
  ]){
    const f=fixture({start:async()=>snapshot(capability(true),result)});
    await f.open(target(AGENT,capability(true,{connection_check:{state:'ready_to_test',can_test:true}})));
    await f.controller.connectAndTest({});
    assert.equal(f.controller.pendingId,null);
    f.select(target(AGENT,capability(false,{connection_check:{state:'unconfigured',can_test:false}})));
    assert.equal(f.controller.check.state,'unconfigured');
    assert.equal(grokConnectionView(f.controller.capability,f.controller.check).state,'unconfigured');
  }
});

test('an unknown save with no confirmed route remains blocked instead of transmitting the credentials again',async()=>{
  const f=fixture({save:async()=>{throw new Error('fixture unknown save');}});
  await f.open(target());
  assert.equal(await f.controller.connectAndTest(DETAILS),false);
  assert.equal(f.controller.saveUncertain,true);
  assert.equal(f.controller.capability.persistent,false);
  assert.equal(f.controller.canStart(DETAILS),false);
  await f.controller.connectAndTest(DETAILS);
  assert.equal(f.calls.filter(row=>row[0]==='save').length,1);
  assert.equal(f.calls.filter(row=>row[0]==='start').length,0);
  assert.equal(grokConnectionView(f.controller.capability,f.controller.check,{saveUncertain:true}).state,'unconfirmed');
});

test('a delayed credential save cannot continue into a test after selection changes',async()=>{
  const pending=deferred();
  const f=fixture({save:async()=>pending.promise});
  await f.open(target());const active=f.controller.connectAndTest(DETAILS);
  f.select(target(OTHER,capability(true)));await turn();
  pending.resolve({connected:true});assert.equal(await active,false);
  assert.deepEqual(f.calls,[['save',AGENT],['read',OTHER]]);
  assert.equal(f.controller.target.agent_id,OTHER);
  assert.equal(f.controller.capability.persistent,true);
  assert.equal(f.controller.pendingId,null);
});

test('a delayed read after saving cannot apply old proof or continue POST on a new selection',async()=>{
  const pending=deferred();let waiting=false;
  const f=fixture({read:async(agent,current)=>waiting&&agent===AGENT?pending.promise:current});
  await f.open(target());waiting=true;const active=f.controller.connectAndTest(DETAILS);await turn();
  assert.deepEqual(f.calls,[['save',AGENT],['read',AGENT]]);
  f.select(target(OTHER,capability()));await turn();
  pending.resolve(snapshot(capability(true),proof()));assert.equal(await active,false);
  assert.equal(f.controller.target.agent_id,OTHER);
  assert.equal(f.controller.capability.persistent,false);
  assert.equal(f.controller.check?.test_id,undefined);
  assert.equal(f.calls.filter(row=>row[0]==='start').length,0);
});

test('disabled or revoked selections cannot save, test, resolve or continue an in-flight setup',async()=>{
  for(const selected of [
    target(AGENT,capability(true,{can_send:false,reason:'dispatch_disabled'})),
    {...target(),state:'revoked'},
    {...target(),provider:'cursor'},
  ]){
    const f=fixture();await f.open(selected);
    assert.equal(grokAssistantEnabled(selected),false);
    assert.equal(f.controller.canStart(DETAILS),false);
    assert.equal(await f.controller.connectAndTest(DETAILS),false);
    await f.controller.resolve();assert.deepEqual(f.calls,[]);
  }
  const pending=deferred(),f=fixture({save:async()=>pending.promise});
  await f.open(target());const active=f.controller.connectAndTest(DETAILS);
  f.select(target(AGENT,capability(false,{can_send:false,reason:'dispatch_disabled'})));
  pending.resolve();assert.equal(await active,false);
  assert.deepEqual(f.calls,[['save',AGENT]]);
});

test('a same-Agent route change fences an old pending GET before its verified reply arrives',async()=>{
  const pending=deferred();let delayed=false;
  const f=fixture({read:async(agent,current)=>delayed&&current.check?.route_revision===ROUTE_A?pending.promise:current});
  await f.open(target(AGENT,routeCapability(ROUTE_A)));
  delayed=true;const active=f.controller.refresh();
  f.select(target(AGENT,routeCapability(ROUTE_B)));await turn();
  pending.resolve(routeSnapshot(ROUTE_A,proof()));await active;
  assert.equal(f.controller.capability.connection_check.route_revision,ROUTE_B);
  assert.equal(f.controller.check.route_revision,ROUTE_B);
  assert.notEqual(grokConnectionView(f.controller.capability,f.controller.check).state,'verified');
  assert.equal(f.calls.filter(row=>row[0]==='start'||row[0]==='save').length,0);
});

test('a same-Agent route change rejects an old POST receipt rather than blessing the replacement route',async()=>{
  const pending=deferred(),f=fixture({start:async()=>pending.promise});
  await f.open(target(AGENT,routeCapability(ROUTE_A)));
  const active=f.controller.connectAndTest({});
  f.select(target(AGENT,routeCapability(ROUTE_B)));await turn();
  pending.resolve(routeSnapshot(ROUTE_A,proof()));await active;
  assert.equal(f.controller.capability.connection_check.route_revision,ROUTE_B);
  assert.equal(f.controller.check.route_revision,ROUTE_B);
  assert.notEqual(grokConnectionView(f.controller.capability,f.controller.check).state,'verified');
  assert.deepEqual(f.calls.filter(row=>row[0]==='start'),[['start',AGENT,TEST]]);
});

test('a retired route token cannot be restored by a later stale parent capability',async()=>{
  const f=fixture();await f.open(target(AGENT,routeCapability(ROUTE_A)));
  f.select(target(AGENT,routeCapability(ROUTE_B)));await turn();
  f.controller.bind(target(AGENT,routeCapability(ROUTE_A,proof(OLD_TEST))));
  assert.equal(f.controller.capability.connection_check.route_revision,ROUTE_B);
  assert.equal(f.controller.check.route_revision,ROUTE_B);
  assert.notEqual(grokConnectionView(f.controller.capability,f.controller.check).state,'verified');
  assert.equal(f.calls.filter(row=>row[0]==='start'||row[0]==='save').length,0);
});

test('same-Agent authorization loss stays revoked across stale active binds and resets only for another Agent identity',async()=>{
  for(const status of [403,404]){
    let rejected=false;
    const f=fixture({read:async(agent,current)=>{
      if(rejected&&agent===AGENT){const error=new Error('fixture revoked membership');error.status=status;throw error;}
      return current;
    }});
    await f.open(target(AGENT,routeCapability(ROUTE_A)));rejected=true;
    await f.controller.refresh();
    f.controller.bind(target(AGENT,routeCapability(ROUTE_A,proof(OLD_TEST))));
    assert.equal(f.controller.capability.can_send,false);
    assert.equal(f.controller.capability.reason,'agent_not_selected');
    assert.equal(grokConnectionView(f.controller.capability,f.controller.check).state,'disabled');
    assert.equal(f.controller.canStart({}),false);
    assert.equal(await f.controller.connectAndTest({}),false);
    f.select(target(OTHER,routeCapability(ROUTE_B)));await turn();
    assert.equal(f.controller.target.agent_id,OTHER);
    assert.equal(f.controller.capability.can_send,true);
    assert.equal(f.controller.canStart({}),true);
    assert.equal(f.calls.filter(row=>row[0]==='start'||row[0]==='save').length,0);
  }
});

test('an older parent proof on the current route cannot replace its active pending test',async()=>{
  const f=fixture({start:async(agent,id)=>routeSnapshot(ROUTE_A,{test_id:id,state:'queued',can_test:false,verified:false,received:false,replied:false})});
  await f.open(target(AGENT,routeCapability(ROUTE_A)));
  await f.controller.connectAndTest({});
  f.controller.bind(target(AGENT,routeCapability(ROUTE_A,proof(OLD_TEST))));
  assert.equal(f.controller.check.test_id,TEST);
  assert.equal(f.controller.check.route_revision,ROUTE_A);
  assert.notEqual(grokConnectionView(f.controller.capability,f.controller.check).state,'verified');
  assert.equal(f.controller.canStart({}),false);
  await f.controller.connectAndTest({});
  assert.deepEqual(f.calls.filter(row=>row[0]==='start'),[['start',AGENT,TEST]]);
});

test('reconciling an old UUID receipt displays the newer authoritative test on that same route',async()=>{
  let starts=0;
  const later={test_id:NEXT_TEST,state:'queued',can_test:false,verified:false,received:false,replied:false};
  const f=fixture({start:async()=>{
    starts++;if(starts===1)throw new Error('fixture lost first POST receipt');
    return {capability:routeCapability(ROUTE_A,later),check:routeCheck(ROUTE_A,proof(TEST))};
  }});
  await f.open(target(AGENT,routeCapability(ROUTE_A)));
  assert.equal(await f.controller.connectAndTest({}),false);
  await f.controller.resolve();
  assert.deepEqual(f.calls.filter(row=>row[0]==='start'),[['start',AGENT,TEST],['start',AGENT,TEST]]);
  assert.equal(f.ids(),1);
  assert.equal(f.controller.check.test_id,NEXT_TEST);
  assert.equal(f.controller.capability.connection_check.test_id,NEXT_TEST);
  assert.equal(grokConnectionView(f.controller.capability,f.controller.check).state,'queued');
  assert.equal(f.controller.canStart({}),false);
});

test('an explicitly requested first connection may adopt its newly saved route token and submit one test',async()=>{
  let configured=false;
  const f=fixture({save:async()=>{configured=true;},read:async(agent,current)=>configured?routeSnapshot(ROUTE_B):current,
    start:async(agent,id)=>routeSnapshot(ROUTE_B,{test_id:id,state:'queued',can_test:false,verified:false,received:false,replied:false})});
  await f.open(target(AGENT,capability(false,{connection_check:routeCheck(null,{state:'unconfigured',can_test:false})})));
  assert.equal(await f.controller.connectAndTest(DETAILS),true);
  assert.deepEqual(f.calls,[['save',AGENT],['read',AGENT],['start',AGENT,TEST]]);
  assert.equal(f.controller.capability.connection_check.route_revision,ROUTE_B);
  assert.equal(f.controller.check.route_revision,ROUTE_B);
  assert.equal(f.controller.check.test_id,TEST);
  assert.equal(f.ids(),1);
});

test('same-route newer test authority fences an old GET and permanently rejects its retired test proof',async()=>{
  const pending=deferred();let delayNext=false,delayed=false;
  const f=fixture({read:async(agent,current)=>{
    if(delayNext&&!delayed){delayed=true;return pending.promise;}
    return current;
  }});
  await f.open(target(AGENT,routeCapability(ROUTE_A,proof(OLD_TEST))));
  delayNext=true;const oldRead=f.controller.refresh();
  const later={test_id:NEXT_TEST,state:'queued',can_test:false,verified:false,received:false,replied:false};
  f.select(target(AGENT,routeCapability(ROUTE_A,later)));
  assert.notEqual(grokConnectionView(f.controller.capability,f.controller.check).state,'verified');
  await turn();
  assert.equal(f.controller.check.test_id,NEXT_TEST);
  pending.resolve(routeSnapshot(ROUTE_A,proof(OLD_TEST)));await oldRead;
  assert.equal(f.controller.check.test_id,NEXT_TEST);
  assert.equal(grokConnectionView(f.controller.capability,f.controller.check).state,'queued');
  f.controller.bind(target(AGENT,routeCapability(ROUTE_A,proof(OLD_TEST))));
  assert.equal(f.controller.check.test_id,NEXT_TEST);
  assert.equal(grokConnectionView(f.controller.capability,f.controller.check).state,'queued');
  assert.equal(f.calls.filter(row=>row[0]==='read').length,2);
  assert.equal(f.calls.filter(row=>row[0]==='start'||row[0]==='save').length,0);
});

test('an unfamiliar same-route parent proof stays unverified until a fresh GET confirms the actual current test',async()=>{
  const pending=deferred();let waiting=false;
  const later={test_id:NEXT_TEST,state:'queued',can_test:false,verified:false,received:false,replied:false};
  const f=fixture({read:async(agent,current)=>waiting?pending.promise:current});
  await f.open(target(AGENT,routeCapability(ROUTE_A,later)));
  waiting=true;f.controller.bind(target(AGENT,routeCapability(ROUTE_A,proof(OLD_TEST))));
  assert.notEqual(grokConnectionView(f.controller.capability,f.controller.check).state,'verified');
  assert.equal(f.controller.canStart({}),false);await turn();
  assert.notEqual(grokConnectionView(f.controller.capability,f.controller.check).state,'verified');
  pending.resolve(routeSnapshot(ROUTE_A,later));await turn();
  assert.equal(f.controller.check.test_id,NEXT_TEST);
  assert.equal(grokConnectionView(f.controller.capability,f.controller.check).state,'queued');
  f.controller.bind(target(AGENT,routeCapability(ROUTE_A,proof(OLD_TEST))));
  assert.equal(f.controller.check.test_id,NEXT_TEST);
  assert.equal(grokConnectionView(f.controller.capability,f.controller.check).state,'queued');
  assert.equal(f.calls.filter(row=>row[0]==='read').length,1);
  assert.equal(f.calls.filter(row=>row[0]==='start'||row[0]==='save').length,0);
});
