import { officeGet,officePost } from './office-api.js';
import {isMobileAccess} from './mobile-access.js';
import {grokConnectionView} from './grok-connection.js';
import {openCodexModelDialog} from './codex-model-ui.js';

export const commandLabel=(state)=>({pending:'等待送出',sending:'正在送往原聊天',queued:'已排隊 · 等候原聊天開始',waiting_user:'等待你批准操作',
  running:'原聊天已接收 · 執行中',completed:'已完成',failed:'執行失敗',cancelled:'已中止',
  delivery_unknown:'送達結果未確認 · 請查看原聊天'}[state] || '狀態待確認');

export const codexBackgroundFailureHint=code=>({
  codex_model_unavailable:'目前本機 Codex 執行環境無法使用這個模型。請檢查版本及帳戶權限，或按「模型設定」自行選擇模型；同一條對話及紀錄保留，不會自動切換模型。',
  codex_authentication_required:'Codex 登入已失效或未完成。請在運行 OFFICE 的電腦重新登入 Codex，再重試。',
  codex_login_required:'請在運行 OFFICE 的電腦登入 Codex，再重試。',
  codex_quota_exceeded:'Codex 帳戶目前的用量已達上限。請查看帳戶用量，待恢復後再重試。',
  codex_rate_limited:'Codex 暫時限制請求頻率。請稍後再重試。',
  codex_context_limit:'這條 Codex 對話已達內容長度限制。請建立新的背景對話，並提供需要沿用的摘要。',
  codex_provider_unavailable:'Codex 服務暫時無法使用。請稍後再重試。',
  codex_turn_failed:'Codex 未能完成這次工作；既有對話及紀錄保留。請核對指示後再重試。',
}[code]||'');

