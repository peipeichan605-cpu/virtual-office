import {officeGet, officePost} from './office-api.js';

const BASE='/api/office/mobile';
const ownerEntryHelp='這個分頁沒有管理權限。請返回這部電腦原先開啟的私人 OFFICE 管理分頁，或開啟你儲存的私人管理書籤，再按「手機接入」。';
const ownerAccessDenied=error=>error?.code==='private_office_required'||error?.status===401||
  (error?.status===403&&(!error.code||error.code==='forbidden'));
const failure=error=>({private_office_required:ownerEntryHelp,mobile_disabled:'手機入口尚未設定。請先完成私人連線設定，再重新檢查。',mobile_not_configured:'手機入口尚未設定。請先完成私人連線設定，再重新檢查。',invalid_identity:'請填入手機登入 Tailscale 所用的帳戶。',invalid_scopes:'請選擇手機可以查看的 Agents。',invalid_mobile_scope:'請重新選擇查看及發訊息權限。',mobile_agent_not_selected:'Agent 名單已變更，請重新檢查後再儲存。',mobile_device_not_found:'找不到這部手機，請重新檢查。',mobile_device_unavailable:'這部手機已過期或撤銷，請重新配對。'}[error?.code||error?.message]||'暫時未能完成操作，請重新檢查狀態。');
const ownerKey=()=>{try{return decodeURIComponent(globalThis.location?.hash?.slice(1)||'');}catch{return '';}};
const assistantPending=snapshot=>snapshot?.setup?.transport?.state==='assistant_pending';
const canPair=snapshot=>snapshot?.enabled===true&&snapshot?.credentials_separated!==false&&snapshot?.configured!==false&&snapshot?.setup?.configuration_ready!==false&&snapshot?.setup?.pairing_available!==false&&!assistantPending(snapshot);
const pendingMessage='手機入口尚待確認。請在這裡選擇手機並繼續。';
function readFailure(error){
  if(error?.code==='invalid_mobile_configuration')return 'OFFICE 的手機入口設定無法讀取。請在電腦檢查這套 OFFICE 的設定，再重新檢查。';
  if(error?.name==='TimeoutError'||error?.name==='AbortError')return '電腦上的 OFFICE 回應逾時，請重新檢查。';
  if(error instanceof TypeError)return '連不到電腦上的 OFFICE。請確認 OFFICE 仍在運行，再重新檢查。';
  return '暫時讀不到 OFFICE 的手機接入狀態，請重新檢查。';
}
const hostInstructions={
  install:'先在這部電腦安裝 Tailscale，然後回到 OFFICE。',
  login:'請在電腦的 Tailscale 登入手機正在使用的帳戶。登入後回到 OFFICE。',
  connect:'請在電腦的 Tailscale 按 Connect，連線後回到 OFFICE。',
  device_approval:'這部電腦正等候私人網絡管理員批准。在 Tailscale 裝置列表批准後回來繼續。',
  update_required:'請更新這部電腦的 Tailscale，完成後回來繼續。',
  https_required:'這個私人網絡需要先啟用 HTTPS。請在 Tailscale 的 DNS 頁開啟 HTTPS Certificates，然後回來繼續。',
  dns_required:'請在 Tailscale 的 DNS 頁開啟 MagicDNS，讓手機能找到這部電腦。',
  no_phone:'未找到在線的手機。在手機打開 Tailscale，用同一帳戶登入並按 Connect，再重新尋找。',
  select_phone:'選擇你要用來開啟 OFFICE 的手機。',
  ready:'電腦入口已準備好。下一步可以選擇手機的 Agent 權限。',
  permission_required:'電腦上的 Tailscale 未批准建立私人入口。請使用這部電腦上有權管理 Tailscale 的帳戶，批准後回來重新檢查。',
  conflict:'現有私人連線服務已佔用這個入口。請私人網絡的管理員檢查該服務；OFFICE 會保留現有設定。',
  busy:'另一個設定操作正在進行，稍後可以重新檢查。',
  confirm_again:'手機或電腦的狀態剛有變更。請重新尋找手機，再確認一次。',
  activation_unconfirmed:'尚未確認電腦入口已啟用。請重新檢查目前進度。',
  existing_entry:'這套 OFFICE 已有私人入口。可沿用原本的連線及手機配對。',
  unknown:'未能確認電腦的 Tailscale 狀態。請檢查電腦連線，然後再試。',
};
const localInstructions={
  idle:'這套 OFFICE 使用舊管理連結。請在這部電腦確認一次升級，之後會開啟新的 OFFICE 管理頁。升級會更新書籤及停用舊版外部動態通知。',
  pending:'請在這部電腦彈出的確認視窗選擇是否升級。批准後，請在新開啟的 OFFICE 管理頁繼續。',
  cancelled:'這次升級已取消。你可以隨時再開始。',
  unavailable:'OFFICE 無法顯示電腦確認視窗。請在運行 OFFICE 的電腦登入桌面並解鎖，再試一次。',
  retry_later:'剛剛已要求確認，請稍候再試。',
  configuration_changed:'OFFICE 設定已變更。請重新檢查後再開始。',
  failed:'未能完成升級。請重新檢查後再開始。',
  open_failed:'請從這部電腦原本的 OFFICE 啟動入口重新開啟管理頁，再選「手機接入」。',
  changed:'舊管理頁已無法使用。請查看新開啟的 OFFICE 管理頁；如看不到，可從這部電腦的 OFFICE 啟動入口重新開啟。',
};
const platforms=['windows','macos','linux'];
const expiryMs=value=>typeof value==='number'?value*1000:Date.parse(value);
// The API returns state/revoked_at; revoked also covers the immediate local update.
const deviceRevoked=device=>device?.state==='revoked'||device?.revoked_at!=null||device?.revoked===true;
const scopePayload=(snapshot,{view_agents=[],send_agents=[],view_mode='selected',send_mode='selected'})=>{
  if(!['selected','enrolled'].includes(view_mode)||!['selected','enrolled'].includes(send_mode)||!Array.isArray(view_agents)||!Array.isArray(send_agents)||send_mode==='enrolled'&&view_mode!=='enrolled')return null;
  const available=new Set((snapshot?.agents||[]).map(a=>a.id)),views=[...new Set(view_agents)],sends=[...new Set(send_agents)];
  if(view_mode==='selected'&&(!views.length||views.some(id=>!available.has(id)))||send_mode==='selected'&&sends.some(id=>!available.has(id)||view_mode==='selected'&&!views.includes(id)))return null;
  return {view_agents:view_mode==='enrolled'?[]:views,send_agents:send_mode==='enrolled'?[]:sends,view_mode,send_mode};
};

