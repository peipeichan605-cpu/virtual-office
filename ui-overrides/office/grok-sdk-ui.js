import {officeGet,officePost} from './office-api.js';

const BASE='/api/office/grok/sdk';
const messages={
  private_office_required:'請從你的私人 OFFICE 入口開啟。',
  grok_sdk_runtime_unavailable:'這個 OFFICE 尚未備妥 Grok Bot SDK。請讓安裝 Agent 完成實驗模式設定，再重新檢查。',
  grok_sdk_runtime_required:'這個 OFFICE 尚未備妥 Grok Bot SDK。請讓安裝 Agent 完成實驗模式設定，再重新檢查。',
  grok_sdk_runtime_integrity:'SDK 程式未通過完整性檢查，請讓安裝 Agent 檢查安裝檔案。',
  grok_sdk_node_unavailable:'這個 OFFICE 尚未找到可用的 Node.js。請讓安裝 Agent 完成 SDK 設定，再重新檢查。',
  grok_sdk_login_required:'請登入你要接入的 Grok Bot 帳戶。',
  grok_sdk_auth_expired:'登入已過期，請重新登入；已有的 OFFICE 紀錄會保留。',
  grok_sdk_login_expired:'這次登入連結已過期，請重新開始登入。',
  grok_sdk_login_cancelled:'已取消這次登入。',
  grok_sdk_auth_failed:'Grok 未接受這次登入。請重新登入或核對帳戶的登入要求。',
  grok_sdk_login_policy:'帳戶的登入規則未允許這次連接。請依原平台要求處理。',
  grok_sdk_login_pending:'請在登入頁完成登入，再返回這裡。',
  grok_sdk_unavailable:'暫時連不上 Grok Bot，請稍後重新檢查。',
  grok_sdk_transport_unavailable:'暫時連不上 Grok Bot，請稍後重新檢查。',
  grok_sdk_login_unavailable:'暫時未能核對登入結果，請稍後重新檢查。',
  grok_sdk_access_denied:'目前帳戶未獲准使用這個接駁。請依原平台要求處理。',
  grok_sdk_account_changed:'登入帳戶已改變。請核對目前帳戶，再選擇要接入的 Bot。',
  grok_sdk_account_mismatch:'這次登入與已連接的帳戶不同。請使用原帳戶，已有接駁會保留。',
  grok_sdk_account_unverified:'暫時未能確認登入帳戶，請稍後重新檢查。',
  grok_sdk_invalid_response:'Grok Bot 回傳的資料未能核對，暫時不會接入。',
  grok_sdk_bot_not_found:'這個 Bot 目前不在可選清單中，請重新讀取清單。',
  grok_sdk_session_not_found:'這個聊天目前不可使用，請重新選擇。',
  grok_sdk_identity_conflict:'這個 Bot／聊天已有另一個 OFFICE 接駁，請先核對已登記的 Agent。',
  grok_sdk_session_already_selected:'這個 Bot／聊天已加入 OFFICE，請返回辦公室使用原有 Agent。',
  invalid_name:'請填寫 1 至 80 字的 Agent 名稱。',
  name_in_use:'這個名稱已有人使用，請換一個名稱。',
  request_id_conflict:'這次設定的資料未能核對。請先核對接入結果，不會自動重做。',
  agent_membership_changed:'這次接駁已登出或身份已更改。紀錄會保留，需要時可重新選擇。',
  browser_storage_unavailable:'瀏覽器未能保存設定編號。請允許此頁使用分頁儲存後再試。',
};
const uuid=value=>typeof value==='string'&&/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(value);
const identity=value=>typeof value==='string'&&/^[A-Za-z0-9_.-]{1,160}$/.test(value);
const publicName=value=>typeof value==='string'?value.replace(/[\u0000-\u001f\u007f]/g,'').slice(0,160):'';
export function grokSdkError(error){return messages[error?.code||error?.message]||'暫時未能完成 Grok Bot SDK 設定，請核對連線後再試。';}
export function grokSdkLoginUrl(value){
  try{const url=new URL(value);return url.protocol==='https:'&&url.hostname==='cursor.com'&&!url.port&&!url.username&&!url.password&&!url.hash&&url.pathname==='/loginDeepControl'?url.href:null;}
  catch{return null;}
}
export function grokSdkRequestStore(getStorage,scope){
  const prefix='office-grok-sdk:'+scope+':';
  return {
    load(kind){const value=getStorage().getItem(prefix+kind);return uuid(value)?value:null;},
    save(kind,id){if(!['login','connect'].includes(kind)||!uuid(id))throw new Error('invalid_request_identity');getStorage().setItem(prefix+kind,id);},
    clear(kind,id){const storage=getStorage();if(storage.getItem(prefix+kind)===id)storage.removeItem(prefix+kind);},
  };
}
function authState(data){
  const value=data?.auth?.state??data?.state;
  return ['logged_out','pending','authenticated','expired','unavailable'].includes(value)?value:'unavailable';
}
function loginReceipt(data,requestId){
  const row=data?.login||data?.latest_login||data;
  if(!row||!uuid(row.login_id)||row.login_id!==requestId||row.request_id&&row.request_id!==requestId)return null;
  const url=grokSdkLoginUrl(row.login_url);
  return url?{request_id:requestId,login_id:row.login_id,login_url:url,state:row.state||'pending'}:null;
}
function connectionReceipt(data,requestId,target){
  return data?.request_id===requestId&&data?.mode==='grok_sdk'&&data.connected===true&&data.same_session===true&&identity(data.bot_id)&&(data.session_id===''||identity(data.session_id))&&(!target||data.bot_id===target.bot_id&&data.session_id===target.session_id)&&typeof data.agent_id==='string'&&/^grok_bot:[A-Za-z0-9_.-]{1,160}$/.test(data.agent_id)
    ?{agent_id:data.agent_id,name:publicName(data.name)||'Grok Bot',request_id:requestId,mode:'grok_sdk',bot_id:data.bot_id,session_id:data.session_id}:null;
}
function botsFrom(data){
  if(!Array.isArray(data?.bots))throw new Error('grok_sdk_invalid_response');
  return [...new Map((Array.isArray(data?.bots)?data.bots:[]).filter(row=>identity(row?.id)).slice(0,500).map(row=>[row.id,{id:row.id,name:publicName(row.name)||row.id}])).values()];
}
function sessionsFrom(data,botId){
  if(data?.bot_id!==botId||!Array.isArray(data.sessions))return null;
  return [...new Map((Array.isArray(data.sessions)?data.sessions:[]).filter(row=>row?.session_id===''||identity(row?.session_id)).slice(0,500).map(row=>[row.session_id,{id:row.session_id,name:publicName(row.label||row.name||row.title||row.kind)||(row.session_id===''?'主要聊天':'聊天'),updated_at:publicName(row.updated_at||row.last_activity_at)}])).values()];
}
export function grokSdkSelectionHint(state,name){
  if(state.phase==='sessions_loading')return '正在讀取所選 Bot 的聊天，請稍候。';
  if(state.phase==='connecting')return '正在接入所選聊天，請稍候。';
  if(!state.botId)return '請先選擇一隻 Bot。';
  if(!state.sessions.length)return '這隻 Bot 暫時沒有可確認的聊天。請先在 Grok App 開啟它，再重新選擇。';
  const selected=state.sessions.find(row=>row.id===state.sessionId);
  if(!selected)return state.sessions.length>1?'這隻 Bot 有多個聊天，請選擇要接入的一個。':'請選擇要接入的聊天。';
  const selection=state.sessions.length===1?`已自動選擇這隻 Bot 唯一的聊天「${selected.name}」。`:`已選擇「${selected.name}」。`;
  if(!String(name||'').trim())return selection+'請填寫 OFFICE 名稱。';
  return selection+'按「接入所選聊天」完成設定。';
}

