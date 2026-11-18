import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {createHash,randomUUID} from 'node:crypto';
import {operate,boundedFetch} from './grok-sdk/worker.mjs';
import {encodeConnectFrame} from './vendor/grok-bot-sdk/connect-stream.mjs';

const fingerprint=createHash('sha256').update('account-A').digest('hex');
async function fixture(t){const dir=await fs.mkdtemp(path.join(os.tmpdir(),'office-grok-sdk-'));const root=path.join(dir,'grok-sdk');t.after(()=>fs.rm(dir,{recursive:true,force:true}));return root;}
async function account(root,identity='account-A'){
  await fs.mkdir(root,{recursive:true,mode:0o700});
  await fs.writeFile(path.join(root,'account.json'),JSON.stringify({accessToken:'PRIVATE-ACCESS-TOKEN',refreshToken:'PRIVATE-REFRESH-TOKEN',account_fingerprint:createHash('sha256').update(identity).digest('hex'),expires_at_ms:null}),{mode:0o600});
}
const json=(value,status=200)=>new Response(JSON.stringify(value),{status,headers:{'content-type':'application/json'}});
const noNetwork=()=>{throw new Error('Unexpected network request');};

test('fresh login is idempotent and all returned values omit credentials',async t=>{
  const root=await fixture(t),id=randomUUID();
  const first=await operate({root,action:'login_start',request_id:id},{fetchImpl:noNetwork});
  const second=await operate({root,action:'login_start',request_id:id},{fetchImpl:noNetwork});
  assert.deepEqual(first,second);assert.equal(first.login_id,id);assert.equal(first.state,'pending');
  const url=new URL(first.login_url);assert.equal(url.origin,'https://cursor.com');assert.equal(url.pathname,'/loginDeepControl');assert.equal(url.searchParams.get('mode'),'login');assert.equal(url.searchParams.has('verifier'),false);
  const secret=JSON.parse(await fs.readFile(path.join(root,'login-'+id+'.json')));assert.ok(secret.verifier);
  assert.equal(JSON.stringify(first).includes(secret.verifier),false);
  if(process.platform!=='win32')assert.equal((await fs.stat(path.join(root,'login-'+id+'.json'))).mode&0o777,0o600);
});

test('login-status missing receipt is read-only and returns not_found',async t=>{
  const root=await fixture(t),id=randomUUID();
  assert.deepEqual(await operate({root,action:'login_status',login_id:id},{fetchImpl:noNetwork}),{login_id:id,state:'not_found'});
  assert.deepEqual(await fs.readdir(root),[]);
});

test('one pending login poll makes one request; complete login stores private tokens only',async t=>{
  const root=await fixture(t),id=randomUUID();await operate({root,action:'login_start',request_id:id});
  let calls=0;
  const pending=await operate({root,action:'login_poll',login_id:id},{fetchImpl:async()=>{calls++;return json({},404);}});
  assert.equal(calls,1);assert.equal(pending.state,'pending');
  const done=await operate({root,action:'login_poll',login_id:id},{fetchImpl:async(url)=>{
    calls++;return new URL(url).pathname==='/auth/poll'?json({accessToken:'PRIVATE-ACCESS-TOKEN',refreshToken:'PRIVATE-REFRESH-TOKEN'}):json({authId:'account-A',email:'do-not-return@example.test'});
  }});
  assert.equal(done.state,'authenticated');assert.equal(calls,3);assert.doesNotMatch(JSON.stringify(done),/PRIVATE|example\.test/);
  const saved=JSON.parse(await fs.readFile(path.join(root,'account.json')));assert.equal(saved.account_fingerprint,fingerprint);
  const flow=JSON.parse(await fs.readFile(path.join(root,'login-'+id+'.json')));assert.equal(flow.verifier,undefined);assert.equal(flow.candidate,undefined);
  const state=await operate({root,action:'auth_status'},{fetchImpl:noNetwork});assert.equal(state.state,'authenticated');assert.equal(state.account_fingerprint,fingerprint);assert.doesNotMatch(JSON.stringify(state),/PRIVATE|example\.test/);
});

test('completed authorization resumes account verification instead of redoing auth poll',async t=>{
  const root=await fixture(t),id=randomUUID();await operate({root,action:'login_start',request_id:id});
  let polls=0;
  await assert.rejects(operate({root,action:'login_poll',login_id:id},{fetchImpl:async(url)=>{
    if(new URL(url).pathname==='/auth/poll'){polls++;return json({accessToken:'PRIVATE-ACCESS-TOKEN',refreshToken:'PRIVATE-REFRESH-TOKEN'});}return json({},503);
  }}));
  const done=await operate({root,action:'login_poll',login_id:id},{fetchImpl:async(url)=>{assert.notEqual(new URL(url).pathname,'/auth/poll');return json({authId:'account-A'});}});
  assert.equal(polls,1);assert.equal(done.state,'authenticated');
});

test('different account cannot replace existing credential or routes',async t=>{
  const root=await fixture(t),id=randomUUID();await account(root);const before=await fs.readFile(path.join(root,'account.json'),'utf8');
  await operate({root,action:'login_start',request_id:id});
  const result=await operate({root,action:'login_poll',login_id:id},{fetchImpl:async(url)=>new URL(url).pathname==='/auth/poll'?json({accessToken:'OTHER-PRIVATE-TOKEN',refreshToken:'OTHER-REFRESH'}):json({authId:'account-B'})});
  assert.equal(result.reason,'grok_sdk_account_mismatch');assert.equal(await fs.readFile(path.join(root,'account.json'),'utf8'),before);
  const flow=JSON.parse(await fs.readFile(path.join(root,'login-'+id+'.json')));assert.equal(flow.candidate,undefined);assert.equal(flow.verifier,undefined);
});