/** Only loads a local encoder, and only after an owner creates a pairing. */
export class MobilePairingQR{
  constructor({load=()=>import('./vendor/qrcode-generator/qrcode.mjs'),draw,clear=()=>{},unavailable=()=>{}}){Object.assign(this,{load,draw,clear,unavailable});this.epoch=0;this.url=null;}
  reset(){this.epoch++;this.url=null;this.clear();}
  async show(pairing){
    const url=pairing?.url||null;if(url===this.url)return;
    this.reset();if(!url)return;this.url=url;const epoch=this.epoch;
    try{const module=await this.load();if(epoch!==this.epoch)return;this.draw(module.default,url);}
    catch{if(epoch===this.epoch){this.clear();this.unavailable();}}
  }
}

export function drawPairingQR(canvas,encode,url){
  const code=encode(0,'M');code.addData(url,'Byte');code.make();
  const count=code.getModuleCount(),quiet=4,cell=Math.max(1,Math.ceil(288/(count+quiet*2))),size=(count+quiet*2)*cell;
  if(!Number.isInteger(count)||count<21||count>177)throw Error('Invalid QR dimensions');
  canvas.width=canvas.height=size;const ctx=canvas.getContext('2d');if(!ctx)throw Error('Canvas unavailable');
  ctx.fillStyle='#fff';ctx.fillRect(0,0,size,size);ctx.fillStyle='#000';
  for(let row=0;row<count;row++)for(let column=0;column<count;column++)if(code.isDark(row,column))ctx.fillRect((column+quiet)*cell,(row+quiet)*cell,cell,cell);
}

export function pairingLink(origin,token){
  try{
    const url=new URL(origin);
    if(url.protocol!=='https:'||url.username||url.password||url.pathname!=='/'||url.search||url.hash||typeof token!=='string'||!/^[A-Za-z0-9_-]{24,256}$/.test(token))return null;
    return new URL('/mobile/pair#pair='+encodeURIComponent(token),url.origin).href;
  }catch{return null;}
}