/** Read-only discovery follows an explicit platform choice. Writes retain durable UUIDs. */
export class GrokSdkController{
  constructor({transport,pendingStore,makeId=()=>crypto.randomUUID(),onChange=()=>{},onConnected=()=>{}}){
    Object.assign(this,{transport,pendingStore,makeId,onChange,onConnected});
    this.visible=false;this.epoch=0;this.revision=0;this.selectionRevision=0;this.phase='closed';this.error='';this.auth='logged_out';
    this.bots=[];this.sessions=[];this.botId='';this.sessionId=null;this.truncated=false;this.login=null;this.created=null;
    this.loginRequestId=null;this.connectRequestId=null;this.loginFlight=null;this.connectFlight=null;this.pollFlight=null;this.cancelFlight=null;this.notifiedId=null;
  }
  emit(){this.onChange(this);}
  current(epoch){return this.visible&&this.epoch===epoch;}
  clear(kind,id){try{this.pendingStore.clear(kind,id);}catch{/* Recover a completed receipt again after reload. */}}
  save(kind){const id=this.makeId();this.pendingStore.save(kind,id);return id;}
  async open(){
    this.visible=true;this.epoch++;this.error='';
    try{this.loginRequestId=this.pendingStore.load('login')||this.loginRequestId;this.connectRequestId=this.pendingStore.load('connect')||this.connectRequestId;}
    catch{this.phase='failed';this.error=messages.browser_storage_unavailable;this.emit();return;}
    await this.refresh();
  }
  close(){this.visible=false;this.epoch++;this.revision++;this.selectionRevision++;this.phase='closed';this.error='';this.emit();}
  complete(requestId,data,focus){
    this.revision++;
    this.clear('connect',requestId);this.connectRequestId=null;this.created=data;
    if(this.visible)this.phase='configured';
    if(this.notifiedId!==requestId){this.notifiedId=requestId;this.onConnected(data,{focus});}
  }
  async refresh(){
    if(!this.visible)return;
    const epoch=this.epoch,revision=++this.revision,current=()=>this.current(epoch)&&revision===this.revision;
    const requestId=this.connectRequestId,loginRequestId=this.loginRequestId;
    this.phase='loading';this.error='';this.emit();
    try{
      const data=await this.transport.read({requestId,loginRequestId});if(!current())return;
      this.auth=authState(data);
      if(requestId&&this.connectRequestId===requestId){
        const receipt=data.request;
        if(receipt?.request_id===requestId){
          const done=receipt.state==='completed'&&connectionReceipt(receipt.response,requestId);
          if(done){this.complete(requestId,done,true);this.emit();return;}
          if(['not_found','failed','revoked'].includes(receipt.state)&&!this.connectFlight){this.clear('connect',requestId);this.connectRequestId=null;if(receipt.state!=='not_found')this.error=grokSdkError({code:receipt.reason});}
        }
        if(this.connectRequestId){this.phase=this.connectFlight?'connecting':'connect_unknown';this.emit();return;}
      }
      if(this.auth==='authenticated'){
        if(this.loginRequestId)this.clear('login',this.loginRequestId);this.loginRequestId=null;this.login=null;
        await this.loadBots(current);return;
      }
      if(loginRequestId){
        const receipt=loginReceipt(data,loginRequestId);if(receipt)this.login=receipt;
        const ended=data.login?.request_id===loginRequestId&&['expired','cancelled','failed'].includes(data.login.state);
        const missing=data.login_request?.request_id===loginRequestId&&data.login_request.state==='not_found';
        if((missing||ended)&&!this.loginFlight){this.clear('login',loginRequestId);this.loginRequestId=null;this.login=null;if(ended)this.error=messages[data.login.state==='expired'?'grok_sdk_login_expired':data.login.state==='cancelled'?'grok_sdk_login_cancelled':'grok_sdk_auth_failed'];}
        else{this.phase=this.login?'login_pending':this.loginFlight?'login_starting':'login_unknown';this.emit();return;}
      }
      this.phase=this.auth==='unavailable'?'unavailable':'login_required';
      if(this.auth==='expired')this.error=messages.grok_sdk_auth_expired;
      else if(data.auth?.reason)this.error=grokSdkError({code:data.auth.reason});
    }catch(error){if(!current())return;this.phase=this.connectRequestId?'connect_unknown':this.loginRequestId?'login_unknown':'failed';this.error=grokSdkError(error);}
    if(current())this.emit();
  }
  async loadBots(current=()=>this.visible){
    try{
      const data=await this.transport.bots();if(!current())return;
      this.bots=botsFrom(data);this.truncated=data.truncated===true;
      if(!this.bots.some(bot=>bot.id===this.botId)){this.botId='';this.sessionId=null;this.sessions=[];}
      this.phase='selecting';
    }catch(error){if(!current())return;this.phase='failed';this.error=grokSdkError(error);}
    if(current())this.emit();
  }
  async startLogin(){
    if(!this.visible||this.loginRequestId||this.loginFlight||!['login_required','login_failed'].includes(this.phase))return false;
    try{this.loginRequestId=this.save('login');}catch{this.error=messages.browser_storage_unavailable;this.emit();return false;}
    const requestId=this.loginRequestId,epoch=this.epoch,operation={id:requestId};this.loginFlight=operation;this.revision++;this.phase='login_starting';this.error='';this.emit();
    try{
      const data=await this.transport.login({request_id:requestId}),receipt=loginReceipt(data,requestId);
      if(!receipt)throw Object.assign(new Error('invalid_login_receipt'),{status:502});
      if(this.loginRequestId!==requestId)return false;
      this.revision++;
      this.login=receipt;if(this.current(epoch))this.phase='login_pending';return true;
    }catch(error){
      if(this.loginRequestId!==requestId)return false;
      const uncertain=!error.status||error.status>=500||error.code==='request_id_conflict';
      if(!uncertain){this.clear('login',requestId);this.loginRequestId=null;}
      if(this.current(epoch)){this.phase=uncertain?'login_unknown':'login_failed';this.error=grokSdkError(error);}return false;
    }finally{if(this.loginFlight===operation)this.loginFlight=null;if(this.visible)this.emit();}
  }
  async pollLogin(){
    if(!this.visible||!this.login||this.pollFlight||this.cancelFlight||this.auth==='authenticated')return;
    const epoch=this.epoch,id=this.login.login_id,operation={id};this.pollFlight=operation;
    try{
      const data=await this.transport.poll(id);if(!this.current(epoch)||this.login?.login_id!==id||this.cancelFlight)return;
      const state=data.auth?.state==='authenticated'?'authenticated':data.state??data.auth?.state;
      if(state==='authenticated'){this.revision++;this.auth=state;this.clear('login',id);this.loginRequestId=null;this.login=null;this.phase='loading';this.error='';this.emit();await this.loadBots(()=>this.current(epoch));}
      else if(['expired','cancelled','failed','logged_out'].includes(state)){
        this.revision++;this.clear('login',id);this.loginRequestId=null;this.login=null;this.auth='logged_out';this.phase='login_required';this.error=messages[state==='expired'?'grok_sdk_login_expired':state==='cancelled'?'grok_sdk_login_cancelled':'grok_sdk_auth_failed'];this.emit();
      }else{this.phase='login_pending';this.error='';this.emit();}
    }catch(error){if(this.current(epoch)&&this.login?.login_id===id){this.error=grokSdkError(error);this.emit();}}
    finally{if(this.pollFlight===operation)this.pollFlight=null;if(this.current(epoch))this.emit();}
  }
  async cancelLogin(){
    if(!this.visible||!this.login||this.cancelFlight)return false;
    const epoch=this.epoch,id=this.login.login_id,operation={id};this.cancelFlight=operation;this.revision++;this.error='';this.emit();
    try{
      const data=await this.transport.cancel({login_id:id});
      if(this.login?.login_id!==id)return false;
      this.clear('login',id);this.loginRequestId=null;this.login=null;
      if((data.auth?.state??data.state)==='authenticated'){
        this.auth='authenticated';if(this.current(epoch)){this.phase='loading';await this.loadBots(()=>this.current(epoch));}return true;
      }
      if(this.current(epoch)){this.phase='login_required';this.auth='logged_out';this.error=messages.grok_sdk_login_cancelled;}return true;
    }catch(error){if(this.current(epoch))this.error=grokSdkError(error);return false;}
    finally{if(this.cancelFlight===operation)this.cancelFlight=null;if(this.visible)this.emit();}
  }
  async selectBot(id){
    if(!this.visible||this.auth!=='authenticated'||this.connectRequestId||this.connectFlight)return;
    const previousSession=this.botId===id?this.sessionId:null;
    const bot=this.bots.find(row=>row.id===id);this.botId=bot?.id||'';this.sessionId=null;this.sessions=[];this.error='';
    const epoch=this.epoch,revision=++this.selectionRevision;this.revision++;
    if(!bot){this.phase='selecting';this.emit();return;}
    this.phase='sessions_loading';this.emit();
    try{
      const data=await this.transport.sessions(id);if(!this.current(epoch)||revision!==this.selectionRevision||this.botId!==id)return;
      const sessions=sessionsFrom(data,id);if(!sessions)throw new Error('grok_sdk_invalid_response');
      this.sessions=sessions;
      this.sessionId=sessions.length===1?sessions[0].id:sessions.some(row=>row.id===previousSession)?previousSession:null;
      this.phase='selecting';
    }catch(error){if(!this.current(epoch)||revision!==this.selectionRevision)return;this.phase='selecting';this.error=grokSdkError(error);}
    this.emit();
  }
  selectSession(id){if(this.phase!=='selecting'||this.connectRequestId)return;this.sessionId=this.sessions.some(row=>row.id===id)?id:null;this.error='';this.emit();}
  async connect(name){
    if(!this.visible||this.phase!=='selecting'||this.auth!=='authenticated'||this.connectRequestId||this.connectFlight)return false;
    name=String(name||'').trim();
    if(!name||name.length>80||/[\u0000-\u001f\u007f]/.test(name)){this.error=messages.invalid_name;this.emit();return false;}
    if(!this.bots.some(row=>row.id===this.botId)||!this.sessions.some(row=>row.id===this.sessionId)){this.error='請先選擇一隻 Bot 及一個聊天。';this.emit();return false;}
    try{this.connectRequestId=this.save('connect');}catch{this.error=messages.browser_storage_unavailable;this.emit();return false;}
    const requestId=this.connectRequestId,epoch=this.epoch,operation={id:requestId},target={bot_id:this.botId,session_id:this.sessionId};this.connectFlight=operation;this.revision++;this.phase='connecting';this.error='';this.emit();
    try{
      const data=await this.transport.connect({request_id:requestId,...target,name}),receipt=connectionReceipt(data,requestId,target);
      if(!receipt)throw Object.assign(new Error('invalid_connection_receipt'),{status:502});
      this.complete(requestId,receipt,this.current(epoch));return true;
    }catch(error){
      if(this.notifiedId===requestId)return true;
      if(this.connectRequestId!==requestId)return false;
      const uncertain=!error.status||error.status>=500||error.code==='request_id_conflict';
      if(!uncertain){this.clear('connect',requestId);this.connectRequestId=null;}
      if(this.current(epoch)){this.phase=uncertain?'connect_unknown':'selecting';this.error=grokSdkError(error);}return false;
    }finally{if(this.connectFlight===operation)this.connectFlight=null;if(this.visible)this.emit();}
  }
}

