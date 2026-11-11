import test from 'node:test';
import assert from 'node:assert/strict';
import { requestIdentity,queueHint,receivingHint,receivingSetupLabel,receivingSetupControl } from './public/office/commands.js';
import { mergeMessages } from './public/office/scene/dialogue.js';
import { officePost } from './public/office/office-api.js';

test('invitation status separates membership, saved routes, live receivers and revoked invitations',async()=>{
  const prior=globalThis.location;
  globalThis.location={hash:''};
  try{
    const {invitationProgress}=await import('./public/office/invite.js');
    const joined={state:'connected',provider:'codex',conversation:{persistent:true,native_ready:true}};
    assert.equal(invitationProgress(joined),'已入場 · 持續收訊已接通');
    assert.equal(invitationProgress({...joined,conversation:{persistent:true,native_ready:false}}),'已入場 · 持續收訊已設定，等待原平台恢復');
    assert.equal(invitationProgress({...joined,conversation:{persistent:true}}),'已入場 · 持續收訊已設定，等待原平台恢復');
    assert.equal(invitationProgress({...joined,conversation:{persistent:true,native_ready:false,reason:'dispatch_disabled'}}),'已入場 · 派工已停用');
    assert.equal(invitationProgress({...joined,state:'revoked'}),'邀請已取消');
    assert.equal(invitationProgress({...joined,state:'expired'}),'邀請已過期');
    assert.equal(invitationProgress({...joined,state:'claimed'}),'已登記，等待 Agent 首次回報');
    const inbox={state:'connected',provider:'cursor',conversation:{mode:'agent_inbox',persistent:false,receiver_online:false}};
    assert.equal(invitationProgress(inbox),'已入場 · 本機背景收訊未接通，可先留言');
    assert.equal(invitationProgress({...inbox,conversation:{...inbox.conversation,receiver_online:true}}),'已入場 · 臨時收訊中，持續接駁待完成');
    const route={mode:'grok_webhook',persistent:true,native_ready:true,can_send:true};
    assert.equal(invitationProgress({state:'connected',provider:'grok_bot',conversation:route}),'已入場 · 入口已設定 · 尚未測試');
    assert.equal(receivingHint(route),'入口已設定 · 尚未測試');
    assert.equal(receivingSetupLabel(route),'測試連接');
    const verified={...route,connection_check:{state:'verified',verified:true,received:true,replied:true}};
    assert.equal(invitationProgress({state:'connected',provider:'grok_bot',conversation:verified}),'已入場 · 已連接');
    assert.equal(receivingHint({...verified,native_ready:false}),'已驗證 · 等待平台恢復');
  }finally{
    if(prior===undefined)delete globalThis.location;else globalThis.location=prior;
  }
});

test('Grok connection controls remain available after saving a route until a real live verification succeeds',()=>{
  const agent='grok_bot:fixture-control',route={mode:'grok_webhook',persistent:true,native_ready:true,can_send:true};
  assert.deepEqual(receivingSetupControl({mode:'agent_inbox',persistent:false},agent,true),{hidden:false,label:'完成 Grok 連接'});
  assert.deepEqual(receivingSetupControl(route,agent),{hidden:false,label:'測試連接'});
  for(const state of ['queued','waiting_reply'])assert.deepEqual(receivingSetupControl({...route,connection_check:{state,received:true,verified:false}},agent,true),{hidden:false,label:'查看測試進度'});
  const proof={state:'verified',received:true,replied:true,verified:true};
  assert.deepEqual(receivingSetupControl({...route,connection_check:proof},agent),{hidden:true,label:'查看連接狀態'});
  assert.deepEqual(receivingSetupControl({...route,native_ready:false,connection_check:proof},agent),{hidden:false,label:'查看連接狀態'});
  assert.deepEqual(receivingSetupControl({...route,connection_check:{state:'unconfirmed'}},agent),{hidden:false,label:'查看連接狀態'});
  const cursor={mode:'agent_inbox',reason:'cursor_local_background_unavailable'};
  assert.deepEqual(receivingSetupControl(cursor,'cursor:fixture'),{hidden:false,label:'查看收訊限制'});
  assert.deepEqual(receivingSetupControl(cursor,'cursor:fixture',true),{hidden:true,label:'查看收訊限制'});
});

