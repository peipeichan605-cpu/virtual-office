import {officeGet,officePost} from './office-api.js';

const STATUS='/api/office/cursor/local/status',CREATE='/api/office/cursor/local/create';
const messages={
  private_office_required:'請從你的私人 OFFICE 入口開啟。',
  invalid_name:'請填寫 1 至 80 字的對話名稱。',name_in_use:'這個名稱已有人使用，請換一個名字。',
  invalid_workspace:'請選擇可使用的工作資料夾。',workspace_not_found:'找不到這個工作資料夾；需要時可勾選建立資料夾。',
  cursor_sdk_unavailable:'這個 OFFICE 尚未完成 Cursor SDK 設定，請完成安裝後重新檢查。',
  cursor_sdk_missing:'這個 OFFICE 尚未完成 Cursor SDK 設定，請完成安裝後重新檢查。',
  cursor_sdk_required:'這個 OFFICE 尚未完成 Cursor SDK 設定，請完成安裝後重新檢查。',
  cursor_python_required:'這個 OFFICE 需要先完成支援 Cursor SDK 的執行環境設定。',
  cursor_credentials_required:'請填寫這個 OFFICE 使用的 Cursor API key。',
  cursor_connection_details_required:'請填寫 Cursor API key 及可使用的對話資料夾。',
  platform_auth_required:'Cursor 帳戶驗證未通過，請核對連接資料。',
  platform_access_denied:'這個 Cursor 帳戶未獲准使用 SDK，請核對帳戶權限。',
  cursor_creation_pending:'這次建立仍在處理中，請核對結果；不會另開一條對話。',
  cursor_creation_unknown:'建立結果未確認，請核對結果；不會自動重做。',
  request_id_conflict:'這次建立的資料不一致，請先核對 OFFICE 已有的對話。',
  agent_membership_changed:'上一條對話已登出或身份已更改。舊紀錄保留，需要時可建立新對話。',
  browser_storage_unavailable:'瀏覽器暫時無法保存建立編號。請允許此頁的分頁儲存後再試；尚未建立新對話。',
};
export function cursorSdkError(error){return messages[error?.code||error?.message]||'暫時未能完成 Cursor 設定，請稍後核對。';}
const uuid=value=>typeof value==='string'&&/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
export function cursorSdkRequestStore(getStorage,scope){
  const key='office-cursor-sdk-create:'+scope;
  return {
    load(){const value=getStorage().getItem(key);return uuid(value)?value:null;},
    save(id){if(!uuid(id))throw new Error('invalid_request_identity');getStorage().setItem(key,id);},
    clear(id){const storage=getStorage();if(storage.getItem(key)===id)storage.removeItem(key);},
  };
}
function publicHome(data={}){
  return {sdk_available:data.sdk_available===true,credential_configured:data.credential_configured===true,
    workspace:typeof data.workspace==='string'?data.workspace:'',
    workspaces:(Array.isArray(data.workspaces)?data.workspaces:[]).filter(row=>typeof row?.path==='string').slice(0,200).map(row=>({name:String(row.name||row.path),path:row.path}))};
}
function createdSession(data){
  return typeof data?.agent_id==='string'&&data.agent_id.startsWith('cursor:')
    ?{agent_id:data.agent_id,name:typeof data.name==='string'?data.name:'Cursor SDK',scope:'office_owned_session'}:null;
}