/** Owner-only setup; no reads or writes occur until an explicit UI action. */
export class MobileOwnerController{
  constructor({read=()=>officeGet(BASE,ownerKey()),probeHost=()=>officePost(BASE+'/setup-status',ownerKey(),{}),
    activateHost=data=>officePost(BASE+'/setup-activate',ownerKey(),data),
    requestLocalConsent=()=>officePost(BASE+'/local-consent',ownerKey(),{}),
    openTailscale=()=>officePost(BASE+'/open-tailscale',ownerKey(),{}),
    create=data=>officePost(BASE+'/pairings',ownerKey(),data),revoke=data=>officePost(BASE+'/revoke',ownerKey(),data),updateScopes=data=>officePost(BASE+'/scopes',ownerKey(),data),changed=()=>{},now=()=>Date.now(),schedule=(...args)=>globalThis.setTimeout(...args),cancel=timer=>globalThis.clearTimeout(timer)}={}){
    Object.assign(this,{read,probeHost,activateHost,requestLocalConsent,openTailscale,create,revoke,updateScopes,changed,now,schedule,cancel});
    this.ownerEntryRequired=false;this.phase='closed';this.epoch=0;this.busy=false;this.snapshot=null;this.pairing=null;this.notice='';this.expiryTimer=null;
    this.hostStep=false;this.host=null;this.consent='idle';this.consentTimer=null;this.consentDeadline=0;this.phoneName='';this.phoneId='';
  }
  emit(){this.changed(this);}
  loseOwnerAccess(){
    this.ownerEntryRequired=true;this.consent='idle';this.snapshot=null;this.host=null;this.phoneName='';this.phoneId='';
    this.clearPairing();this.stopConsentPoll();this.notice=ownerEntryHelp;
  }
  mutationError(error){
    if(ownerAccessDenied(error))this.loseOwnerAccess();
    else this.notice=failure(error);
  }
  requireLocalUpgrade(){
    this.ownerEntryRequired=false;this.snapshot={enabled:false,configured:false,credentials_separated:false};
    this.host=null;this.consent='idle';this.clearPairing();this.notice='';
  }
  stopConsentPoll(){
    if(this.consentTimer!==null)this.cancel(this.consentTimer);
    this.consentTimer=null;
  }
  pollConsent(){
    this.stopConsentPoll();
    if(this.phase==='closed'||!this.hostStep||this.consent!=='pending')return;
    if(this.now()>=this.consentDeadline){this.consent='retry_later';this.emit();return;}
    this.consentTimer=this.schedule(()=>{this.consentTimer=null;void this.refresh();},1800);
    this.consentTimer?.unref?.();
  }
  clearPairing(){if(this.expiryTimer!==null)this.cancel(this.expiryTimer);this.expiryTimer=null;this.pairing=null;}
  scheduleExpiry(){
    if(!this.pairing)return;
    const pairing=this.pairing,remaining=expiryMs(pairing.expires_at)-this.now();
    if(remaining<=0){this.clearPairing();this.notice='配對連結已過期，請重新建立。';this.emit();return;}
    this.expiryTimer=this.schedule(()=>{this.expiryTimer=null;if(this.pairing===pairing)this.scheduleExpiry();},Math.min(remaining,2147483647));this.expiryTimer?.unref?.();
  }
  async open(){
    this.stopConsentPoll();this.ownerEntryRequired=false;this.phase='open';this.epoch++;this.busy=false;this.snapshot=null;
    this.host=null;this.consent='idle';this.phoneName='';this.phoneId='';this.clearPairing();this.notice='';await this.refresh();
  }
  close(){
    this.stopConsentPoll();this.phase='closed';this.epoch++;this.hostStep=false;this.host=null;
    this.consent='idle';this.phoneName='';this.phoneId='';this.clearPairing();this.notice='';this.busy=false;this.emit();
  }
  async refresh(){
    if(this.phase==='closed'||this.busy)return false;
    const epoch=this.epoch;this.busy=true;this.notice='';this.host=null;this.emit();
    try{
      const data=await this.read();if(epoch!==this.epoch)return false;
      if(typeof data?.enabled!=='boolean')throw Error('invalid_status');
      if(!canPair(data)||data.public_origin!==this.snapshot?.public_origin||data.identity!==this.snapshot?.identity)this.clearPairing();
      if(this.phoneId&&data.managed_phone_entry&&!data.pairing_node_ids?.includes(this.phoneId)){
        this.clearPairing();this.phoneId='';this.phoneName='';
      }
      this.ownerEntryRequired=false;this.snapshot=data;
      if(data.credentials_separated===false&&data.local_consent){
        const state=data.local_consent.state;
        if(this.consent!=='pending'&&state==='pending')this.consentDeadline=this.now()+150000;
        this.consent=['opening','opened'].includes(state)?'changed':Object.hasOwn(localInstructions,state)?state:'idle';
      }
    }catch(error){
      if(epoch===this.epoch){
        if(this.consent==='pending'&&(error?.status===401||error?.status===403)){
          this.requireLocalUpgrade();this.consent='changed';
        }else if(error?.code==='mobile_owner_upgrade_required')this.requireLocalUpgrade();
        else if(ownerAccessDenied(error))this.loseOwnerAccess();
        else{this.snapshot=null;this.clearPairing();this.notice=readFailure(error);}
      }return false;
    }
    finally{if(epoch===this.epoch){this.busy=false;this.emit();this.pollConsent();}}
    if(this.hostStep)await this.checkHost();
    return true;
  }
  setHost(result){
    if(result?.state==='legacy_required'){this.requireLocalUpgrade();return;}
    const state=Object.hasOwn(hostInstructions,result?.state)?result.state:'unknown';
    const phones=Array.isArray(result?.phones)?result.phones.filter(p=>typeof p?.id==='string'&&typeof p.name==='string'&&typeof p.selection_token==='string'):[];
    this.host={state,platform:platforms.includes(result?.platform)?result.platform:'other',
      host:typeof result?.host==='string'?result.host:'',reason:result?.reason,phones};
  }
  hostError(error){
    this.clearPairing();
    if(error?.code==='mobile_owner_upgrade_required')this.requireLocalUpgrade();
    else if(ownerAccessDenied(error))this.loseOwnerAccess();
    else {this.host={state:'unknown',phones:[]};this.notice=error?.status===404?'電腦上的 OFFICE 尚未支援畫面內設定。請更新並重新開啟 OFFICE。':readFailure(error);}
  }
  async checkHost(chooseAnother=false){
    const data=this.snapshot;
    if(this.phase==='closed'||!this.hostStep||this.busy||this.host||!data||data.credentials_separated===false)return false;
    if(canPair(data)&&!chooseAnother&&(data.managed_phone_entry!==true||data.pairing_node_ids?.includes(this.phoneId)))return false;
    const epoch=this.epoch;this.busy=true;this.notice='';this.emit();
    try{
      const result=await this.probeHost();if(epoch!==this.epoch)return false;
      this.setHost(result);return true;
    }catch(error){if(epoch===this.epoch)this.hostError(error);return false;}
    finally{if(epoch===this.epoch){this.busy=false;this.emit();}}
  }
  async activatePhone(id){
    if(this.phase==='closed'||!this.hostStep||this.busy||!['select_phone','ready'].includes(this.host?.state))return false;
    const phone=this.host.phones.find(p=>p.id===id);
    if(!phone)return false;
    if(this.phoneId!==id)this.clearPairing();
    if(this.host.state==='ready'&&phone.authorized===true&&canPair(this.snapshot)&&this.snapshot.pairing_node_ids?.includes(id)){
      this.phoneId=id;this.phoneName=phone.name;this.emit();return true;
    }
    const epoch=this.epoch;this.busy=true;this.notice='正在為所選手機設定私人入口…';this.emit();
    let activated=false;
    try{
      const result=await this.activateHost({phone_id:id,selection_token:phone.selection_token});
      if(epoch!==this.epoch)return false;
      this.setHost(result);
      activated=result?.state==='ready';
      if(!activated)this.notice='';
    }catch(error){if(epoch===this.epoch)this.hostError(error);return false;}
    finally{if(epoch===this.epoch){this.busy=false;this.emit();}}
    if(!activated)return false;
    this.phoneId=id;this.phoneName=phone.name;
    const ok=await this.refresh();
    return ok&&canPair(this.snapshot)&&(!this.snapshot.managed_phone_entry||this.snapshot.pairing_node_ids?.includes(id));
  }
  async requestConsent(){
    if(this.phase==='closed'||!this.hostStep||this.busy||this.snapshot?.credentials_separated!==false||['pending','changed'].includes(this.consent))return false;
    const epoch=this.epoch;this.busy=true;this.notice='';this.emit();
    try{
      const result=await this.requestLocalConsent();if(epoch!==this.epoch)return false;
      this.consent=result?.state==='already_upgraded'?'changed':Object.hasOwn(localInstructions,result?.state)?result.state:'unavailable';
      this.consentDeadline=this.now()+150000;return this.consent==='pending';
    }catch(error){if(epoch===this.epoch)this.hostError(error);return false;}
    finally{if(epoch===this.epoch){this.busy=false;this.emit();this.pollConsent();}}
  }
  async launchTailscale(){
    if(this.phase==='closed'||!this.hostStep||this.busy||!['login','connect'].includes(this.host?.state))return false;
    const epoch=this.epoch;this.busy=true;this.notice='';this.emit();
    try{
      const result=await this.openTailscale();if(epoch!==this.epoch)return false;
      if(result?.state==='ready')this.host=null;
      else if(result?.state==='install')this.setHost(result);
      else if(result?.state==='device_approval')this.setHost({...this.host,state:'device_approval'});
      else if(this.host?.platform==='windows')this.notice='在 Windows 工作列右下角按 ∧，右鍵點 Tailscale，選 Log in 或 Connect，完成後回到 OFFICE。';
      else if(this.host?.platform==='macos')this.notice='按螢幕頂部選單列的 Tailscale 圖示，選 Log in 或 Connect，完成後回到 OFFICE。';
      else this.notice='請在 Tailscale 登入或連線，完成後回到 OFFICE。';
    }catch(error){if(epoch===this.epoch)this.hostError(error);return false;}
    finally{if(epoch===this.epoch){this.busy=false;this.emit();}}
    if(!this.host)await this.checkHost();
    return true;
  }
  async pair({identity,label,view_agents,send_agents,view_mode,send_mode}){
    if(this.phase==='closed'||this.busy||!canPair(this.snapshot))return false;
    const scopes=scopePayload(this.snapshot,{view_agents,send_agents,view_mode,send_mode});
    if(this.snapshot.managed_phone_entry&&!this.snapshot.pairing_node_ids?.includes(this.phoneId)){this.notice='請先選擇要配對的手機。';this.emit();return false;}
    if(!identity?.trim()||!label?.trim()||!scopes){this.notice='請填好手機名稱、Tailscale 帳戶，並選擇允許查看的 Agents。';this.emit();return false;}
    const payload={identity:identity.trim(),label:label.trim(),view_agents:scopes.view_agents,send_agents:scopes.send_agents};
    if(this.snapshot.managed_phone_entry)payload.node_id=this.phoneId;
    if(view_mode!==undefined||send_mode!==undefined)Object.assign(payload,{view_mode:scopes.view_mode,send_mode:scopes.send_mode});
    const epoch=this.epoch;let recheckPhone=false;this.busy=true;this.notice='';this.clearPairing();this.emit();
    try{
      const data=await this.create(payload);
      if(epoch!==this.epoch)return false;
      const origin=data.public_origin||this.snapshot.public_origin;
      const link=(!this.snapshot.public_origin||origin===this.snapshot.public_origin)?pairingLink(origin,data.pairing_token):null;
      if(!link||!Number.isFinite(expiryMs(data.expires_at))||expiryMs(data.expires_at)<=this.now()){this.notice='配對資料未能確認或已過期；請重新建立。';return false;}
      this.pairing={url:link,expires_at:data.expires_at};this.scheduleExpiry();this.notice='配對連結只供這部手機使用一次。請勿公開分享。';return true;
    }catch(error){
      if(epoch===this.epoch){
        if(error?.code==='mobile_setup_pending'&&this.snapshot.managed_phone_entry){
          recheckPhone=true;this.phoneId='';this.phoneName='';this.host=null;
          this.notice='手機連線已變更。請確認手機的 Tailscale 顯示 Connected，然後再選擇手機。';
        }else if(error?.code==='mobile_setup_pending'){
          const setup=this.snapshot.setup||{};
          this.snapshot={...this.snapshot,setup:{...setup,configuration_ready:true,pairing_available:false,transport:{...setup.transport,state:'assistant_pending'}}};
          this.host=null;this.notice=pendingMessage;
        }else this.mutationError(error);
      }return false;
    }
    finally{
      if(epoch===this.epoch){
        this.busy=false;this.emit();
        if(recheckPhone)await this.checkHost(true);
      }
    }
  }
  async updateDeviceScopes(device_id,selection){
    const device=this.snapshot?.devices?.find(d=>d.device_id===device_id);
    if(this.phase==='closed'||this.busy||!this.snapshot?.enabled||!device||deviceRevoked(device)||!['ready','renewal_required'].includes(device.state))return false;
    const scopes=scopePayload(this.snapshot,selection);
    if(!scopes){this.notice='請選擇有效的查看及發訊息權限。';this.emit();return false;}
    const epoch=this.epoch;this.busy=true;this.notice='';this.emit();
    try{
      const data=await this.updateScopes({device_id,...scopes});if(epoch!==this.epoch)return false;
      if(data?.device?.device_id!==device_id)throw Error('invalid_mobile_scope');
      this.snapshot={...this.snapshot,devices:this.snapshot.devices.map(d=>d.device_id===device_id?data.device:d)};
      this.notice=data.changed===false?'手機權限沒有變更。':'手機權限已儲存，毋須重新配對。舊權限下尚未開始的指令已取消；已開始的工作不受影響。';return true;
    }catch(error){if(epoch===this.epoch)this.mutationError(error);return false;}
    finally{if(epoch===this.epoch){this.busy=false;this.emit();}}
  }
  async revokeDevice(device_id){
    if(this.phase==='closed'||this.busy||!this.snapshot?.devices?.some(d=>d.device_id===device_id&&!deviceRevoked(d)))return false;
    const epoch=this.epoch;this.busy=true;this.emit();
    try{await this.revoke({device_id});if(epoch!==this.epoch)return false;this.snapshot={...this.snapshot,devices:this.snapshot.devices.map(d=>d.device_id===device_id?{...d,revoked:true}:d)};this.notice='這部手機已撤銷，未派送指令已攔截。已開始的工作及已下載資料不會因此收回。';return true;}
    catch(error){if(epoch===this.epoch)this.mutationError(error);return false;}
    finally{if(epoch===this.epoch){this.busy=false;this.emit();}}
  }
}