test('cancel purges candidate credentials and cannot restart same request',async t=>{
  const root=await fixture(t),id=randomUUID();await operate({root,action:'login_start',request_id:id});
  const file=path.join(root,'login-'+id+'.json'),flow=JSON.parse(await fs.readFile(file));flow.candidate={accessToken:'PRIVATE'};await fs.writeFile(file,JSON.stringify(flow),{mode:0o600});
  assert.equal((await operate({root,action:'login_cancel',login_id:id})).state,'cancelled');
  const saved=JSON.parse(await fs.readFile(file));assert.equal(saved.candidate,undefined);assert.equal(saved.verifier,undefined);
  assert.equal((await operate({root,action:'login_start',request_id:id})).state,'cancelled');
});

test('send uses exact IDs and explicit main-session empty string, no auto retry',async t=>{
  const root=await fixture(t);await account(root);const id=randomUUID();let calls=0;
  const result=await operate({root,action:'send_message',bot_id:'bot-1',session_id:'',message_id:id,text:'[OFFICE:'+id+']\nHello',client_version:'1.0.1'},{fetchImpl:async(url,options)=>{
    calls++;assert.match(new URL(url).pathname,/SendGrokBotUserMessage$/);const body=JSON.parse(options.body);
    assert.equal(body.agentId,'bot-1');assert.equal(body.sessionId,'');assert.equal(body.messageId,id);assert.equal(body.isFork,false);assert.equal(options.headers['x-cursor-client-version'],'1.0.1');
    return json({delivery:1,dispatched:true});
  }});
  assert.equal(result.messageId,id);assert.equal(calls,1);
  await assert.rejects(operate({root,action:'send_message',bot_id:'bot-1',message_id:id,text:'Hello'},{fetchImpl:noNetwork}),/TARGET_REQUIRED/);
  calls=0;await assert.rejects(operate({root,action:'send_message',bot_id:'bot-1',session_id:'',message_id:id,text:'Hello'},{fetchImpl:async()=>{calls++;throw new Error('network secret');}}),/SEND_UNCONFIRMED/);assert.equal(calls,1);
});

test('management operations are not in the worker allowlist',async t=>{
  const root=await fixture(t);await account(root);
  for(const action of ['createBot','deleteBot','updateBot','duplicateBot'])await assert.rejects(operate({root,action},{fetchImpl:noNetwork}),/ACTION_NOT_ALLOWED/);
});

test('native status preserves exact allowlisted settlement, never arbitrary fields',async t=>{
  const root=await fixture(t);await account(root);
  const result=await operate({root,action:'agent_statuses',bot_ids:['bot-1']},{fetchImpl:async()=>json({statuses:[{agentId:'bot-1',live:{agentId:'bot-1',sessionId:'',isRunning:false,lastTurnSettlement:{turnId:'turn-1',clientNonce:'input-1',outcome:'success',settledAtMs:'12345',privateReasoning:'NEVER'}}}]})});
  assert.deepEqual(result.statuses[0].live.lastTurnSettlement,{turnId:'turn-1',clientNonce:'input-1',outcome:'success',settledAtMs:'12345'});
  assert.doesNotMatch(JSON.stringify(result),/NEVER/);
});

test('watch remains scoped, preserves native settlement and does not fabricate completion',async t=>{
  const root=await fixture(t);await account(root);const emitted=[];
  const body=Buffer.concat([encodeConnectFrame({agentState:{snapshot:true,live:[{agentId:'other',sessionId:'',lastTurnSettlement:{turnId:'wrong',outcome:'success',settledAtMs:'1'}},{agentId:'bot-1',sessionId:'',lastTurnSettlement:{turnId:'turn-1',clientNonce:'input-1',outcome:'success',settledAtMs:'123'}}]}}),Buffer.from([2,0,0,0,2,123,125])]);
  await operate({root,action:'watch',bot_id:'bot-1',session_id:''},{emit:x=>emitted.push(x),fetchImpl:async(url)=>new URL(url).pathname.endsWith('ListGrokBotTranscriptEntries')?json({entries:[],generation:1}):new Response(body,{headers:{'content-type':'application/connect+json'}})});
  assert.equal(emitted.length,1);assert.equal(emitted[0].event.type,'agent.state');assert.equal(emitted[0].event.data.live.length,1);assert.equal(emitted[0].event.data.live[0].lastTurnSettlement.clientNonce,'input-1');
});

test('401/403 cannot remain authenticated-ready; rejected endpoint never receives token',async t=>{
  const root=await fixture(t);await account(root);
  await assert.rejects(operate({root,action:'list_bots'},{fetchImpl:async()=>json({},403)}));
  assert.equal((await operate({root,action:'auth_status'})).state,'unavailable');
  await assert.rejects(operate({root,action:'list_bots'},{fetchImpl:async()=>json({},401)}));
  assert.equal((await operate({root,action:'auth_status'})).state,'expired');
  await assert.rejects(boundedFetch('https://example.test/steal',{headers:{authorization:'secret'}}),/ENDPOINT_NOT_ALLOWED/);
});