export function queueHint(command,now=Date.now(),mode='background_queue') {
  return mode==='background_queue' && command?.state==='queued' && now-Date.parse(command.created_at)>=45000
    ? '仍在等候原聊天；如 App 未載入該聊天，請手動開啟一次。' : '';
}
export function localReceiverUnavailable(capability){return capability?.mode==='agent_inbox'&&capability.reason==='cursor_local_background_unavailable';}
const grokSdkLoginRequired=capability=>['grok_sdk_login_required','grok_sdk_login_expired','grok_sdk_authentication_required','grok_sdk_auth_required','grok_sdk_credentials_unavailable'].includes(capability?.reason);
export function receivingHint(capability,received=false,agent=''){
  if(capability?.mode==='grok_sdk'){
    if(capability.reason==='dispatch_disabled')return 'Grok Bot SDK（實驗）· 指令入口已停用';
    if(grokSdkLoginRequired(capability))return 'Grok Bot SDK（實驗）· 請重新登入';
    return capability.native_ready===true&&capability.can_send===true?'Grok Bot SDK（實驗）· 背景收訊入口可用':'Grok Bot SDK（實驗）· 入口暫時不可用';
  }
  if(capability?.mode==='codex_background')return capability.native_ready===true&&capability.can_send===true?'Codex 背景模式 · App 可關閉 · OFFICE 需運作':'Codex 背景入口暫時不可用 · 對話仍保留';
  if(capability?.mode==='cursor_local')return capability.native_ready===true&&capability.can_send===true?'Cursor SDK · 純對話 · 由此傳送及續接':'Cursor SDK 入口暫時不可用 · 對話仍保留';
  if(localReceiverUnavailable(capability))return '只保存留言 · 無法自動喚醒原聊天';
  if(capability?.mode==='grok_webhook'||agent.startsWith('grok_bot:'))return grokConnectionView(capability).title;
  if(capability?.mode==='agent_inbox')return received?'上一則已被收取 · 新訊息會排隊':capability.receiver_online?'臨時收訊中 · 持續接駁待完成':'持續接駁待完成 · 可先留言';
  return capability?.persistent?(capability.native_ready===false?'指示會保留 · 等待原平台恢復':'持續收訊已接通 · 無需待命'):'背景傳送 · 忙碌時排隊';
}
export function receivingSetupControl(capability,agent='',received=false){
  if(capability?.mode==='grok_sdk')return {hidden:capability.native_ready===true&&capability.can_send===true,label:grokSdkLoginRequired(capability)?'重新登入 Grok':'查看 Grok SDK 設定'};
  if(capability?.mode==='grok_webhook'||agent.startsWith('grok_bot:')){
    const view=grokConnectionView(capability),check=capability?.connection_check;
    if(view.state==='verified')return {hidden:true,label:'查看連接狀態'};
    if(['waiting_platform','unconfirmed','disabled','unsupported'].includes(view.state))return {hidden:false,label:'查看連接狀態'};
    if(['queued','waiting_reply'].includes(check?.state))return {hidden:false,label:'查看測試進度'};
    if(!capability?.persistent)return {hidden:false,label:'完成 Grok 連接'};
    return {hidden:false,label:'測試連接'};
  }
  return {hidden:capability?.mode!=='agent_inbox'||received,label:localReceiverUnavailable(capability)?'查看收訊限制':'啟用持續收訊'};
}
export function receivingSetupLabel(capability,agent=''){return receivingSetupControl(capability,agent).label;}
export function publicSyncHint(capability){
  if(capability?.mode==='grok_sdk'){
    const sync=capability.public_sync;
    if(grokSdkLoginRequired(capability))return 'Grok App 公開同步已暫停；重新登入後續接已選的 Bot／聊天。';
    if(sync?.history_gap===true||sync?.has_gaps===true||sync?.state==='history_gap')return 'Grok App 公開同步：部分正文未取得，紀錄尚未完整。';
    if(sync?.state==='recovering')return 'Grok App 公開同步暫時中斷；正在重新連接同一聊天。';
    if(sync?.configured===true&&sync.automatic===true&&sync.manual_app_automatic===true&&sync.state==='configured')return '同一 Grok Bot 聊天：公開同步已設定；實際同步請以新工作紀錄為準。';
    if(sync?.state==='unavailable'||sync?.state==='disabled')return 'Grok App 公開同步暫時不可用；既有紀錄保留。';
    return 'Grok App 公開同步準備中；尚未確認新對話已同步。';
  }
  if(capability?.mode==='codex_background')return '公開同步範圍：這條 OFFICE 管理的 Codex 背景對話；工作資料夾唯讀，額外權限須逐次批准。';
  if(capability?.mode==='cursor_local')return '公開同步範圍：這條 OFFICE 管理的 Cursor SDK 對話。';
  const sync=capability?.public_sync;
  if(!sync)return 'App 公開同步：能力待確認';
  if(sync.mode==='cloud_conversation_observer'&&sync.scope==='selected_cloud_agent'){
    if(sync.state==='configured'&&sync.configured===true&&sync.automatic===true&&sync.manual_app_automatic===true)return '同一 Cloud Agent：自動同步 Cursor App 新工作、公開對話及交付';
    if(sync.state==='recovering')return 'Cloud 對話同步暫時中斷；正在重新連接';
    return 'Cloud 對話同步準備中；尚未確認 App 新工作';
  }
  if(sync.mode==='cooperative'||sync.state==='cooperative')return 'App 直接工作：需 Agent 合作上報；沒有原生自動同步';
  if(sync.state==='unsupported'||sync.mode==='unsupported'||sync.scope==='none')return 'App 直接工作：尚未有可用的公開同步入口';
  if(sync.scope==='office_dispatched_runs')return sync.configured===true?'公開對話：只同步 OFFICE 派發的工作':'公開同步只限 OFFICE 派發的工作；尚待設定';
  if(sync.configured===true&&sync.manual_app_automatic===true&&['native_observer','native_hook','native_run_observer'].includes(sync.mode))return '原聊天公開同步已設定';
  if(sync.state==='setup_required')return 'App 公開同步：尚待完成設定';
  return 'App 公開同步：範圍有限，請查看平台能力';
}

const bindingScope=capability=>JSON.stringify([capability?.mode||null,capability?.connection_check?.route_revision||null,
  capability?.can_send===true,capability?.native_ready===true,capability?.reason==='agent_not_selected',capability?.reason==='dispatch_disabled']);

const errors={not_connected:'呢個 Agent 尚未接通指令入口。',adapter_unavailable:'原聊天指令入口暫時無法使用。',
  queue_full:'待處理指令已滿，請先查看原聊天。',request_id_conflict:'呢次指令記錄不一致，請重新開啟 OFFICE。',
  mobile_forbidden:'這部手機未獲准向此 Agent 發訊息。',mobile_scope_denied:'這部手機未獲准向此 Agent 發訊息。',mobile_rate_limited:'訊息太頻繁，請稍後再試。',mobile_command_expired:'這次指令已過期，沒有自動重新派送。',
  invalid_command:'請輸入 6,000 字以內嘅文字指示。',private_office_required:'請從私人 OFFICE 入口開啟。'};