/** No keys or prompts are stored in controller snapshots or browser storage. */
export class CursorSdkController{
  constructor({transport,pendingStore,makeId=()=>crypto.randomUUID(),onChange=()=>{},onCreated=()=>{}}){
    Object.assign(this,{transport,pendingStore,makeId,onChange,onCreated});this.epoch=0;this.revision=0;this.visible=false;this.phase='closed';this.home=null;this.error='';this.requestId=null;this.uncertain=false;this.creating=false;this.created=null;
  }
  emit(){this.onChange(this);}
  current(epoch){return this.visible&&this.epoch===epoch;}
  async open(){
    this.visible=true;this.epoch++;this.created=null;this.error='';
    try{const id=this.pendingStore.load();if(id){this.requestId=id;this.uncertain=true;}}
    catch{this.error=messages.browser_storage_unavailable;}
    await this.refresh();
  }
  forgetRequest(id){try{this.pendingStore.clear(id);}catch{/* A completed receipt can safely be read again after reload. */}}
  close(){this.visible=false;this.epoch++;this.revision++;this.phase='closed';this.error='';this.emit();}
  async refresh(){
    if(!this.visible)return;
    const epoch=this.epoch,revision=++this.revision,current=()=>this.current(epoch)&&revision===this.revision;this.phase='loading';this.error='';this.emit();
    try{
      const data=await this.transport.read(this.uncertain?this.requestId:null);
      if(!current())return;
      this.home=publicHome(data);
      const receipt=data.creation;
      if(this.uncertain&&receipt?.request_id===this.requestId){
        if(receipt.state==='completed'){
          const session=createdSession(receipt);
          if(session){this.forgetRequest(this.requestId);this.uncertain=false;this.created=session;this.phase='created';this.onCreated(session,{focus:true});this.emit();return;}
        }else if(['failed','not_found','revoked'].includes(receipt.state)){
          this.forgetRequest(this.requestId);this.uncertain=false;this.requestId=null;
          if(receipt.state!=='not_found')this.error=cursorSdkError({code:receipt.reason});
        }
      }
      this.phase=this.creating?'creating':this.uncertain?'unknown':this.home.sdk_available?'ready':'unavailable';
    }catch(error){if(!current())return;this.phase=this.uncertain?'unknown':'failed';this.error=cursorSdkError(error);}
    if(current())this.emit();
  }
  async create(values){
    if(!this.visible||this.creating||this.uncertain||this.phase!=='ready'||!this.home?.sdk_available)return false;
    const name=String(values.name||'').trim(),workspace=String(values.workspace||'').trim();
    if(!name||name.length>80){this.error=messages.invalid_name;this.emit();return false;}
    if(!workspace){this.error=messages.invalid_workspace;this.emit();return false;}
    if(!this.home.credential_configured&&!String(values.api_key||'').trim()){this.error=messages.cursor_credentials_required;this.emit();return false;}
    try{
      const pending=this.pendingStore.load();
      if(pending){this.requestId=pending;this.uncertain=true;this.phase='unknown';this.error=messages.cursor_creation_unknown;this.emit();return false;}
      this.requestId=this.makeId();this.pendingStore.save(this.requestId);
    }catch{this.error=messages.browser_storage_unavailable;this.emit();return false;}
    const epoch=this.epoch,requestId=this.requestId;
    const body={name,workspace,request_id:requestId};
    if(values.create_folder===true)body.create_folder=true;
    if(String(values.api_key||'').trim())body.api_key=String(values.api_key).trim();
    this.revision++;this.creating=true;this.phase='creating';this.error='';this.emit();
    try{
      const data=await this.transport.create(body),session=createdSession(data);
      if(!session)throw Object.assign(new Error('invalid_creation_receipt'),{status:502});
      if(this.current(epoch))this.revision++;
      this.forgetRequest(requestId);this.uncertain=false;this.created=session;
      this.onCreated(session,{focus:this.current(epoch)});
      if(this.current(epoch))this.phase='created';
      return true;
    }catch(error){
      if(this.current(epoch))this.revision++;
      this.uncertain=!error.status||error.status>=500||['cursor_creation_pending','cursor_creation_unknown','request_id_conflict'].includes(error.code||error.message);
      if(!this.uncertain)this.forgetRequest(requestId);
      if(this.current(epoch)){this.phase=this.uncertain?'unknown':'ready';this.error=this.uncertain?messages.cursor_creation_unknown:cursorSdkError(error);}
      return false;
    }finally{
      delete body.api_key;this.creating=false;
      if(this.current(epoch))this.emit();
      else if(this.visible){this.phase=this.uncertain?'unknown':this.home?.sdk_available?'ready':'unavailable';this.emit();}
    }
  }
}