test('Grok activation stays in the original invitation and stops asking after a binding is saved',async()=>{
  const prior=globalThis.location;globalThis.location={hash:''};
  try{
    const {needsGrokActivation}=await import('./public/office/invite.js');
    const selected={id:'fixture-invitation',agent_id:'grok_bot:fixture',provider:'grok_bot',state:'connected',conversation:{mode:'agent_inbox',persistent:false,can_send:true}};
    assert.equal(needsGrokActivation(selected),true);
    for(const state of ['pending','claimed','revoked','expired'])assert.equal(needsGrokActivation({...selected,state}),false);
    assert.equal(needsGrokActivation({...selected,conversation:{...selected.conversation,reason:'dispatch_disabled'}}),false);
    assert.equal(needsGrokActivation({...selected,conversation:{...selected.conversation,can_send:false}}),false);
    assert.equal(needsGrokActivation({...selected,conversation:undefined}),false);
    assert.equal(needsGrokActivation({...selected,agent_id:undefined}),false);
    for(const native_ready of [true,false])assert.equal(needsGrokActivation({...selected,conversation:{persistent:true,native_ready}}),false);
    assert.equal(needsGrokActivation({...selected,provider:'cursor'}),false);
    assert.equal(needsGrokActivation({...selected,setup:true}),true);
  }finally{if(prior===undefined)delete globalThis.location;else globalThis.location=prior;}
});

test('optional invitation resume accepts one existing id without guessing malformed parameters',async()=>{
  const prior=globalThis.location;globalThis.location={hash:''};
  try{
    const {invitationResumeId}=await import('./public/office/invite.js');
    const id='0123456789abcdef0123456789abcdef';
    assert.equal(invitationResumeId('?invitation='+id),id);
    assert.equal(invitationResumeId('?view=office&invitation='+id),id);
    for(const search of ['',undefined,null,'?invitation=','?agent='+id,'?invitation='+id.toUpperCase(),'?invitation='+id.slice(1),'?invitation='+id+'f','?invitation=%20'+id,'?invitation='+id+'&invitation='+id,'?invitation=../../private.json']){
      assert.equal(invitationResumeId(search),null);
    }
  }finally{if(prior===undefined)delete globalThis.location;else globalThis.location=prior;}
});

test('new invitations require a supported explicit platform and never infer Grok',async()=>{
  const prior=globalThis.location;globalThis.location={hash:''};
  try{
    const {invitationPlatform}=await import('./public/office/invite.js');
    for(const value of ['',undefined,null,'auto','Grok Bot','CODEX','hermes-unconfigured',{}])assert.equal(invitationPlatform(value),null);
    for(const value of ['codex','cursor','cursor_cloud','grok_bot','crouton','generic'])assert.equal(invitationPlatform(value),value);
  }finally{if(prior===undefined)delete globalThis.location;else globalThis.location=prior;}
});

test('an invitation GET cannot render an unseen stale route after the accepted controller scope changes',async()=>{
  const prior=globalThis.location;globalThis.location={hash:''};
  try{
    const {invitationViewGuard}=await import('./public/office/invite.js'),guard=invitationViewGuard();
    const view={open:true,invitation:'fixture-invitation',agent:'grok_bot:fixture',desiredInvitation:'fixture-invitation',desiredAgent:'grok_bot:fixture',epoch:1,revision:1};
    const ticket=guard.capture(view),renders=[];let finish;
    const pending=new Promise(resolve=>{finish=resolve;}).then(data=>{if(guard.accepts(ticket,view))renders.push(data);});
    view.epoch=2;view.revision=4;
    finish({conversation:{connection_check:{route_revision:'unseen-intermediate-route',verified:true,received:true,replied:true}}});
    await pending;assert.deepEqual(renders,[]);
    assert.equal(guard.accepts(guard.capture(view),view),true);
    const revisionOnly=guard.capture(view);view.revision++;
    assert.equal(guard.accepts(revisionOnly,view),false);
  }finally{if(prior===undefined)delete globalThis.location;else globalThis.location=prior;}
});

test('explicit Agent selection and close/reopen intents reject late setup and resume results',async()=>{
  const prior=globalThis.location;globalThis.location={hash:''};
  try{
    const {invitationViewGuard}=await import('./public/office/invite.js'),guard=invitationViewGuard();
    const view={open:true,invitation:null,agent:null,desiredInvitation:null,desiredAgent:'grok_bot:fixture-a',epoch:1,revision:1};
    guard.change();const agentA=guard.capture(view),renders=[];let finishA;
    const pending=new Promise(resolve=>{finishA=resolve;}).then(data=>{if(guard.accepts(agentA,view))renders.push(data);});
    guard.change();view.desiredAgent='grok_bot:fixture-b';finishA({agent_id:'grok_bot:fixture-a'});
    await pending;assert.deepEqual(renders,[]);
    const resume=guard.capture(view);guard.change();view.open=false;
    assert.equal(guard.accepts(resume,view),false);
    view.open=true;guard.change();
    assert.equal(guard.accepts(resume,view),false);
    assert.equal(guard.accepts(guard.capture(view),view),true);
  }finally{if(prior===undefined)delete globalThis.location;else globalThis.location=prior;}
});