/** Retry identity survives a page reload; prompt text is never put in browser storage. */
function savedRequestIdentity(agent,storage) {
  try {
    storage??=globalThis.sessionStorage;
    const saved=JSON.parse(storage?.getItem('office-command:'+agent)||'null');
    return typeof saved?.hash==='string'&&/^[0-9a-f]{64}$/.test(saved.hash)&&/^[0-9a-f-]{36}$/.test(saved.id)?saved:null;
  } catch { return null; }
}
function retireRequestIdentity(agent,command) {
  if(command?.agent_id!==agent||savedRequestIdentity(agent)?.id!==command.id)return;
  try{globalThis.sessionStorage.removeItem('office-command:'+agent);}catch{}
}
export async function requestIdentity(agent,text,storage=sessionStorage) {
  const hash=[...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(text.trim())))].map(x=>x.toString(16).padStart(2,'0')).join('');
  const key='office-command:'+agent,saved=savedRequestIdentity(agent,storage);
  if(saved?.hash===hash)return saved.id;
  const id=crypto.randomUUID();storage.setItem(key,JSON.stringify({id,hash}));return id;
}

export class CommandComposer {
  constructor() {
    this.element=document.createElement('div');this.element.className='office-command';this.busy=false;this.epoch=0;this.readRevision=0;this.mutationDepth=0;
    this.element.innerHTML='<form hidden><label>傳送指示到原聊天<textarea rows="2" maxlength="6000" placeholder="輸入指示…" autocomplete="off"></textarea></label><div class="command-actions"><span>背景傳送 · 忙碌時排隊</span><button type="submit">傳送</button></div></form><p class="command-status" role="status" aria-live="polite"></p><p class="command-status command-public-sync"></p>';
    this.form=this.element.querySelector('form');this.input=this.element.querySelector('textarea');this.button=this.element.querySelector('button');this.status=this.element.querySelector('.command-status');
    this.approvalBox=document.createElement('div');this.approvalBox.className='command-approvals';this.element.append(this.approvalBox);
    this.syncStatus=this.element.querySelector('.command-public-sync');
    this.form.addEventListener('submit',event=>{event.preventDefault();this.send();});
    this.input.addEventListener('keydown',event=>{event.stopPropagation();if(!event.isComposing && event.key==='Enter'&&(event.metaKey||event.ctrlKey)){event.preventDefault();this.send();}});
    this.input.addEventListener('keyup',event=>event.stopPropagation());
    this.input.addEventListener('input',()=>this.updateButton());
  }
  scope(){return {agent:this.agent,epoch:this.epoch};}
  current(scope){return this.agent===scope.agent&&this.epoch===scope.epoch;}
  beginMutation(){this.readRevision=(this.readRevision||0)+1;this.refreshing=false;this.mutationDepth=(this.mutationDepth||0)+1;}
  endMutation(scope){if(this.current(scope)){this.readRevision++;this.refreshing=false;this.mutationDepth=Math.max(0,this.mutationDepth-1);}}
  invalidateAsync(){
    this.epoch++;this.readRevision=(this.readRevision||0)+1;this.mutationDepth=0;this.busy=false;this.refreshing=false;
    this.approvalSignature=null;this.pendingSignature=null;this.approvalBox.replaceChildren();
    if(this.pendingBox){this.pendingBox.replaceChildren();this.pendingBox.hidden=true;}
  }
  resetIdentity(){
    this.invalidateAsync();this.latest=null;this.attempt=null;this.uncertain=false;
    this.input.value='';this.status.textContent='';this.status.title='';delete this.status.dataset.state;
  }
  bind(agent='',capability) {
    if(this.agent!==agent)this.resetIdentity();
    else if(bindingScope(this.capability)!==bindingScope(capability))this.invalidateAsync();
    this.agent=agent;this.capability=capability;
    const inbox=capability?.mode==='agent_inbox',grokSdk=capability?.mode==='grok_sdk',grok=!grokSdk&&(capability?.mode==='grok_webhook'||agent.startsWith('grok_bot:')),sdk=capability?.mode==='cursor_local',cloud=capability?.mode==='cursor_cloud',background=capability?.mode==='codex_background';
    const targetLabel=grokSdk?'傳送到 Grok App 的同一 Bot／聊天':background?'傳送到 Codex 背景對話':sdk?'傳送到 Cursor SDK 對話':cloud?'傳送到 Cursor App 的 Cloud Agent':inbox?'同 Agent 對話':capability?.mode==='grok_webhook'?'傳送指示給 Bot':'傳送指示到原聊天';
    this.input.setAttribute('aria-label',targetLabel);this.form.querySelector('label').firstChild.textContent=targetLabel;
    this.form.hidden=!capability?.can_send;
    if(!capability?.can_send && !this.latest)this.status.textContent=capability?.reason==='mobile_view_only'?'這部手機只有查看權限，未獲准向此 Agent 發訊息。':capability?.reason==='dispatch_disabled'?'這位 Agent 的指令入口已停用。':grokSdk?'Grok Bot SDK 入口暫時無法使用；既有 Bot、聊天及紀錄保留。':background?'Codex 背景入口暫時無法使用；既有對話及紀錄保留。':sdk?'Cursor SDK 入口暫時無法使用；既有對話及紀錄保留。':capability?.same_session?'原聊天指令入口暫時無法使用。':'尚未接通指令入口';
    else if(!this.latest && !this.uncertain)this.status.textContent=localReceiverUnavailable(capability)?'只能保存留言，不能自動喚醒這個 Cursor 原聊天。':grokSdk?'指示會透過實驗 SDK 送到已選的 Grok Bot／聊天。入口已設定；實際回覆及交付仍需核對。':background?'首次指示會啟動獨立的 Codex 背景對話，之後續接同一對話；公開回覆及交付紀錄會同步到這裡。':sdk?'傳送時會建立或續接這條 SDK session，公開回覆會同步到這裡。':cloud?'指示會送到 Cursor App 中同一隻 Cloud Agent。':grok?grokConnectionView(capability).message:inbox?'訊息會保存在這位 Agent 的私有收件箱。':'指示會送到呢個 Agent 原本嘅聊天。';
    const received=inbox&&['queued','running','waiting_user'].includes(this.latest?.state);
    this.form.querySelector('.command-actions span').textContent=receivingHint(capability,received,agent);
    if(!this.connect){this.connect=document.createElement('button');this.connect.type='button';this.connect.className='office-workspace-edit';this.connect.textContent='啟用持續收訊';this.connect.addEventListener('click',()=>window.dispatchEvent(new CustomEvent(this.capability?.mode==='grok_sdk'?'office:new-grok-sdk-conversation':'office-inbox-setup',{detail:{agent_id:this.agent}})));this.element.append(this.connect);}
    const setup=receivingSetupControl(capability,agent,received);
    this.connect.hidden=isMobileAccess()||setup.hidden;
    this.connect.textContent=setup.label;
    this.syncStatus.textContent=publicSyncHint(capability);this.syncStatus.title=this.syncStatus.textContent;
    if(background&&!this.modelControl){this.modelControl=document.createElement('button');this.modelControl.type='button';this.modelControl.className='office-workspace-edit';this.modelControl.textContent='模型設定';this.modelControl.addEventListener('click',()=>{if(this.capability?.mode==='codex_background')openCodexModelDialog(this.agent);});this.element.append(this.modelControl);}
    if(this.modelControl)this.modelControl.hidden=isMobileAccess()||!background;
    this.updateButton();
  }
  updateButton(){this.button.disabled=this.busy||!this.capability?.can_send||!this.input.value.trim();this.button.textContent=this.busy?'傳送中…':this.capability?.mode==='agent_inbox'&&!this.capability.receiver_online?'留言':'傳送';}
  show(command) {
    this.latest=command;
    const hint=queueHint(command,Date.now(),this.capability?.mode);
    const label=this.capability?.mode==='agent_inbox'?({pending:'已儲存 · 等待 Agent 收取',queued:'已交給接收工具 · 等待 Agent 確認',running:'Agent 已確認 · 處理中',completed:'Agent 已交付回覆',cancelled:'已取消'}[command.state]||commandLabel(command.state)):this.capability?.mode==='grok_webhook'?({queued:'Webhook 已接收 · 等待 Bot 回報',running:'Bot 已回報開始 · 執行中',completed:'Bot 已回報完成'}[command.state]||commandLabel(command.state)):commandLabel(command.state);
    const sdkLabels={pending:'等待 Cursor SDK 執行',sending:'正在送到 Cursor SDK',queued:'Cursor SDK 已排隊',running:'Cursor SDK 執行中',completed:'Cursor SDK 已交付回覆',delivery_unknown:'SDK 執行結果未確認 · 請核對這次工作'};
    const backgroundLabels={pending:'等待 Codex 背景執行',sending:'正在送到 Codex 背景對話',queued:'Codex 背景指示已排隊',running:'Codex 背景執行中',completed:'Codex 背景對話已交付回覆',delivery_unknown:'背景執行結果未確認 · 請核對這次工作'};
    const grokSdkLabels={pending:'等待 Grok SDK 傳送',sending:'正在送到 Grok 原聊天',queued:'Grok 入口已接收 · 等待原聊天開始',running:'Grok 原聊天執行中',completed:'Grok 原聊天已交付回覆',delivery_unknown:'Grok 送達結果未確認 · 請核對原聊天'};
    const visibleLabel=this.capability?.mode==='grok_sdk'?(grokSdkLabels[command.state]||label):this.capability?.mode==='codex_background'?(backgroundLabels[command.state]||label):this.capability?.mode==='cursor_local'?(sdkLabels[command.state]||label):localReceiverUnavailable(this.capability)&&command.state==='pending'?'已保存留言 · 原聊天未收到':label;
    this.status.textContent=hint || visibleLabel+(command.detail_code==='open_original_app'?' · 請開啟原 App':'')+' — '+command.preview.replace(/\s+/g,' ').slice(0,48);
    if(command.detail_code==='completed_without_reply')this.status.textContent+=' · 平台未提供回覆文字';
    if(command.detail_code==='platform_agent_busy')this.status.textContent='原 Agent 正在處理另一項工作；此指令未送達。';
    if(command.detail_code==='waiting_for_reported_work')this.status.textContent='等待這位 Agent 在 App 的工作結束；指示尚未送出';
    if(command.detail_code==='mobile_command_expired')this.status.textContent='手機指令已過期並取消，沒有送交 Agent 執行。';
    if(command.detail_code==='mobile_authorization_revoked')this.status.textContent='手機授權已撤銷，此待處理指令已取消，沒有送交 Agent 執行。';
    if(this.capability?.mode==='codex_background'&&['failed','delivery_unknown'].includes(command.state)){
      const explanation=codexBackgroundFailureHint(command.detail_code);if(explanation)this.status.textContent+=' · '+explanation;
    }
    this.status.title=this.status.textContent;
    this.status.dataset.state=command.state;
  }
  renderApprovals(items) {
    if(isMobileAccess())items=[];
    const signature=JSON.stringify(items.map(item=>[item.id,item.can_allow,item.preview]));
    if(signature===this.approvalSignature)return;
    this.approvalSignature=signature;this.approvalBox.replaceChildren();
    const scope=this.scope();
    for(const item of items){
      const card=document.createElement('div');card.className='command-approval';
      const title=document.createElement('strong');title.textContent='Agent 要求批准';
      const preview=document.createElement('pre');preview.textContent=item.preview;
      const controls=document.createElement('div');
      for(const [decision,label] of [['reject_once','拒絕'],['allow_once','允許今次']]){
        if(decision==='allow_once'&&!item.can_allow)continue;
        const button=document.createElement('button');button.type='button';button.textContent=label;
        button.addEventListener('click',async()=>{
          if(!this.current(scope)||this.approvalSignature!==signature)return;
          for(const b of controls.children)b.disabled=true;
          this.beginMutation();
          try{await officePost('/api/office/permissions',decodeURIComponent(location.hash.slice(1)),{agent_id:scope.agent,approval_id:item.id,decision});if(this.current(scope))card.remove();}
          catch{if(this.current(scope)){if(this.approvalSignature===signature)this.approvalSignature=null;preview.textContent='批准要求已過期或連線中斷；請查看指令狀態。';}}
          finally{this.endMutation(scope);}
        });controls.append(button);
      }
      card.append(title,preview,controls);this.approvalBox.append(card);
    }
  }
  renderPending(items) {
    const mobile=isMobileAccess();
    const pending=items.filter(item=>item.state==='pending');
    const signature=JSON.stringify(pending.map(item=>[item.id,!mobile||item.can_cancel===true]));
    if(signature===this.pendingSignature)return;
    this.pendingSignature=signature;
    if(!this.pendingBox){this.pendingBox=document.createElement('details');this.pendingBox.className='command-pending';this.element.append(this.pendingBox);}
    this.pendingBox.replaceChildren();this.pendingBox.hidden=!pending.length;
    const summary=document.createElement('summary');summary.textContent=`待收取訊息（${pending.length}）`;this.pendingBox.append(summary);
    const scope=this.scope();
    for(const item of pending){const row=document.createElement('p'),text=document.createElement('span');text.textContent=item.preview.slice(0,100);
      if(mobile&&item.can_cancel!==true){row.append(text);this.pendingBox.append(row);continue;}
      const button=document.createElement('button');button.type='button';button.textContent='取消';button.setAttribute('aria-label','取消待收取訊息：'+item.preview.slice(0,60));
      button.addEventListener('click',async()=>{if(!this.current(scope)||this.pendingSignature!==signature)return;button.disabled=true;this.beginMutation();let refresh=false;try{const data=await officePost('/api/office/commands/cancel',decodeURIComponent(location.hash.slice(1)),{agent_id:scope.agent,command_id:item.id});if(!this.current(scope))return;this.show(data.command);this.pendingSignature=null;refresh=true;}catch{if(this.current(scope)){this.status.textContent='訊息可能已被收取，未能取消；請查看最新狀態。';button.disabled=false;}}finally{this.endMutation(scope);if(refresh&&this.current(scope))await this.refresh();}});row.append(text,button);this.pendingBox.append(row);}
  }
  confirmed(command) {
    this.show(command);
    // A reloaded composer has no in-memory attempt. Retire its saved retry only
    // when this exact Agent/request has an authoritative server receipt.
    retireRequestIdentity(this.agent,command);
    if(this.attempt?.id===command.id){
      if(this.input.value.trim()===this.attempt.text)this.input.value='';
      this.attempt=null;this.uncertain=false;
    }
    this.updateButton();
  }
  async send() {
    if(this.busy||!this.capability?.can_send||!this.input.value.trim())return;
    this.busy=true;this.updateButton();const scope=this.scope(),agent=scope.agent,text=this.input.value.trim();let submitted=false;this.beginMutation();
    try {
      const id=await requestIdentity(agent,text);if(!this.current(scope)||!this.capability?.can_send)return;this.attempt={id,text};
      this.status.textContent='正在傳送指示…';
      submitted=true;
      const data=await officePost('/api/office/commands',decodeURIComponent(location.hash.slice(1)),{request_id:id,agent_id:agent,text});
      if(this.current(scope))this.confirmed(data.command);
    }catch(error){
      if(!this.current(scope))return;
      this.uncertain=submitted&&(!error.status||error.status>=500);
      this.status.textContent=this.uncertain?'未確認伺服器有冇收到；正在核對。再次傳送同一句會沿用同一指令編號。':errors[error.message]||'暫時無法傳送指示。';
    }finally{this.endMutation(scope);if(this.current(scope)){this.busy=false;this.updateButton();}}
  }
  async refresh() {
    if(!this.agent||!(this.capability?.connected||this.capability?.same_session)||this.refreshing||this.busy||this.mutationDepth>0)return;
    this.refreshing=true;const scope=this.scope(),agent=scope.agent,revision=this.readRevision,currentRead=()=>this.current(scope)&&revision===this.readRevision;
    try{
      const data=await officeGet('/api/office/commands?'+new URLSearchParams({agent}),decodeURIComponent(location.hash.slice(1)));
      if(!currentRead())return;
      this.bind(agent,data.capability);
      this.renderApprovals(data.approvals || []);
      this.renderPending(data.commands);
      const savedId=savedRequestIdentity(agent)?.id;
      retireRequestIdentity(agent,data.commands.find(row=>row.agent_id===agent&&row.id===savedId));
      const command=data.commands.find(row=>row.id===this.attempt?.id)||data.commands[0];
      if(command && (!this.uncertain||command.id===this.attempt?.id))this.confirmed(command);
    }catch{if(currentRead()&&this.latest)this.status.textContent='連線中斷 · 上次指令狀態：'+commandLabel(this.latest.state);}
    finally{if(currentRead())this.refreshing=false;}
  }
  dispose(){this.resetIdentity();this.agent=null;this.capability=null;this.form.hidden=true;this.syncStatus.textContent='';if(this.modelControl)this.modelControl.hidden=true;}
}
