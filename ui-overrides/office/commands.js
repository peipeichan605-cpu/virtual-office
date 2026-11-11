import { officeGet,officePost } from './office-api.js';
import {grokConnectionView} from './grok-connection.js';

export const commandLabel=(state)=>({pending:'等待送出',sending:'正在送往原聊天',queued:'已排隊 · 等候原聊天開始',waiting_user:'等待你批准操作',
  running:'原聊天已接收 · 執行中',completed:'已完成',failed:'執行失敗',cancelled:'已中止',
  delivery_unknown:'送達結果未確認 · 請查看原聊天'}[state] || '狀態待確認');

export function queueHint(command,now=Date.now(),mode='background_queue') {
  return mode==='background_queue' && command?.state==='queued' && now-Date.parse(command.created_at)>=45000
    ? '仍在等候原聊天；如 App 未載入該聊天，請手動開啟一次。' : '';
}
export function localReceiverUnavailable(capability){return capability?.mode==='agent_inbox'&&capability.reason==='cursor_local_background_unavailable';}
export function receivingHint(capability,received=false,agent=''){
  if(localReceiverUnavailable(capability))return '只保存留言 · 無法自動喚醒原聊天';
  if(capability?.mode==='grok_webhook'||agent.startsWith('grok_bot:'))return grokConnectionView(capability).title;
  if(capability?.mode==='agent_inbox')return received?'上一則已被收取 · 新訊息會排隊':capability.receiver_online?'臨時收訊中 · 持續接駁待完成':'持續接駁待完成 · 可先留言';
  return capability?.persistent?(capability.native_ready===false?'指示會保留 · 等待原平台恢復':'持續收訊已接通 · 無需待命'):'背景傳送 · 忙碌時排隊';
}
export function receivingSetupControl(capability,agent='',received=false){
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
  const sync=capability?.public_sync;
  if(!sync)return 'App 公開同步：能力待確認';
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
  invalid_command:'請輸入 6,000 字以內嘅文字指示。',private_office_required:'請從私人 OFFICE 入口開啟。'};