export function initGrokSdkUI({document:doc=document,events=window,key=()=>decodeURIComponent(location.hash.slice(1)),pendingStore,openEvent='office:new-grok-sdk-conversation'}={}){
  pendingStore ||= grokSdkRequestStore(()=>sessionStorage,globalThis.location?location.origin+location.pathname.replace(/[^/]*$/,''):'office');
  const dialog=doc.createElement('dialog');dialog.className='office-invite-dialog grok-sdk-dialog';dialog.setAttribute('aria-labelledby','grok-sdk-heading');
  dialog.innerHTML=`<button class="invite-close" type="button" aria-label="關閉 Grok Bot SDK 設定">×</button>
    <div class="invite-eyebrow">VIRTUAL OFFICE / GROK BOT</div><h2 id="grok-sdk-heading">Grok Bot SDK · 實驗模式</h2>
    <p class="invite-intro">登入後，選擇你已有的 Bot 及聊天加入 OFFICE。</p>
    <p class="invite-footnote">此模式使用非官方 SDK；原平台更新可能影響接駁。登入授權涵蓋帳戶，比單一 Webhook 的範圍廣；OFFICE 只會同步及派工你選定的 Bot／聊天。登入資料保存在運行 OFFICE 的電腦，沿用原平台的批准規則。</p>
    <p class="grok-sdk-status" role="status" aria-live="polite"></p>
    <section class="grok-sdk-login" hidden><button class="invite-primary grok-sdk-login-start" type="button">開始登入 Grok Bot</button><p class="invite-footnote">由你在瀏覽器完成登入，毋須把密碼或驗證碼交給 Agent。</p></section>
    <section class="grok-sdk-login-pending" hidden><a class="invite-primary grok-sdk-login-link" target="_blank" rel="noopener noreferrer">開啟登入頁</a><p class="invite-footnote">登入頁可能顯示 Cursor，這是 Grok Bot 使用的帳戶登入流程。完成後返回此視窗，OFFICE 會接續顯示可選的 Bot。</p><button class="grok-sdk-login-check" type="button">我已登入，重新檢查</button><button class="grok-sdk-login-cancel" type="button">取消這次登入</button></section>
    <form class="invite-form" hidden><label>選擇現有 Bot<select name="bot" required><option value="">請選擇一隻 Bot</option></select></label>
      <label>選擇聊天<select name="session" required><option value="">先選擇 Bot</option></select></label>
      <label>OFFICE 名稱<input name="name" maxlength="80" required autocomplete="off" placeholder="例如：我的 Grok 助手"></label>
      <p class="grok-sdk-selection-note invite-footnote" role="status" aria-live="polite"></p>
      <p class="grok-sdk-roster-note invite-footnote"></p><p class="invite-footnote">只接入你選定的聊天，同步公開訊息及工作紀錄。</p>
      <button class="invite-primary" type="submit">接入所選聊天</button></form>
    <button class="grok-sdk-refresh" type="button" hidden>重新檢查</button>
    <section class="grok-sdk-result" hidden><p>接駁已設定，尚待雙向實測。請核對 OFFICE 發出的指示、Grok App 的直接對話及實際回覆，不能只以已加入作為收發成功。</p><button class="invite-primary grok-sdk-open-agent" type="button">返回 OFFICE</button></section>
    <p class="invite-notice" role="alert"></p>`;
  doc.body.append(dialog);
  const form=dialog.querySelector('form'),status=dialog.querySelector('.grok-sdk-status'),notice=dialog.querySelector('.invite-notice'),login=dialog.querySelector('.grok-sdk-login'),pending=dialog.querySelector('.grok-sdk-login-pending'),link=dialog.querySelector('.grok-sdk-login-link'),refresh=dialog.querySelector('.grok-sdk-refresh'),result=dialog.querySelector('.grok-sdk-result'),submit=form.querySelector('button[type="submit"]');
  let timer=null,botSignature='',sessionSignature='',suggestedName='';
  const dispatch=(name,detail)=>events.dispatchEvent(new CustomEvent(name,{detail}));
  const controller=new GrokSdkController({pendingStore,transport:{
    read:({requestId,loginRequestId})=>{const query=new URLSearchParams();if(requestId)query.set('request_id',requestId);if(loginRequestId)query.set('login_request_id',loginRequestId);return officeGet(BASE+'/status'+(query.size?'?'+query:''),key());},
    login:body=>officePost(BASE+'/login',key(),body),poll:id=>officeGet(BASE+'/login/status?'+new URLSearchParams({login_id:id}),key()),cancel:body=>officePost(BASE+'/login/cancel',key(),body),
    bots:()=>officeGet(BASE+'/bots',key()),sessions:id=>officeGet(BASE+'/sessions?'+new URLSearchParams({bot_id:id}),key()),connect:body=>officePost(BASE+'/connect',key(),body),
  },onChange:paint,onConnected:(agent)=>dispatch('office:refresh',agent)});
  function stop(){if(timer!==null)clearInterval(timer);timer=null;}
  function options(select,rows,placeholder){select.replaceChildren();const empty=doc.createElement('option');empty.value='';empty.textContent=placeholder;select.append(empty);for(const row of rows){const option=doc.createElement('option');option.value=row.id;option.textContent=row.name;select.append(option);}}
  function paint(state){
    const selecting=['selecting','sessions_loading','connecting'].includes(state.phase),ready=state.phase==='selecting';
    login.hidden=!['login_required','login_failed','login_starting'].includes(state.phase);pending.hidden=state.phase!=='login_pending';form.hidden=!selecting;result.hidden=state.phase!=='configured';
    refresh.hidden=!['failed','unavailable','login_unknown','connect_unknown','selecting'].includes(state.phase);refresh.textContent=state.phase==='connect_unknown'?'核對接入結果':state.phase==='login_unknown'?'核對登入結果':state.phase==='selecting'?'重新讀取清單':'重新檢查';
    status.textContent=({loading:'正在檢查 Grok Bot SDK…',login_required:'第一步：登入你的 Grok Bot 帳戶。',login_starting:'正在準備登入頁…',login_pending:'等待你完成登入。',login_unknown:'登入頁的準備結果未確認，請先核對結果。',login_failed:'未能準備登入頁。',selecting:state.bots.length?'第二步：選擇現有 Bot 及聊天。':'這個帳戶暫時沒有可選的 Bot。',sessions_loading:'正在讀取這隻 Bot 的聊天…',connecting:'正在接入你選定的聊天…',connect_unknown:'接入結果未確認；請核對結果，不會自動重做。',configured:'已設定接駁 · 待實際收發驗證',unavailable:'Grok Bot SDK 暫時不可用。',failed:'暫時未能讀取設定。'}[state.phase]||'');
    notice.textContent=state.error;dialog.querySelector('.grok-sdk-login-start').disabled=Boolean(state.loginFlight);
    link.href=state.login?.login_url||'';dialog.querySelector('.grok-sdk-login-cancel').disabled=Boolean(state.cancelFlight);dialog.querySelector('.grok-sdk-login-check').disabled=Boolean(state.pollFlight||state.cancelFlight);
    for(const input of form.elements)input.disabled=!ready;form.elements.bot.disabled=!['selecting','sessions_loading'].includes(state.phase);form.elements.session.disabled=!ready||!state.sessions.length;submit.disabled=!ready||!state.botId||state.sessionId===null||!form.elements.name.value.trim();
    const bots=JSON.stringify(state.bots);if(bots!==botSignature){botSignature=bots;options(form.elements.bot,state.bots,'請選擇一隻 Bot');}form.elements.bot.value=state.botId;
    const sessions=JSON.stringify([state.botId,state.sessions]);if(sessions!==sessionSignature){sessionSignature=sessions;options(form.elements.session,state.sessions.map((row,index)=>({...row,id:String(index),name:row.name+(row.id?' · '+row.id.slice(-12):'')})),state.botId?'請選擇一個聊天':'先選擇 Bot');}form.elements.session.value=state.sessionId===null?'':String(state.sessions.findIndex(row=>row.id===state.sessionId));
    dialog.querySelector('.grok-sdk-selection-note').textContent=grokSdkSelectionHint(state,form.elements.name.value);
    dialog.querySelector('.grok-sdk-roster-note').textContent=state.truncated?'清單未包含所有 Bot；找不到目標時請稍後重新讀取。':'';
    if(state.visible&&state.phase==='login_pending'){if(timer===null)timer=setInterval(()=>controller.pollLogin(),2000);}else stop();
  }
  const close=()=>{stop();controller.close();if(dialog.open)dialog.close();};
  dialog.querySelector('.invite-close').addEventListener('click',close);dialog.addEventListener('cancel',event=>{event.preventDefault();close();});dialog.addEventListener('close',()=>{stop();if(controller.visible)controller.close();});
  dialog.querySelector('.grok-sdk-login-start').addEventListener('click',()=>controller.startLogin());dialog.querySelector('.grok-sdk-login-check').addEventListener('click',()=>controller.pollLogin());dialog.querySelector('.grok-sdk-login-cancel').addEventListener('click',()=>controller.cancelLogin());refresh.addEventListener('click',()=>controller.refresh());
  form.elements.bot.addEventListener('change',()=>{const selected=controller.bots.find(row=>row.id===form.elements.bot.value);if(!form.elements.name.value.trim()||form.elements.name.value===suggestedName)form.elements.name.value=selected?.name||'';suggestedName=selected?.name||'';controller.selectBot(form.elements.bot.value);});form.elements.session.addEventListener('change',()=>{const index=form.elements.session.value;controller.selectSession(/^(0|[1-9][0-9]*)$/.test(index)?controller.sessions[Number(index)]?.id:null);});
  form.elements.name.addEventListener('input',()=>paint(controller));
  form.addEventListener('submit',event=>{event.preventDefault();controller.connect(form.elements.name.value);});dialog.querySelector('.grok-sdk-open-agent').addEventListener('click',close);
  const open=()=>{if(dialog.open)return;dialog.showModal();controller.open();};events.addEventListener(openEvent,open);
  return {dialog,controller,dispose(){events.removeEventListener(openEvent,open);close();dialog.remove();}};
}
