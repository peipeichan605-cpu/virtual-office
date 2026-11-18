// Private subprocess boundary for the pinned, unofficial Grok Bot SDK.
// The only credential input is this installation's own account.json. Never read
// the desktop client's store, Keychain, environment tokens, or other accounts.
import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash, randomUUID} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {GrokBotClient, createLogin} from '../vendor/grok-bot-sdk/sdk.mjs';
import {requestJSON, API_ORIGIN} from '../vendor/grok-bot-sdk/core.mjs';

const UUID=/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
const KEY=/^[A-Za-z0-9_.-]{1,160}$/;
const LIMIT=8*1024*1024;
const fail=code=>Object.assign(new Error(code),{code});
const codeOf=e=>typeof e?.code==='string'&&/^[A-Z0-9_]{1,80}$/.test(e.code)?e.code:'WORKER_FAILED';
const publicLogin=flow=>({login_id:flow.login_id,state:flow.state,expires_at:flow.expires_at,
  ...(flow.state==='pending'?{login_url:flow.loginUrl}:{}),...(flow.reason?{reason:flow.reason}:{})});
function safeRoot(root){if(typeof root!=='string'||!path.isAbsolute(root)||path.basename(root)!=='grok-sdk')throw fail('PRIVATE_ROOT_INVALID');return root;}
async function prepare(root){
  try{const st=await fs.lstat(root);if(st.isSymbolicLink()||!st.isDirectory())throw fail('PRIVATE_ROOT_INVALID');}
  catch(e){if(e.code!=='ENOENT')throw e;await fs.mkdir(root,{mode:0o700,recursive:true});}
  await fs.chmod(root,0o700);
}
async function readPrivate(file,missing=false){
  let handle;
  try{
    const st=await fs.lstat(file);
    if(!st.isFile()||st.isSymbolicLink()||st.size>65536||(process.platform!=='win32'&&(st.mode&0o077)))throw fail('PRIVATE_FILE_INVALID');
    handle=await fs.open(file,'r');
    const current=await handle.stat();if(current.ino!==st.ino||current.size>65536)throw fail('PRIVATE_FILE_INVALID');
    const value=JSON.parse(await handle.readFile('utf8'));
    if(!value||Array.isArray(value)||typeof value!=='object')throw fail('PRIVATE_FILE_INVALID');
    return value;
  }catch(e){if(missing&&e.code==='ENOENT')return null;if(codeOf(e)!=='WORKER_FAILED')throw e;throw fail('PRIVATE_FILE_INVALID');}
  finally{await handle?.close();}
}
async function writePrivate(file,value){
  try{const st=await fs.lstat(file);if(st.isSymbolicLink()||!st.isFile())throw fail('PRIVATE_FILE_INVALID');}catch(e){if(e.code!=='ENOENT')throw e;}
  const temp=file+'.'+randomUUID()+'.tmp';
  try{await fs.writeFile(temp,JSON.stringify(value),{mode:0o600,flag:'wx'});await fs.rename(temp,file);}
  finally{await fs.unlink(temp).catch(()=>{});}
}
function tokenExpiry(token){
  try{const value=JSON.parse(Buffer.from(token.split('.')[1],'base64url').toString('utf8'));return typeof value.exp==='number'&&Number.isFinite(value.exp)?value.exp*1000:null;}catch{return null;}
}
function validateAccount(value){
  if(!value||typeof value.accessToken!=='string'||value.accessToken.length<8||value.accessToken.length>32768
    ||typeof value.account_fingerprint!=='string'||!/^[a-f0-9]{64}$/.test(value.account_fingerprint))throw fail('AUTH_REQUIRED');
  if(value.expires_at_ms&&value.expires_at_ms<=Date.now())throw fail('AUTH_EXPIRED');
  return value;
}
export async function boundedFetch(url,options={}){
  const u=new URL(url);
  if(u.origin!==API_ORIGIN||u.username||u.password||u.hash)throw fail('ENDPOINT_NOT_ALLOWED');
  const response=await fetch(url,{...options,redirect:'error'});
  // Connect framing has its own per-frame bound and must remain streaming.
  if(response.headers.get('content-type')?.split(';')[0].trim()==='application/connect+json')return response;
  const reader=response.body?.getReader();if(!reader)return response;
  const chunks=[];let bytes=0;
  try{for(;;){const {value,done}=await reader.read();if(done)break;bytes+=value.byteLength;if(bytes>LIMIT)throw fail('RESPONSE_TOO_LARGE');chunks.push(value);}}
  finally{await reader.cancel().catch(()=>{});reader.releaseLock();}
  return new Response(Buffer.concat(chunks),{status:response.status,statusText:response.statusText,headers:response.headers});
}
function client(account,fetchImpl,version){
  if(version!==undefined&&(typeof version!=='string'||!/^\d+\.\d+\.\d+(?:[-+.][A-Za-z0-9.-]+)?$/.test(version)))throw fail('CLIENT_VERSION_INVALID');
  return new GrokBotClient({accessToken:account.accessToken,...(version?{version}:{}),...(Number.isInteger(account.selectedTeamId)?{teamId:account.selectedTeamId}:{}),fetchImpl});
}
function target(input){if(!KEY.test(input.bot_id??'')||!Object.hasOwn(input,'session_id')||typeof input.session_id!=='string'||input.session_id.length>256||/[\x00-\x1f\x7f]/.test(input.session_id))throw fail('TARGET_REQUIRED');return {botId:input.bot_id,sessionId:input.session_id};}
async function authState(root,value){await writePrivate(path.join(root,'auth-state.json'),{...value,checked_at:new Date().toISOString()});}