const node=(tag,text,className)=>{const n=document.createElement(tag);if(text)n.textContent=text;if(className)n.className=className;return n;};
const labelled=(text,input)=>{const label=node('label',text);label.append(input);return label;};
const select=(options,value)=>{const input=node('select');for(const [id,text] of options){const option=node('option',text);option.value=id;input.append(option);}input.value=value;return input;};
export const mobileEntrySummary=snapshot=>{
  if(!snapshot||typeof snapshot.enabled!=='boolean')return '仍未取得電腦入口狀態。';
  if(snapshot.credentials_separated===false)return localInstructions.idle;
  if(assistantPending(snapshot))return pendingMessage;
  const setup=snapshot?.setup;
  if(setup?.configuration_ready&&setup.local_listener_ready&&setup.transport?.kind==='tsnet'&&setup.transport.running&&setup.transport.listening&&setup.transport.configuration_matches)return '電腦私人入口正在運作。手機是否能連入，仍要掃碼配對後確認。';
  if(setup?.configuration_ready&&setup.pairing_available===false)return '電腦私人入口目前未能使用。先檢查電腦上的 OFFICE 和私人連線服務。';
  if(setup?.configuration_ready===true||!setup&&snapshot?.configured===true)return '私人入口資料已設定；尚未確認手機連線。';
  return '這部電腦的 OFFICE 手機入口尚未設定。';
};
const link=(text,url)=>{const a=node('a',text);a.href=url;a.target='_blank';a.rel='noopener noreferrer';return a;};
const expiry=value=>{const date=new Date(typeof value==='number'?value*1000:value);return Number.isNaN(date.valueOf())?'短時間內有效':date.toLocaleString('zh-HK');};
const downloadPages={windows:'https://tailscale.com/download/windows',macos:'https://tailscale.com/download/mac',linux:'https://tailscale.com/download/linux',other:'https://tailscale.com/download'};
export function initMobileOwner({slot=document.querySelector('#topbar .bhq-right'),controller:provided}={}){
  if(!slot||document.querySelector('meta[name="office-access"]')?.content==='mobile'||new URLSearchParams(location.search).get('mode')==='wallpaper')return null;
  if(slot.querySelector('.mobile-owner-launch'))return null;
  const launch=node('button','手機接入','mobile-owner-launch');launch.type='button';slot.append(launch);
  const dialog=node('dialog',null,'mobile-owner-dialog');dialog.setAttribute('aria-labelledby','mobile-owner-title');
  const close=node('button','×','mobile-owner-close');close.type='button';close.setAttribute('aria-label','關閉手機連接');
  const title=node('h2','連接你的手機');title.id='mobile-owner-title';

  const status=node('p','','mobile-owner-status');status.setAttribute('role','status');
  const modes=node('nav',null,'mobile-owner-modes');modes.setAttribute('aria-label','手機接入方式');
  const firstMode=node('button','首次連接 / 加入手機'),dailyMode=node('button','日常使用');firstMode.type=dailyMode.type='button';modes.append(firstMode,dailyMode);
  const wizard=node('section',null,'mobile-owner-wizard');
  const stepPanels=['在手機設定 Tailscale','選擇手機','選擇 Agent 權限','確認配對'].map(name=>{
    const panel=node('section',null,'mobile-owner-step');panel.setAttribute('aria-label',name);panel.append(node('h3',name));return panel;
  });
  const phoneGuide=node('ol',null,'mobile-owner-checklist');
  const install=node('li','在手機下載官方 Tailscale。');install.append(link('Android · Google Play','https://play.google.com/store/apps/details?id=com.tailscale.ipn'),node('span',' ／ '),link('iPhone · App Store','https://apps.apple.com/app/tailscale/id1470499037'));
  phoneGuide.append(install,node('li','打開 Tailscale，允許加入 VPN，然後用和電腦相同的帳戶登入。'),node('li','按 Connect，看到 Connected 後回到 OFFICE。'));
  const vpnHelp=node('details',null,'mobile-owner-help');vpnHelp.append(node('summary','手機已有其他 VPN，或 Tailscale 連不上？'),node('p','Android／iPhone 通常只能同時啟用一個 VPN。若另一個 VPN 是必要連線，請先向你的網絡管理者確認可用方式，勿直接關掉。這個助手不會修改電腦或手機的 VPN、路由或防火牆。'),link('Tailscale 官方 VPN 相容說明','https://tailscale.com/docs/reference/faq/other-vpns'));
  const phoneNext=node('button','手機已顯示 Connected，下一步','mobile-owner-primary');phoneNext.type='button';
  stepPanels[0].append(phoneGuide,phoneNext,vpnHelp);
  const hostSummary=node('p','','mobile-owner-entry-summary');
  const hostLink=link('','https://tailscale.com/download');
  const button=(text,style='mobile-owner-primary')=>{const b=node('button',text,style);b.type='button';return b;};
  const phonePicker=node('select',null,'mobile-owner-phone-picker'),pickerLabel=labelled('手機',phonePicker);
  const selectionNote=node('p','','mobile-owner-note');
  const activate=button('連接這部手機'),consent=button('在這部電腦確認升級'),openTs=button('開啟 Tailscale');
  const hostCheck=button('重新檢查','mobile-owner-refresh'),phoneHelp=button('在手機設定 Tailscale','mobile-owner-refresh');
  const phoneStores=node('p',null,'mobile-owner-note');
  phoneStores.append(link('在 Android 下載 Tailscale','https://play.google.com/store/apps/details?id=com.tailscale.ipn'),node('span',' ／ '),link('在 iPhone 下載 Tailscale','https://apps.apple.com/app/tailscale/id1470499037'));
  const hostNext=button('選擇 Agent 權限'),another=button('連接另一部手機','mobile-owner-refresh');
  stepPanels[1].append(hostSummary,pickerLabel,selectionNote,activate,consent,hostLink,openTs,hostNext,phoneStores,hostCheck,phoneHelp);
  const daily=node('section',null,'mobile-owner-daily');daily.append(node('h3','下次直接打開 OFFICE'),node('ol',null,'mobile-owner-checklist'));
  daily.children[1].append(node('li','電腦保持開機，OFFICE 與私人連線服務保持運作。'),node('li','手機打開 Tailscale，確認 Connected／已連線，再打開已收藏的 OFFICE 手機網址。'),node('li','配對時可選「記住這部私人手機」。已配對的手機也可在 OFFICE 的「手機登入」中開啟；仍有效的登入不需重掃 QR 碼。啟用後可自動續期；未啟用則最長登入 24 小時、閒置 1 小時後需要重新配對。'),node('li','已撤銷、超過記住期限、超過 7 天未使用，或清除了瀏覽器資料，就回到電腦重新配對。'));
  const everydayLink=node('a','開啟手機版 OFFICE');everydayLink.target='_blank';everydayLink.rel='noopener noreferrer';
  const everydayCopy=node('button','複製日常手機網址','mobile-owner-copy');everydayCopy.type='button';
  daily.append(everydayLink,everydayCopy,node('p','請收藏進入 OFFICE 後的普通網址；一次性配對連結只供首次使用。換瀏覽器、無痕模式或清除網站資料，不能沿用另一個瀏覽器的登入。','mobile-owner-note'));
  const troubleshooting=node('details',null,'mobile-owner-help');troubleshooting.append(node('summary','打不開、登入過期或收不到訊息？'),node('p','連線逾時：先檢查手機 Tailscale 的 Connected 狀態、手機與電腦的網絡，以及電腦有否休眠。登入失效／已撤銷：在電腦重新配對。能看 OFFICE 但不能發訊息：檢查這部手機的發訊息權限，以及該 Agent 的接收入口。'),node('p','配對只證明手機能進入 OFFICE。要驗證 Agent 收發，請選一隻已授權的 Agent 發一則簡短訊息，確認它的實際回覆；接收狀態不等於已完成工作。'));daily.append(troubleshooting);
  wizard.append(...stepPanels);
  const form=node('form');const label=node('input');label.type='text';label.maxLength=80;label.placeholder='例如：我的手機';label.autocomplete='off';label.required=true;
  const identity=node('input');identity.type='text';identity.maxLength=254;identity.placeholder='手機登入 Tailscale 所用的帳戶';identity.autocomplete='off';identity.required=true;
  const scopeTitle=node('h3','允許這部手機使用哪些 Agents？');const scopes=node('div',null,'mobile-owner-scopes');
  const followView=node('input'),followSend=node('input');followView.type=followSend.type='checkbox';followView.checked=followSend.checked=false;
  const scopeNote=node('p','可分開設定查看及發訊息權限。同步所有已登記 Agents 會跟隨 OFFICE 入場／登出名單；發訊息仍沿用該 Agent 的工具及批准規則。','mobile-owner-note');
  const create=node('button','顯示手機配對 QR 碼','mobile-owner-primary');create.type='submit';
  const identityHelp=node('details',null,'mobile-owner-help');identityHelp.append(node('summary','使用哪個 Tailscale 帳戶？'),node('p','手機必須使用這個電腦入口指定的帳戶。已設定時會自動填入，不需重新輸入。'),labelled('Tailscale 帳戶',identity));
  form.append(labelled('手機名稱',label),identityHelp,scopeTitle,labelled('同步所有已登記 Agents',followView),labelled('可向所有已登記 Agents 發訊息',followSend),scopes,scopeNote,create);
  const pairPanel=node('section',null,'mobile-owner-pair');pairPanel.hidden=true;
  const pairLabel=node('label','在已連接 Tailscale 的手機開啟');const pairText=node('textarea');pairText.readOnly=true;pairText.rows=3;pairText.setAttribute('aria-label','一次性手機配對連結');pairText.autocomplete='off';pairLabel.append(pairText);
  const qrPanel=node('div',null,'mobile-owner-qr');qrPanel.hidden=true;const qrCanvas=node('canvas');qrCanvas.setAttribute('role','img');qrCanvas.setAttribute('aria-label','一次性手機配對 QR 碼');const qrCaption=node('p','用手機相機掃描（不用在 Tailscale 內掃），以 Chrome／Safari 開啟，再按「配對並進入 OFFICE」。請保持 Tailscale 顯示 Connected。','mobile-owner-note');qrPanel.append(qrCanvas,qrCaption);
  const qrFallback=node('p','','mobile-owner-note');qrFallback.hidden=true;
  const qr=new MobilePairingQR({draw:(encode,url)=>{drawPairingQR(qrCanvas,encode,url);qrPanel.hidden=false;qrFallback.hidden=true;},clear:()=>{qrCanvas.width=qrCanvas.height=0;qrPanel.hidden=true;qrFallback.hidden=true;},unavailable:()=>{qrFallback.textContent='QR 碼暫時未能顯示，仍可複製下方私人配對連結。';qrFallback.hidden=false;}});
  const pairExpiry=node('p','','mobile-owner-note');const copy=node('button','複製私人配對連結','mobile-owner-copy');copy.type='button';pairPanel.append(qrPanel,qrFallback,pairLabel,pairExpiry,copy);
  const devicesTitle=node('h3','已配對手機');const devices=node('div',null,'mobile-owner-devices');const refresh=node('button','重新檢查','mobile-owner-refresh');refresh.type='button';
  const checkPair=node('button','手機已按配對，檢查結果','mobile-owner-primary');checkPair.type='button';
  stepPanels[2].append(another,form,pairPanel);
  stepPanels[3].append(node('p','先在手機按「配對並進入 OFFICE」，再檢查下方已配對手機。若仍未出現，確認你掃的是目前有效的 QR 碼。'),checkPair,node('p','看到已配對手機代表伺服器已接受登入；Agent 的回覆仍要另行實測。','mobile-owner-note'));
  const pairNext=node('button','已在手機配對，查看結果','mobile-owner-refresh');pairNext.type='button';pairPanel.append(pairNext);
  dialog.append(close,title,modes,status,wizard,daily,devicesTitle,devices,refresh);document.body.append(dialog);
  let activeStep=1,activeMode='first',modeChosen=false,forceWizard=false,returnCheck=false,phoneListSignature='';
  let draftPhoneId='';
  const resetPhoneScopes=()=>{draftPhoneId='';label.value='';followView.checked=followSend.checked=false;scopeSignature='';};
  const showDevices=()=>{devicesTitle.hidden=devices.hidden=refresh.hidden=activeMode!=='daily'&&activeStep!==3;};
  const showStep=(index,check=true)=>{
    activeStep=index;showDevices();stepPanels.forEach((panel,i)=>{panel.hidden=i!==index;});
    controller.hostStep=activeMode==='first'&&activeStep===1;
    if(controller.hostStep&&check)return controller.checkHost();
  };
  const showMode=mode=>{activeMode=mode;controller.hostStep=mode==='first'&&activeStep===1;showDevices();wizard.hidden=mode!=='first';daily.hidden=mode!=='daily';firstMode.setAttribute('aria-pressed',mode==='first'?'true':'false');dailyMode.setAttribute('aria-pressed',mode==='daily'?'true':'false');};
  const needsSelection=snapshot=>snapshot?.managed_phone_entry===true&&!snapshot.pairing_node_ids?.includes(controller.phoneId);
  const initialStep=snapshot=>canPair(snapshot)&&!needsSelection(snapshot)?2:1;
  firstMode.addEventListener('click',()=>{modeChosen=true;showMode('first');return showStep(initialStep(controller.snapshot));});
  dailyMode.addEventListener('click',()=>{modeChosen=true;showMode('daily');});
  phoneNext.addEventListener('click',()=>{showStep(initialStep(controller.snapshot),false);return controller.refresh();});
  hostNext.addEventListener('click',()=>showStep(2));
  phoneHelp.addEventListener('click',()=>showStep(0));
  another.addEventListener('click',()=>{
    controller.clearPairing();controller.phoneId='';controller.phoneName='';controller.host=null;
    resetPhoneScopes();
    showStep(1,false);return controller.checkHost(true);
  });
  consent.addEventListener('click',()=>controller.requestConsent());
  openTs.addEventListener('click',()=>{returnCheck=true;return controller.launchTailscale();});
  hostLink.addEventListener('click',()=>{returnCheck=true;});
  const resumeOnFocus=()=>{
    if(!returnCheck||!controller.hostStep||controller.phase==='closed'||document.visibilityState==='hidden')return;
    if(controller.busy)return;
    returnCheck=false;void controller.refresh();
  };
  globalThis.addEventListener?.('focus',resumeOnFocus);
  document.addEventListener?.('visibilitychange',resumeOnFocus);
  activate.addEventListener('click',async()=>{
    if(await controller.activatePhone(phonePicker.value)){
      if(draftPhoneId&&draftPhoneId!==controller.phoneId)resetPhoneScopes();
      draftPhoneId=controller.phoneId;label.value=controller.phoneName;showStep(2);render(controller);
    }
  });
  let scopeSignature='';const choices=new Map(),deviceChoices=new Map();let identityTouched=false;identity.addEventListener('input',()=>{identityTouched=true;});
  const renderPairChoices=busy=>{
    followView.disabled=followSend.disabled=busy;
    for(const entry of choices.values()){
      entry.view.checked=followView.checked||entry.selectedView;entry.send.checked=followSend.checked||entry.selectedSend;
      entry.view.disabled=busy||followView.checked;entry.send.disabled=busy||followSend.checked;
    }
  };
  followView.addEventListener('change',()=>{if(!followView.checked)followSend.checked=false;renderPairChoices(controller.busy);});
  followSend.addEventListener('change',()=>{if(followSend.checked)followView.checked=true;renderPairChoices(controller.busy);});
  const render=controller=>{
    status.textContent=controller.notice|| (controller.busy?'正在處理…':'');
    if(controller.phase==='closed'){pairText.value='';pairPanel.hidden=true;qr.reset();return;}
    if(!controller.snapshot){
      modes.hidden=wizard.hidden=daily.hidden=devicesTitle.hidden=devices.hidden=true;
      refresh.hidden=controller.ownerEntryRequired||controller.busy;refresh.disabled=controller.busy;
      pairText.value='';pairPanel.hidden=true;qr.reset();
      if(controller.ownerEntryRequired){
        resetPhoneScopes();identity.value='';identityTouched=false;deviceChoices.clear();devices.replaceChildren();
      }
      return;
    }
    showMode(activeMode);
    const snapshot=controller.snapshot,pairReady=canPair(snapshot),host=controller.host;
    if(!modeChosen&&controller.snapshot){
      const hasPhone=(snapshot.devices||[]).some(d=>!deviceRevoked(d)&&['ready','renewal_required'].includes(d.state));
      showMode(hasPhone&&!forceWizard?'daily':'first');showStep(initialStep(snapshot),false);modeChosen=true;
    }
    modes.hidden=!(snapshot.devices||[]).length;
    create.disabled=controller.busy||!pairReady||needsSelection(snapshot);
    refresh.disabled=hostCheck.disabled=checkPair.disabled=controller.busy;
    if(activeMode==='first'&&activeStep===2&&(!pairReady||needsSelection(snapshot)))showStep(1,false);
    const legacy=snapshot.credentials_separated===false,phones=host?.phones||[];
    hostSummary.textContent=legacy?localInstructions[controller.consent]||localInstructions.idle:host?hostInstructions[host.state]:mobileEntrySummary(controller.snapshot);
    const choosing=!legacy&&['ready','select_phone'].includes(host?.state)&&phones.length>0;
    if(host?.state==='conflict'&&host.reason==='mobile_listener_unavailable')hostSummary.textContent='電腦未能開啟 OFFICE 專用的手機入口，可能正由另一個本機服務使用。請稍後重新檢查。';
    if(host?.state==='ready'&&!phones.length&&snapshot.managed_phone_entry)hostSummary.textContent=hostInstructions.no_phone;
    const signature=JSON.stringify(phones.map(p=>[p.id,p.name]));
    if(signature!==phoneListSignature){
      const previous=phonePicker.value;phoneListSignature=signature;
      phonePicker.replaceChildren();
      if(phones.length>1){const option=node('option','請選擇一部手機');option.value='';phonePicker.append(option);}
      for(const phone of phones){const option=node('option',phone.name);option.value=phone.id;phonePicker.append(option);}
      phonePicker.value=phones.some(p=>p.id===previous)?previous:phones.length===1?phones[0].id:'';
    }
    pickerLabel.hidden=!choosing;phonePicker.disabled=controller.busy;
    const selected=phones.find(p=>p.id===phonePicker.value);
    activate.hidden=!choosing;activate.disabled=controller.busy||!selected;
    const canReuse=host?.state==='ready'&&selected?.authorized===true&&pairReady&&snapshot.pairing_node_ids?.includes(selected.id);
    activate.textContent=canReuse?'使用這部手機':'連接這部手機';
    selectionNote.hidden=!choosing||canReuse;
    selectionNote.textContent='沿用私人網絡現有規則。未獲授權的裝置即使能連到這個私人連接埠，也不能進入 OFFICE。'+(host?.host?'HTTPS 憑證的公開記錄會包含電腦網域 '+host.host+'。':'');
    if(host?.state==='https_required'&&host.host)hostSummary.textContent+=' 啟用後，電腦名稱 '+host.host+' 可能列入公開憑證記錄。';
    consent.hidden=!legacy||['changed','pending'].includes(controller.consent);consent.disabled=controller.busy;
    openTs.hidden=legacy||!['login','connect'].includes(host?.state);openTs.disabled=controller.busy;
    hostLink.hidden=true;
    if(!legacy&&host){
      const official={
        install:['下載電腦版 Tailscale',downloadPages[host.platform]],
        update_required:['更新 Tailscale',downloadPages[host.platform]],
        device_approval:['開啟 Tailscale 裝置列表','https://login.tailscale.com/admin/machines'],
        https_required:['開啟 Tailscale HTTPS 設定','https://login.tailscale.com/admin/dns'],
        dns_required:['開啟 Tailscale DNS 設定','https://login.tailscale.com/admin/dns'],
      }[host.state];
      if(official){hostLink.textContent=official[0];hostLink.href=official[1];hostLink.hidden=false;}
    }
    hostCheck.hidden=legacy||choosing||(!host&&controller.busy);
    phoneStores.hidden=legacy||!(host?.state==='no_phone'||host?.state==='ready'&&!phones.length);
    phoneHelp.hidden=legacy||choosing||!host||host.state==='ready'&&phones.length>0;
    hostNext.hidden=legacy||choosing||!pairReady||needsSelection(snapshot);hostNext.disabled=controller.busy;
    another.hidden=snapshot.managed_phone_entry!==true;another.disabled=controller.busy;
    const normalURL=(()=>{try{const u=new URL(snapshot.public_origin);return u.protocol==='https:'&&!u.username&&!u.password&&u.pathname==='/'&&!u.search&&!u.hash?new URL('/mobile/',u.origin).href:'';}catch{return '';}})();
    everydayLink.href=normalURL||'#';everydayLink.hidden=!normalURL;everydayCopy.disabled=!normalURL;everydayCopy.datasetUrl=normalURL;
    if(!identityTouched&&snapshot.identity)identity.value=snapshot.identity;
    const agents=Array.isArray(snapshot.agents)?snapshot.agents:[];const sig=JSON.stringify(agents.map(a=>[a.id,a.name]));
    if(sig!==scopeSignature){scopeSignature=sig;choices.clear();scopes.replaceChildren();
      if(!agents.length)scopes.append(node('p','目前沒有已登記的 Agents。可選擇同步所有已登記 Agents，之後入場時自動顯示。','mobile-owner-note'));
      for(const agent of agents){const row=node('div',null,'mobile-owner-agent'),name=node('span',agent.name||agent.id),view=node('input'),send=node('input');view.type=send.type='checkbox';view.checked=send.checked=false;
        const entry={view,send,selectedView:false,selectedSend:false};
        view.addEventListener('change',()=>{entry.selectedView=view.checked;if(!view.checked)entry.selectedSend=false;renderPairChoices(controller.busy);});
        send.addEventListener('change',()=>{entry.selectedSend=send.checked;if(send.checked)entry.selectedView=true;renderPairChoices(controller.busy);});
        row.append(name,labelled('查看',view),labelled('發訊息',send));scopes.append(row);choices.set(agent.id,entry);}
    }
    renderPairChoices(controller.busy);
    pairPanel.hidden=!controller.pairing;pairText.value=controller.pairing?.url||'';pairExpiry.textContent=controller.pairing?'有效期至 '+expiry(controller.pairing.expires_at):'';
    void qr.show(controller.pairing);
    devices.replaceChildren();for(const device of snapshot.devices||[]){const revoked=deviceRevoked(device);
      // Server state includes idle expiry, even before the absolute login deadline.
      const message=revoked?'已撤銷，這部手機不能再登入或續期':device.state==='expired'?'已過期，請重新配對 · 最長登入期限原為 '+expiry(device.expires_at):device.state==='renewal_required'?'已記住這部手機；用原瀏覽器重開 OFFICE 會嘗試續期。最長至 '+expiry(device.renewal_expires_at):'登入有效期至 '+expiry(device.expires_at)+(device.remembered?' · 可自動續期，最長至 '+expiry(device.renewal_expires_at):' · 未啟用記住手機');
      const row=node('div',null,'mobile-owner-device'),description=node('div'),name=node('strong',device.label||'已配對手機'),detail=node('small',message);description.append(name,detail);row.append(description);
      if(!revoked&&['ready','renewal_required'].includes(device.state)){
        const signature=JSON.stringify([device.revision,device.view_mode,device.send_mode,device.view_agents,device.send_agents]);
        let draft=deviceChoices.get(device.device_id);
        if(!draft||draft.signature!==signature){draft={signature,view:device.view_mode==='enrolled'?'enrolled':'selected',send:device.send_mode==='enrolled'?'enrolled':device.send_agents?.length?'selected':'none'};deviceChoices.set(device.device_id,draft);}
        const view=select([['selected','只保留目前查看名單'],['enrolled','同步所有已登記 Agents']],draft.view);
        const send=select([['selected','只保留目前可發訊息名單'],['enrolled','可向所有已登記 Agents 發訊息'],['none','不允許發訊息']],draft.send);
        view.disabled=send.disabled=controller.busy;
        view.addEventListener('change',()=>{draft.view=view.value;if(view.value==='selected'&&send.value==='enrolled')draft.send=send.value='selected';});
        send.addEventListener('change',()=>{draft.send=send.value;if(send.value==='enrolled')draft.view=view.value='enrolled';});
        const save=node('button','儲存權限');save.type='button';save.disabled=controller.busy||snapshot.enabled!==true;
        save.addEventListener('click',()=>{const available=new Set(agents.map(a=>a.id));return controller.updateDeviceScopes(device.device_id,{
          view_mode:draft.view,send_mode:draft.send==='enrolled'?'enrolled':'selected',
          view_agents:draft.view==='selected'?(device.view_agents||[]).filter(id=>available.has(id)):[],
          send_agents:draft.send==='selected'?(device.send_agents||[]).filter(id=>available.has(id)):[],
        });});
        description.append(labelled('查看範圍',view),labelled('發訊息範圍',send),node('small','儲存後即時生效，毋須重新配對；未開始的舊指令會取消。'),save);
      }
      if(!revoked){const revoke=node('button','撤銷');revoke.type='button';revoke.disabled=controller.busy;revoke.addEventListener('click',()=>controller.revokeDevice(device.device_id));row.append(revoke);}devices.append(row);}
    if(!snapshot.devices?.length)devices.append(node('p',controller.snapshot?'尚未配對手機。':'仍未取得手機名單。','mobile-owner-note'));
  };
  const controller=provided||new MobileOwnerController({changed:render});if(provided){const before=provided.changed;provided.changed=value=>{before?.(value);render(value);};}
  phonePicker.addEventListener('change',()=>render(controller));
  showStep(1,false);showMode('first');
  const openDialog=resume=>{
    modeChosen=false;forceWizard=resume;returnCheck=false;phoneListSignature='';showStep(1,false);showMode('first');
    resetPhoneScopes();deviceChoices.clear();identityTouched=false;identity.value='';dialog.showModal();return controller.open();
  };
  launch.addEventListener('click',()=>openDialog(false));close.addEventListener('click',()=>dialog.close());dialog.addEventListener('close',()=>{returnCheck=false;controller.close();});
  refresh.addEventListener('click',()=>controller.refresh());hostCheck.addEventListener('click',()=>controller.refresh());
  checkPair.addEventListener('click',()=>controller.refresh());pairNext.addEventListener('click',()=>{showStep(3);return controller.refresh();});
  everydayCopy.addEventListener('click',async()=>{if(!everydayCopy.datasetUrl)return;try{await navigator.clipboard.writeText(everydayCopy.datasetUrl);status.textContent='已複製日常手機網址；需要 Tailscale 連線和有效手機登入。';}catch{status.textContent='請使用「開啟手機版 OFFICE」連結，並在手機瀏覽器收藏。';}});
  form.addEventListener('submit',event=>{event.preventDefault();controller.pair({identity:identity.value,label:label.value,view_mode:followView.checked?'enrolled':'selected',send_mode:followSend.checked?'enrolled':'selected',view_agents:[...choices].filter(([,c])=>c.selectedView).map(([id])=>id),send_agents:[...choices].filter(([,c])=>c.selectedSend).map(([id])=>id)});});
  copy.addEventListener('click',async()=>{const link=controller.pairing?.url;if(!link)return;try{await navigator.clipboard.writeText(link);status.textContent='已複製。請只傳送到你自己的手機。';}catch{pairText.focus();pairText.select();status.textContent='請複製已選取的配對連結。';}});
  if(new URLSearchParams(location.search).get('mobile-setup')==='1')void openDialog(true);
  return {controller,dialog,launch,dispose(){globalThis.removeEventListener?.('focus',resumeOnFocus);document.removeEventListener?.('visibilitychange',resumeOnFocus);controller.close();dialog.remove();launch.remove();}};
}