test('activation autofocus happens once per invitation opening and never interrupts draft editing',async()=>{
  const prior=globalThis.location;globalThis.location={hash:''};
  try{
    const {activationFocusPolicy}=await import('./public/office/invite.js');
    const policy=activationFocusPolicy(),context={identity:'fixture-first',enabled:true,open:true,editing:false,urlEmpty:true,keyEmpty:true};
    assert.equal(policy.next({...context,enabled:false}),null);
    assert.equal(policy.next({...context,open:false}),null);
    assert.equal(policy.next(context),'url');
    assert.equal(policy.next(context),null);
    assert.equal(policy.next({...context,identity:'fixture-second',urlEmpty:false}),'key');
    const editing={...context,identity:'fixture-editing',editing:true};
    assert.equal(policy.next(editing),null);
    assert.equal(policy.next({...editing,editing:false}),null);
    const filled={...context,identity:'fixture-filled',urlEmpty:false,keyEmpty:false};
    assert.equal(policy.next(filled),null);
    assert.equal(policy.next({...filled,urlEmpty:true}),null);
    policy.reset();assert.equal(policy.next(context),'url');
  }finally{if(prior===undefined)delete globalThis.location;else globalThis.location=prior;}
});

test('unavailable Cursor local receiving explains mailbox storage without repeating setup instructions',()=>{
  const local={mode:'agent_inbox',persistent:false,receiver_online:false,reason:'cursor_local_background_unavailable'};
  assert.equal(receivingSetupLabel(local),'查看收訊限制');
  assert.equal(receivingHint(local),'只保存留言 · 無法自動喚醒原聊天');
  assert.equal(receivingHint(local,true),'只保存留言 · 無法自動喚醒原聊天');
  const grok={mode:'agent_inbox',persistent:false,receiver_online:false};
  assert.equal(receivingSetupLabel(grok),'啟用持續收訊');
  assert.equal(receivingHint(grok),'持續接駁待完成 · 可先留言');
  assert.equal(receivingHint({...grok,receiver_online:true}),'臨時收訊中 · 持續接駁待完成');
  assert.equal(receivingHint({mode:'cursor_cloud',persistent:true,native_ready:true}),'持續收訊已接通 · 無需待命');
});

test('lost response and reloaded composer retain retry identity without storing prompt',async()=>{
  const map=new Map(),storage={getItem:key=>map.get(key),setItem:(key,value)=>map.set(key,value)};
  const original=await requestIdentity('selected','private prompt',storage);
  assert.equal(await requestIdentity('selected','private prompt',storage),original);
  assert.ok(!JSON.stringify([...map]).includes('private prompt'));
  assert.notEqual(await requestIdentity('other','private prompt',storage),original);
  assert.notEqual(await requestIdentity('selected','a different instruction',storage),original);
});

test('a delayed queue shows a conditional manual hint, never assumes execution',()=>{
  const created_at=new Date(100000).toISOString();
  assert.equal(queueHint({state:'queued',created_at},110000),'');
  assert.match(queueHint({state:'queued',created_at},160000),/如 App 未載入/);
  for(const state of ['running','completed','failed'])assert.equal(queueHint({state,created_at},160000),'');
  assert.equal(queueHint({state:'queued',created_at},160000,'hermes_acp'),'');
});

test('native streaming revision replaces the existing bubble message',()=>{
  const rows=mergeMessages([{seq:1,event_id:'native-1',text:'First'}],[{seq:3,event_id:'native-1',text:'Final',phase:'final'}]);
  assert.equal(rows.length,1);assert.equal(rows[0].text,'Final');
  assert.equal(mergeMessages(rows,[{seq:1,event_id:'native-1',text:'Old page'}])[0].text,'Final');
});

test('composer can cancel pending work and answer approvals without posting outside owner endpoints',async(t)=>{
  const calls=[];
  t.mock.method(globalThis,'fetch',async(path,options)=>{calls.push({path,options});return {ok:true,json:async()=>({accepted:true})};});
  for(const path of ['/api/office/commands','/api/office/commands/cancel','/api/office/permissions']){
    assert.equal((await officePost(path,'fixture-owner-key',{agent_id:'selected'})).accepted,true);
  }
  for(const path of ['https://example.invalid/api/office/commands','/api/agents/inbox','/api/office/shutdown'])await assert.rejects(officePost(path,'fixture-owner-key',{}),/Invalid command endpoint/);
  assert.equal(calls.length,3);
  for(const {path,options} of calls){assert.equal(options.method,'POST');assert.equal(options.credentials,'same-origin');assert.equal(options.headers['X-Office-Key'],'fixture-owner-key');assert.ok(!path.includes('fixture-owner-key'));}
});