/** Explicitly opened from “New conversation”; never shown on Office entry. */
export function initCursorSdkUI({document:doc=document,events=window,key=()=>decodeURIComponent(location.hash.slice(1)),pendingStore,openEvent='office:new-conversation'}={}){
  pendingStore ||= cursorSdkRequestStore(()=>sessionStorage,location.origin+location.pathname.replace(/[^/]*$/,''));
  const dialog=doc.createElement('dialog');dialog.className='office-invite-dialog cursor-sdk-dialog';dialog.setAttribute('aria-labelledby','cursor-sdk-heading');
  dialog.innerHTML=`<button class="invite-close" type="button" aria-label="關閉新對話">×</button>
    <div class="invite-eyebrow">VIRTUAL OFFICE / CURSOR SDK</div><h2 id="cursor-sdk-heading">Cursor SDK · 純對話</h2>
    <p class="invite-intro">登記由 OFFICE 管理的純對話，傳送訊息時建立或續接原生 SDK session。目前不開放本機工具；要保留已有的 Cursor 聊天，請選「接入現有 App／Cloud Agent」。</p>
    <p class="cursor-sdk-status" role="status" aria-live="polite"></p>
    <form class="invite-form" hidden><label>對話名稱<input name="name" maxlength="80" required autocomplete="off" placeholder="例如：專案助手"></label>
      <label>對話資料夾<select name="known"><option value="">自行輸入路徑</option></select></label>
      <label>對話資料夾路徑<input name="workspace" required autocomplete="off" placeholder="輸入對話資料夾的絕對路徑"></label>
      <label class="cursor-sdk-create-folder"><input name="create_folder" type="checkbox"> 資料夾未存在時建立</label>
      <p class="cursor-sdk-saved invite-footnote" hidden>這個 OFFICE 已保存 Cursor 連接資料，其他 SDK 對話可以沿用。</p>
      <button class="cursor-sdk-change-key invite-resume" type="button" hidden>更新 Cursor API key</button>
      <label class="cursor-sdk-key">Cursor API key<input name="api_key" type="password" autocomplete="off" placeholder="只需設定一次"></label>
      <p class="invite-footnote">連接資料只保存在這個 OFFICE 的私人設定，不會放入瀏覽器儲存空間。只建立你選擇的對話，不會掃描或匯入其他 Agents。</p>
      <button class="invite-primary" type="submit">登記對話</button></form>
    <button class="cursor-sdk-refresh invite-primary" type="button" hidden>重新檢查</button>
    <section class="cursor-sdk-result" hidden><p>返回辦公室後，點新角色頭頂即可輸入訊息；傳送時才建立或續接原生 SDK session。收到實際回覆及完成紀錄後，才算驗證收發成功。</p><button class="invite-primary cursor-sdk-open" type="button">查看新 Agent</button></section>
    <p class="invite-notice" role="alert"></p>`;
  doc.body.append(dialog);
  const form=dialog.querySelector('form'),notice=dialog.querySelector('.invite-notice'),status=dialog.querySelector('.cursor-sdk-status'),submit=form.querySelector('button[type="submit"]'),refresh=dialog.querySelector('.cursor-sdk-refresh'),result=dialog.querySelector('.cursor-sdk-result'),saved=dialog.querySelector('.cursor-sdk-saved'),keyRow=dialog.querySelector('.cursor-sdk-key'),changeKey=dialog.querySelector('.cursor-sdk-change-key');
  let homeSignature='',showKey=false;
  const dispatch=(name,detail)=>events.dispatchEvent(new CustomEvent(name,{detail}));
  const controller=new CursorSdkController({pendingStore,transport:{
    read:requestId=>officeGet(STATUS+(requestId?'?'+new URLSearchParams({request_id:requestId}):''),key()),
    create:body=>officePost(CREATE,key(),body),
  },onCreated:(session,options)=>{dispatch('office:refresh');if(options.focus)dispatch('office:cursor-sdk-created',session);},onChange:paint});
  function paint(state){
    const ready=state.phase==='ready',creating=state.phase==='creating';
    form.hidden=!['ready','creating'].includes(state.phase);result.hidden=state.phase!=='created';
    refresh.hidden=!['unavailable','unknown','failed'].includes(state.phase);refresh.textContent=state.phase==='unknown'?'核對建立結果':'重新檢查';
    status.textContent=({loading:'正在檢查這個 OFFICE 的 Cursor SDK…',ready:'選擇對話資料夾，登記一條新對話。',creating:'正在登記對話，請稍候…',unavailable:'尚未完成 Cursor SDK 設定。請先讓你的安裝 Agent 完成這個 OFFICE 的 SDK 設定，再重新檢查。',unknown:'登記結果未確認。請核對結果，不會自動另開一條。',created:'已登記 Cursor SDK 對話；原生收發尚待實際回覆驗證。',failed:'暫時未能讀取 Cursor SDK 設定。'}[state.phase]||'');
    notice.textContent=state.error;
    for(const input of form.elements)input.disabled=!ready;
    submit.textContent=creating?'登記中…':'登記對話';
    const signature=JSON.stringify(state.home);
    if(signature!==homeSignature&&state.home){
      homeSignature=signature;const known=form.elements.known;known.replaceChildren();
      const custom=doc.createElement('option');custom.value='';custom.textContent='自行輸入路徑';known.append(custom);
      for(const row of state.home.workspaces){const option=doc.createElement('option');option.value=row.path;option.textContent=row.name;known.append(option);}
      if(!form.elements.workspace.value)form.elements.workspace.value=state.home.workspace;
      known.value=[...known.options].some(o=>o.value===form.elements.workspace.value)?form.elements.workspace.value:'';
    }
    const configured=state.home?.credential_configured===true;
    saved.hidden=!configured;changeKey.hidden=!configured||showKey;keyRow.hidden=configured&&!showKey;form.elements.api_key.required=!configured;
  }
  const close=()=>{form.elements.api_key.value='';controller.close();if(dialog.open)dialog.close();};
  dialog.querySelector('.invite-close').addEventListener('click',close);
  dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
  dialog.addEventListener('close',()=>{form.elements.api_key.value='';if(controller.visible)controller.close();});
  form.elements.known.addEventListener('change',()=>{if(form.elements.known.value)form.elements.workspace.value=form.elements.known.value;});
  changeKey.addEventListener('click',()=>{showKey=true;paint(controller);form.elements.api_key.focus();});
  refresh.addEventListener('click',()=>controller.refresh());
  form.addEventListener('submit',event=>{
    event.preventDefault();const values={name:form.elements.name.value,workspace:form.elements.workspace.value,create_folder:form.elements.create_folder.checked,api_key:form.elements.api_key.value};
    controller.create(values);form.elements.api_key.value='';delete values.api_key;
  });
  dialog.querySelector('.cursor-sdk-open').addEventListener('click',()=>{const created=controller.created;close();if(created)dispatch('office:cursor-sdk-created',created);});
  const open=()=>{
    if(dialog.open)return;
    form.reset();showKey=false;homeSignature='';form.elements.api_key.value='';dialog.showModal();controller.open();
  };
  events.addEventListener(openEvent,open);
  return {dialog,controller,dispose(){events.removeEventListener(openEvent,open);close();dialog.remove();}};
}