export async function operate(input,{fetchImpl=boundedFetch,emit=()=>{}}={}){
  const root=safeRoot(input?.root);await prepare(root);
  const accountFile=path.join(root,'account.json'),stateFile=path.join(root,'auth-state.json');
  const flowFile=id=>{if(!UUID.test(id??''))throw fail('LOGIN_ID_INVALID');return path.join(root,'login-'+id+'.json');};
  const readFlow=async id=>{
    const file=flowFile(id),flow=await readPrivate(file);
    if(flow.login_id!==id)throw fail('LOGIN_ID_INVALID');
    if(flow.state==='pending'&&Date.parse(flow.expires_at)<=Date.now()){flow.state='expired';delete flow.verifier;delete flow.candidate;await writePrivate(file,flow);}
    return flow;
  };
  try{
    if(input.action==='auth_status'){
      const saved=await readPrivate(accountFile,true),state=await readPrivate(stateFile,true);
      const latest=await readPrivate(path.join(root,'latest-login.json'),true);
      let login=null;if(latest?.login_id){try{login=publicLogin(await readFlow(latest.login_id));}catch{}}
      if(!saved)return {state:'logged_out',reason:'grok_sdk_login_required',...(login?{latest_login:login}:{})};
      try{validateAccount(saved);}catch(e){return {state:'expired',reason:'grok_sdk_login_required',account_fingerprint:saved.account_fingerprint??null,...(login?{latest_login:login}:{})};}
      return {state:state?.state==='expired'?'expired':state?.state==='access_denied'?'unavailable':'authenticated',reason:state?.state==='expired'?'grok_sdk_login_required':state?.state==='access_denied'?'grok_sdk_access_denied':null,
        account_fingerprint:saved.account_fingerprint,...(login?{latest_login:login}:{})};
    }
    if(input.action==='login_start'){
      const file=flowFile(input.request_id),old=await readPrivate(file,true);
      if(old)return publicLogin(await readFlow(input.request_id));
      const created=createLogin(),flow={...created,login_id:input.request_id,state:'pending',expires_at:new Date(Date.now()+15*60*1000).toISOString()};
      // Root Python serializes starts; exclusive creation also preserves an
      // existing verifier if another process repeats this request UUID.
      try{await fs.writeFile(file,JSON.stringify(flow),{mode:0o600,flag:'wx'});}catch(e){if(e.code!=='EEXIST')throw e;return publicLogin(await readFlow(input.request_id));}
      await writePrivate(path.join(root,'latest-login.json'),{login_id:input.request_id});
      return publicLogin(flow);
    }
    if(input.action==='login_status'){
      try{return publicLogin(await readFlow(input.login_id));}catch(e){if(e.code==='ENOENT')return {login_id:input.login_id,state:'not_found'};throw e;}
    }
    if(input.action==='login_cancel'){
      const flow=await readFlow(input.login_id);
      if(flow.state==='pending'){flow.state='cancelled';delete flow.verifier;delete flow.candidate;await writePrivate(flowFile(input.login_id),flow);}
      return publicLogin(flow);
    }
    if(input.action==='login_poll'){
      const flow=await readFlow(input.login_id);
      if(flow.state!=='pending')return publicLogin(flow);
      if(flow.candidate)return await finishLogin(flow);
      const url=new URL('/auth/poll',API_ORIGIN);url.search=new URLSearchParams({uuid:flow.uuid,verifier:flow.verifier}).toString();
      const result=await requestJSON(url,{method:'GET',headers:{'content-type':'application/json'},fetchImpl});
      if(result.httpStatus===404)return publicLogin(flow);
      if(result.httpStatus===403){flow.state='failed';flow.reason='grok_sdk_login_refused';delete flow.verifier;await writePrivate(flowFile(input.login_id),flow);return publicLogin(flow);}
      if(!result.ok||typeof result.data?.accessToken!=='string'||typeof result.data?.refreshToken!=='string'
        ||result.data.accessToken.length<8||result.data.accessToken.length>32768||result.data.refreshToken.length>16384)throw fail('LOGIN_POLL_FAILED');
      // Retain the completed authorization locally if the account check has a
      // transient failure. No credential is returned over this stdio boundary.
      const candidate={accessToken:result.data.accessToken,refreshToken:result.data.refreshToken,
        ...(Number.isInteger(result.data.selectedTeamId)?{selectedTeamId:result.data.selectedTeamId}:{})};
      flow.candidate=candidate;await writePrivate(flowFile(input.login_id),flow);
      return await finishLogin(flow);
    }
    async function finishLogin(flow){
      const candidate=flow.candidate;
      const me=await client(candidate,fetchImpl,input.client_version).getMe();
      const identity=me.authId??me.userId;
      if(typeof identity!=='string'&&typeof identity!=='number')throw fail('ACCOUNT_ID_MISSING');
      const fingerprint=createHash('sha256').update(String(identity)).digest('hex');
      const old=await readPrivate(accountFile,true);
      if(old?.account_fingerprint&&old.account_fingerprint!==fingerprint){
        flow.state='failed';flow.reason='grok_sdk_account_mismatch';delete flow.candidate;delete flow.verifier;
        await writePrivate(flowFile(flow.login_id),flow);return publicLogin(flow);
      }
      await writePrivate(accountFile,{...candidate,account_fingerprint:fingerprint,expires_at_ms:tokenExpiry(candidate.accessToken),created_at:new Date().toISOString()});
      await authState(root,{state:'authenticated',account_fingerprint:fingerprint});
      flow.state='authenticated';delete flow.candidate;delete flow.verifier;await writePrivate(flowFile(flow.login_id),flow);
      return publicLogin(flow);
    }
    if(input.action==='login_finish'){
      const flow=await readFlow(input.login_id);if(flow.state!=='pending')return publicLogin(flow);
      if(!flow.candidate)throw fail('LOGIN_PENDING');return await finishLogin(flow);
    }
    const account=validateAccount(await readPrivate(accountFile,true)),sdk=client(account,fetchImpl,input.client_version);
    let result;
    if(input.action==='list_bots')result=await sdk.listBots({includeTeamAgents:false});
    else if(input.action==='list_sessions'){if(!KEY.test(input.bot_id??''))throw fail('TARGET_REQUIRED');result=await sdk.listSessions(input.bot_id);}
    else if(input.action==='agent_statuses'){
      if(!Array.isArray(input.bot_ids)||input.bot_ids.length<1||input.bot_ids.length>100||input.bot_ids.some(id=>!KEY.test(id)))throw fail('TARGET_REQUIRED');
      result=await sdk.listAgentStatuses(input.bot_ids);
    }else if(input.action==='list_transcript'){
      const t=target(input);if(!Number.isInteger(input.limit)||input.limit<1||input.limit>500)throw fail('READ_LIMIT_INVALID');
      result=await sdk.listTranscript({...t,limit:input.limit,...(input.before_seq!==undefined?{beforeSeq:input.before_seq}:{}),...(input.generation!==undefined?{generation:input.generation}:{})});
    }else if(input.action==='get_send_status')result=await sdk.getSendStatus({...target(input),messageId:input.message_id});
    else if(input.action==='list_tasks')result=await sdk.listTasks(target(input));
    else if(input.action==='list_agent_tasks'){
      if(!KEY.test(input.bot_id??''))throw fail('TARGET_REQUIRED');result=await sdk.listAgentTasks({botId:input.bot_id,includeRecentlyDone:input.include_recently_done===true});
    }else if(input.action==='list_subagents'){
      if(!KEY.test(input.bot_id??''))throw fail('TARGET_REQUIRED');result=await sdk.listSubagents(input.bot_id);
    }else if(input.action==='send_message'){
      if(!UUID.test(input.message_id??'')||typeof input.text!=='string'||!input.text.trim()||Buffer.byteLength(input.text)>24576)throw fail('MESSAGE_INVALID');
      result=await sdk.sendMessage({...target(input),messageId:input.message_id,text:input.text});
    }else if(input.action==='watch'){
      for await(const event of sdk.watchBot({...target(input),...(input.resume_from?{resumeFrom:input.resume_from}:{})}))emit({event});
      return {closed:true};
    }else throw fail('ACTION_NOT_ALLOWED');
    await authState(root,{state:'authenticated',account_fingerprint:account.account_fingerprint});
    return result;
  }catch(e){
    if(e?.httpStatus===401||['AUTH_REQUIRED','AUTH_EXPIRED','ACCOUNT_AUTH_FAILED'].includes(codeOf(e))){await authState(root,{state:'expired'}).catch(()=>{});}
    else if(e?.httpStatus===403){await authState(root,{state:'access_denied'}).catch(()=>{});}
    throw e;
  }
}

async function main(){
  let raw='';for await(const chunk of process.stdin){raw+=chunk;if(Buffer.byteLength(raw)>65536)throw fail('INPUT_TOO_LARGE');}
  let input;try{input=JSON.parse(raw);}catch{throw fail('INPUT_INVALID');}
  const emit=value=>process.stdout.write(JSON.stringify(value)+'\n');
  const result=await operate(input,{emit});emit({ok:true,result});
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
  main().catch(e=>{process.stdout.write(JSON.stringify({ok:false,error:codeOf(e),...(Number.isInteger(e?.httpStatus)?{http_status:e.httpStatus}:{})})+'\n');process.exitCode=1;});
}