/** Retry identity survives a page reload; prompt text is never put in browser storage. */
export async function requestIdentity(agent,text,storage=sessionStorage) {
  const hash=[...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(text.trim())))].map(x=>x.toString(16).padStart(2,'0')).join('');
  const key='office-command:'+agent;let saved;
  try{saved=JSON.parse(storage.getItem(key)||'null');}catch{}
  if(saved?.hash===hash && /^[0-9a-f-]{36}$/.test(saved.id))return saved.id;
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
    const inbox=capability?.mode==='agent_inbox',grok=capability?.mode==='grok_webhook'||agent.startsWith('grok_bot:');
    const targetLabel=inbox?'同 Agent 對話':capability?.mode==='grok_webhook'?'傳送指示給 Bot':'傳送指示到原聊天';
    this.input.setAttribute('aria-label',targetLabel);this.form.querySelector('label').firstChild.textContent=targetLabel;
    this.form.hidden=!capability?.can_send;
    if(!capability?.can_send && !this.latest)this.status.textContent=capability?.reason==='dispatch_disabled'?'這位 Agent 的指令入口已停用。':capability?.same_session?'原聊天指令入口暫時無法使用。':'尚未接通指令入口';
    else if(!this.latest && !this.uncertain)this.status.textContent=localReceiverUnavailable(capability)?'只能保存留言，不能自動喚醒這個 Cursor 原聊天。':grok?grokConnectionView(capability).message:inbox?'訊息會保存在這位 Agent 的私有收件箱。':'指示會送到呢個 Agent 原本嘅聊天。';
    const received=inbox&&['queued','running','waiting_user'].includes(this.latest?.state);
    this.form.querySelector('.command-actions span').textContent=receivingHint(capability,received,agent);
    if(!this.connect){this.connect=document.createElement('button');this.connect.type='button';this.connect.className='office-workspace-edit';this.connect.textContent='啟用持續收訊';this.connect.addEventListener('click',()=>window.dispatchEvent(new CustomEvent('office-inbox-setup',{detail:{agent_id:this.agent}})));this.element.append(this.connect);}
    const setup=receivingSetupControl(capability,agent,received);
    this.connect.hidden=setup.hidden;
    this.connect.textContent=setup.label;
    this.syncStatus.textContent=publicSyncHint(capability);this.syncStatus.title=this.syncStatus.textContent;
    this.updateButton();
  }
  updateButton(){this.button.disabled=this.busy||!this.capability?.can_send||!this.input.value.trim();this.button.textContent=this.busy?'傳送中…':this.capability?.mode==='agent_inbox'&&!this.capability.receiver_online?'留言':'傳送';}
  show(command) {
    this.latest=command;
    const hint=queueHint(command,Date.now(),this.capability?.mode);
    const label=this.capability?.mode==='agent_inbox'?({pending:'已儲存 · 等待 Agent 收取',queued:'已交給接收工具 · 等待 Agent 確認',running:'Agent 已確認 · 處理中',completed:'Agent 已交付回覆',cancelled:'已取消'}[command.state]||commandLabel(command.state)):this.capability?.mode==='grok_webhook'?({queued:'Webhook 已接收 · 等待 Bot 回報',running:'Bot 已回報開始 · 執行中',completed:'Bot 已回報完成'}[command.state]||commandLabel(command.state)):commandLabel(command.state);
    const visibleLabel=localReceiverUnavailable(this.capability)&&command.state==='pending'?'已保存留言 · 原聊天未收到':label;
    this.status.textContent=hint || visibleLabel+(command.detail_code==='open_original_app'?' · 請開啟原 App':'')+' — '+command.preview.replace(/\s+/g,' ').slice(0,48);
    if(command.detail_code==='completed_without_reply')this.status.textContent+=' · 平台未提供回覆文字';
    if(command.detail_code==='platform_agent_busy')this.status.textContent='原 Agent 正在處理另一項工作；此指令未送達。';
    if(command.detail_code==='waiting_for_reported_work')this.status.textContent='等待這位 Agent 在 App 的工作結束；指示尚未送出';
    this.status.title=this.status.textContent;
    this.status.dataset.state=command.state;
  }
  renderApprovals(items) {
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
    const pending=items.filter(item=>item.state==='pending');
    const signature=JSON.stringify(pending.map(item=>item.id));
    if(signature===this.pendingSignature)return;
    this.pendingSignature=signature;
    if(!this.pendingBox){this.pendingBox=document.createElement('details');this.pendingBox.className='command-pending';this.element.append(this.pendingBox);}
    this.pendingBox.replaceChildren();this.pendingBox.hidden=!pending.length;
    const summary=document.createElement('summary');summary.textContent=`待收取訊息（${pending.length}）`;this.pendingBox.append(summary);
    const scope=this.scope();
    for(const item of pending){const row=document.createElement('p'),text=document.createElement('span'),button=document.createElement('button');text.textContent=item.preview.slice(0,100);button.type='button';button.textContent='取消';button.setAttribute('aria-label','取消待收取訊息：'+item.preview.slice(0,60));
      button.addEventListener('click',async()=>{if(!this.current(scope)||this.pendingSignature!==signature)return;button.disabled=true;this.beginMutation();let refresh=false;try{const data=await officePost('/api/office/commands/cancel',decodeURIComponent(location.hash.slice(1)),{agent_id:scope.agent,command_id:item.id});if(!this.current(scope))return;this.show(data.command);this.pendingSignature=null;refresh=true;}catch{if(this.current(scope)){this.status.textContent='訊息可能已被收取，未能取消；請查看最新狀態。';button.disabled=false;}}finally{this.endMutation(scope);if(refresh&&this.current(scope))await this.refresh();}});row.append(text,button);this.pendingBox.append(row);}
  }
  confirmed(command) {
    this.show(command);
    if(this.attempt?.id===command.id){
      if(this.input.value.trim()===this.attempt.text)this.input.value='';
      const key='office-command:'+this.agent;
      try{if(JSON.parse(sessionStorage.getItem(key)||'null')?.id===command.id)sessionStorage.removeItem(key);}catch{}
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
      const command=data.commands.find(row=>row.id===this.attempt?.id)||data.commands[0];
      if(command && (!this.uncertain||command.id===this.attempt?.id))this.confirmed(command);
    }catch{if(currentRead()&&this.latest)this.status.textContent='連線中斷 · 上次指令狀態：'+commandLabel(this.latest.state);}
    finally{if(currentRead())this.refreshing=false;}
  }
  dispose(){this.resetIdentity();this.agent=null;this.capability=null;this.form.hidden=true;this.syncStatus.textContent='';}
}
