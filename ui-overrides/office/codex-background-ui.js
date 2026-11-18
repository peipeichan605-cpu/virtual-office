import {officeGet,officePost} from './office-api.js';
import {codexModelSelectionBlocked,knownUnavailableModels,codexModelError} from './codex-model-ui.js';

const STATUS='/api/office/codex/background/status',CREATE='/api/office/codex/background/create';
const messages={
  private_office_required:'請從你的私人 OFFICE 入口開啟。',
  invalid_name:'請填寫 1 至 80 字的對話名稱。',invalid_connection:'請填寫對話名稱及工作資料夾。',
  name_in_use:'這個名稱已有人使用，請換一個名字。',
  invalid_workspace:'請選擇可使用的工作資料夾。',workspace_not_found:'找不到這個工作資料夾；需要時可勾選建立資料夾。',
  codex_runtime_required:'這個 OFFICE 尚未找到本機 Codex。請安裝 Codex Desktop App 或 CLI，再重新檢查。',
  codex_binary_unavailable:'這個 OFFICE 尚未找到本機 Codex。請安裝 Codex Desktop App 或 CLI，再重新檢查。',
  codex_version_unavailable:'暫時無法讀取本機 Codex 版本。請讓安裝 Agent 檢查 Codex 是否可啟動，再重新檢查。',
  codex_process_unavailable:'Codex 背景程式未能啟動。請讓安裝 Agent 檢查本機 Codex，再重新檢查。',
  codex_process_closed:'Codex 背景程式已結束。請重新檢查；已登記的對話會保留。',
  codex_rpc_timeout:'Codex 回應逾時。請稍後重新檢查；不會自動另開對話。',
  codex_rpc_rejected:'Codex 未接受這次連線。請讓安裝 Agent 檢查本機 Codex 版本及登入，再重新檢查。',
  codex_probe_unavailable:'暫時未能檢查本機 Codex。請確認 Codex 可使用，再重新檢查。',
  codex_probe_failed:'暫時未能檢查本機 Codex。請確認 Codex 可使用，再重新檢查。',
  codex_login_required:'請先在運行 OFFICE 的電腦登入 Codex，再重新檢查。這裡不需要填寫 API key。',
  codex_login_unknown:'暫時未能確認 Codex 登入狀態。請確認本機 Codex 可使用，再重新檢查。',
  codex_model_invalid:'目前的 Codex 模型設定無法使用，請檢查本機 Codex 設定。',
  codex_creation_pending:'這次登記仍在處理中，請核對結果；不會另開一條對話。',
  codex_creation_unknown:'登記結果未確認，請核對結果；不會自動重做。',
  request_id_conflict:'這次登記的資料不一致，請先核對 OFFICE 已有的對話。',
  agent_membership_changed:'上一條對話已登出或身份已更改。舊紀錄保留，需要時可登記新對話。',
  browser_storage_unavailable:'瀏覽器暫時無法保存登記編號。請允許此頁的分頁儲存後再試；尚未登記新對話。',
};
export function codexBackgroundError(error){return ['codex_model_selection_required','codex_model_unavailable'].includes(error?.code||error?.message)?codexModelError(error):messages[error?.code||error?.message]||'暫時未能完成 Codex 背景模式設定，請稍後核對。';}
const uuid=value=>typeof value==='string'&&/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
const validModel=value=>typeof value==='string'&&/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value);
export function codexBackgroundRequestStore(getStorage,scope){
  const key='office-codex-background-create:'+scope;
  return {
    load(){const value=getStorage().getItem(key);return uuid(value)?value:null;},
    save(id){if(!uuid(id))throw new Error('invalid_request_identity');getStorage().setItem(key,id);},
    clear(id){const storage=getStorage();if(storage.getItem(key)===id)storage.removeItem(key);},
  };
}
function publicHome(data={}){
  return {available:data.available===true,logged_in:data.logged_in===true,
    reason:typeof data.reason==='string'&&Object.hasOwn(messages,data.reason)?data.reason:'',
    model:validModel(data.model)?data.model:'',runtime_source:['desktop_app','cli'].includes(data.runtime_source)?data.runtime_source:null,
    configured_model_warning:data.configured_model_warning===true,
    configured_model_requires_selection:data.configured_model_requires_selection===true,
    known_unavailable_models:knownUnavailableModels(data),
    models:[...new Map((Array.isArray(data.models)?data.models:[]).filter(row=>validModel(row?.id)).slice(0,100).map(row=>[row.id,{id:row.id,name:typeof row.name==='string'?row.name.slice(0,160):row.id}])).values()],
    workspace:typeof data.workspace==='string'?data.workspace:'',
    workspaces:(Array.isArray(data.workspaces)?data.workspaces:[]).filter(row=>typeof row?.path==='string').slice(0,200).map(row=>({name:String(row.name||row.path),path:row.path}))};
}
function createdSession(data){
  return typeof data?.agent_id==='string'&&data.agent_id.startsWith('codex:bg-')&&uuid(data.agent_id.slice(9))&&data.mode==='codex_background'
    ?{agent_id:data.agent_id,name:typeof data.name==='string'?data.name:'Codex 背景對話',scope:'office_owned_session'}:null;
}

/** Storage contains only a request UUID. Registration never implies execution proof. */
export class CodexBackgroundController{
  constructor({transport,pendingStore,makeId=()=>crypto.randomUUID(),onChange=()=>{},onCreated=()=>{}}){
    Object.assign(this,{transport,pendingStore,makeId,onChange,onCreated});
    this.epoch=0;this.revision=0;this.visible=false;this.phase='closed';this.home=null;this.error='';
    this.requestId=null;this.uncertain=false;this.inFlight=null;this.created=null;this.notifiedId=null;
  }
  emit(){this.onChange(this);}
  current(epoch){return this.visible&&this.epoch===epoch;}
  available(){return this.home?.available===true&&this.home.logged_in===true;}
  pendingPhase(){return this.inFlight?'creating':this.uncertain?'unknown':this.available()?'ready':'unavailable';}
  forgetRequest(id){try{this.pendingStore.clear(id);}catch{/* A completed receipt can be reconciled again after reload. */}}
  complete(id,session,focus){
    this.forgetRequest(id);
    if(this.requestId===id){this.uncertain=false;this.created=session;if(this.visible)this.phase='created';}
    if(this.notifiedId!==id){this.notifiedId=id;this.onCreated(session,{focus});}
  }
  async open(){
    this.visible=true;this.epoch++;this.created=null;this.error='';
    try{const id=this.pendingStore.load();if(id){this.requestId=id;this.uncertain=true;}}
    catch{this.error=messages.browser_storage_unavailable;}
    await this.refresh();
  }
  close(){this.visible=false;this.epoch++;this.revision++;this.phase='closed';this.error='';this.emit();}
  async refresh(){
    if(!this.visible)return;
    const epoch=this.epoch,revision=++this.revision,current=()=>this.current(epoch)&&revision===this.revision;
    const requestId=this.uncertain||this.inFlight?this.requestId:null;
    this.phase='loading';this.error='';this.emit();
    try{
      const data=await this.transport.read(requestId);
      if(!current())return;
      this.home=publicHome(data);
      const receipt=data.request;
      if(requestId&&requestId===this.requestId&&receipt?.request_id===requestId){
        if(receipt.state==='completed'){
          const session=createdSession(receipt.response);
          if(session&&receipt.response.request_id===requestId){this.complete(requestId,session,true);this.emit();return;}
        }else if(['failed','not_found','revoked'].includes(receipt.state)&&this.inFlight?.id!==requestId){
          this.forgetRequest(requestId);this.uncertain=false;this.requestId=null;
          if(receipt.state!=='not_found')this.error=codexBackgroundError({code:receipt.reason});
        }
      }
      this.phase=this.pendingPhase();
    }catch(error){if(!current())return;this.phase=this.uncertain||this.inFlight?'unknown':'failed';this.error=codexBackgroundError(error);}
    if(current())this.emit();
  }
  async create(values){
    if(!this.visible||this.inFlight||this.uncertain||this.phase!=='ready'||!this.available())return false;
    const name=String(values.name||'').trim(),workspace=String(values.workspace||'').trim();
    if(!name||name.length>80){this.error=messages.invalid_name;this.emit();return false;}
    if(!workspace){this.error=messages.invalid_workspace;this.emit();return false;}
    const model=values.model==null?'':typeof values.model==='string'?values.model.trim():null;
    if(model!==''&&!validModel(model)){this.error=messages.codex_model_invalid;this.emit();return false;}
    if(codexModelSelectionBlocked(this.home,model)){this.error=codexModelError({code:model?'codex_model_unavailable':'codex_model_selection_required'});this.emit();return false;}
    try{
      const pending=this.pendingStore.load();
      if(pending){this.requestId=pending;this.uncertain=true;this.phase='unknown';this.error=messages.codex_creation_unknown;this.emit();return false;}
      this.requestId=this.makeId();this.pendingStore.save(this.requestId);
    }catch{this.error=messages.browser_storage_unavailable;this.emit();return false;}
    const epoch=this.epoch,requestId=this.requestId,operation={id:requestId};
    const body={request_id:requestId,name,workspace};if(values.create_folder===true)body.create_folder=true;
    if(model)body.model=model;
    this.revision++;this.inFlight=operation;this.uncertain=true;this.phase='creating';this.error='';this.emit();
    try{
      const data=await this.transport.create(body),session=createdSession(data);
      if(!session||data.request_id!==requestId)throw Object.assign(new Error('invalid_creation_receipt'),{status:502});
      this.revision++;this.complete(requestId,session,this.current(epoch));return true;
    }catch(error){
      // A status receipt may already have confirmed this operation while POST was in flight.
      if(this.notifiedId===requestId)return true;
      if(this.requestId!==requestId)return false;
      this.revision++;
      this.uncertain=!error.status||error.status>=500||['codex_creation_pending','codex_creation_unknown','request_id_conflict'].includes(error.code||error.message);
      if(!this.uncertain){this.forgetRequest(requestId);this.requestId=null;}
      if(this.visible){this.phase=this.uncertain?'unknown':this.available()?'ready':'unavailable';this.error=this.uncertain?messages.codex_creation_unknown:codexBackgroundError(error);}
      return false;
    }finally{
      if(this.inFlight===operation)this.inFlight=null;
      if(this.visible){if(this.phase==='creating')this.phase=this.pendingPhase();this.emit();}
    }
  }
}

/** This dialog is opened only by an explicit platform choice. */
export function initCodexBackgroundUI({document:doc=document,events=window,key=()=>decodeURIComponent(location.hash.slice(1)),pendingStore,openEvent='office:new-codex-conversation'}={}){
  pendingStore ||= codexBackgroundRequestStore(()=>sessionStorage,location.origin+location.pathname.replace(/[^/]*$/,''));
  const dialog=doc.createElement('dialog');dialog.className='office-invite-dialog codex-background-dialog';dialog.setAttribute('aria-labelledby','codex-background-heading');
  dialog.innerHTML=`<button class="invite-close" type="button" aria-label="關閉 Codex 背景對話">×</button>
    <div class="invite-eyebrow">VIRTUAL OFFICE / CODEX</div><h2 id="codex-background-heading">Codex 背景模式</h2>
    <p class="invite-intro">建立 OFFICE 管理的獨立對話。Codex App 關閉仍可使用；運行 OFFICE 的電腦及背景服務需要保持運作。要保留已有的 Codex App 聊天，請選「接入現有 App／Cloud Agent」。</p>
    <p class="codex-background-status" role="status" aria-live="polite"></p>
    <form class="invite-form" hidden><label>對話名稱<input name="name" maxlength="80" required autocomplete="off" placeholder="例如：專案助手"></label>
      <label>工作資料夾<select name="known"><option value="">自行輸入路徑</option></select></label>
      <label>工作資料夾路徑<input name="workspace" required autocomplete="off" placeholder="輸入工作資料夾的絕對路徑"></label>
      <label class="codex-background-create-folder"><input name="create_folder" type="checkbox"> 資料夾未存在時建立</label>
      <label>模型（可選）<select name="model"><option value="">沿用 Codex 設定</option></select></label>
      <p class="codex-model-warning invite-footnote" role="status" hidden></p>
      <p class="invite-footnote">預設沿用本機模型，不會自動切換。選項由 Codex 提供；列在清單不代表目前帳戶一定可執行，收到實際回覆後才確認。</p>
      <p class="invite-footnote">沿用運行 OFFICE 這部電腦的 Codex 登入，毋須 API key。</p>
      <p class="invite-footnote">本版工作資料夾為唯讀；需要額外權限時會要求你批准今次操作。公開同步只涵蓋這條 OFFICE 背景對話，不會接管原本的 Codex App 聊天。</p>
      <button class="invite-primary" type="submit">登記背景對話</button></form>
    <button class="codex-background-refresh invite-primary" type="button" hidden>重新檢查</button>
    <section class="codex-background-result" hidden><p>點新角色頭頂即可輸入指示。收到實際回覆及交付紀錄後，才算完成收發驗證。</p><button class="invite-primary codex-background-open" type="button">查看新 Agent</button></section>
    <p class="invite-notice" role="alert"></p>`;
  doc.body.append(dialog);
  const form=dialog.querySelector('form'),notice=dialog.querySelector('.invite-notice'),status=dialog.querySelector('.codex-background-status'),submit=form.querySelector('button[type="submit"]'),refresh=dialog.querySelector('.codex-background-refresh'),result=dialog.querySelector('.codex-background-result'),modelWarning=dialog.querySelector('.codex-model-warning');
  let homeSignature='';
  const dispatch=(name,detail)=>events.dispatchEvent(new CustomEvent(name,{detail}));
  const controller=new CodexBackgroundController({pendingStore,transport:{
    read:requestId=>officeGet(STATUS+(requestId?'?'+new URLSearchParams({request_id:requestId}):''),key()),create:body=>officePost(CREATE,key(),body),
  },onCreated:(session,options)=>{dispatch('office:refresh');if(options.focus)dispatch('office:codex-background-created',session);},onChange:paint});
  function paint(state){
    const ready=state.phase==='ready',creating=state.phase==='creating';
    form.hidden=!['ready','creating'].includes(state.phase);result.hidden=state.phase!=='created';
    refresh.hidden=!['unavailable','unknown','failed'].includes(state.phase);refresh.textContent=state.phase==='unknown'?'核對登記結果':'重新檢查';
    const runtime=state.home?.runtime_source==='desktop_app'?'Codex Desktop 內置執行器':state.home?.runtime_source==='cli'?'本機 Codex CLI':'本機 Codex';
    status.textContent=({loading:'正在檢查本機 Codex…',ready:`${runtime} · 選擇工作資料夾，登記一條背景對話。`,creating:'正在登記背景對話，請稍候…',unavailable:codexBackgroundError({code:state.home?.reason||'codex_login_unknown'}),unknown:'登記結果未確認。請核對結果，不會自動另開一條。',created:'已加入，首次指示會啟動背景對話。',failed:'暫時未能讀取 Codex 背景模式設定。'}[state.phase]||'');
    notice.textContent=state.error;for(const input of form.elements)input.disabled=!ready;submit.textContent=creating?'登記中…':'登記背景對話';
    const signature=JSON.stringify(state.home);
    if(signature!==homeSignature&&state.home){
      homeSignature=signature;const known=form.elements.known;known.replaceChildren();
      const custom=doc.createElement('option');custom.value='';custom.textContent='自行輸入路徑';known.append(custom);
      for(const row of state.home.workspaces){const option=doc.createElement('option');option.value=row.path;option.textContent=row.name;known.append(option);}
      if(!form.elements.workspace.value)form.elements.workspace.value=state.home.workspace;
      known.value=[...known.options].some(o=>o.value===form.elements.workspace.value)?form.elements.workspace.value:'';
      const models=form.elements.model,selectedModel=models.value;models.replaceChildren();
      const configured=doc.createElement('option');configured.value='';configured.textContent=state.home.model?`沿用 Codex 設定（${state.home.model}）`:'沿用 Codex 設定';models.append(configured);
      for(const row of state.home.models){const option=doc.createElement('option');option.value=row.id;option.textContent=row.name&&row.name!==row.id?`${row.name}（${row.id}）`:row.id;models.append(option);}
      if(selectedModel&&validModel(selectedModel)&&!state.home.models.some(row=>row.id===selectedModel)){
        const kept=doc.createElement('option');kept.value=selectedModel;kept.textContent=`${selectedModel}（保留你的選擇）`;models.append(kept);
      }
      models.value=selectedModel;
    }
    const blocked=codexModelSelectionBlocked(state.home,form.elements.model.value),warn=blocked||(!form.elements.model.value&&state.home?.configured_model_warning)||knownUnavailableModels(state.home).includes(form.elements.model.value);
    if(ready&&warn)refresh.hidden=false;
    modelWarning.hidden=!warn;modelWarning.textContent=warn?(blocked?'目前沿用的模型曾被這個執行環境拒絕。請檢查本機 Codex 版本，或自行確認要使用的模型。':'這個模型曾有執行失敗紀錄；目前版本或帳戶權限可能已有變更。請自行確認，OFFICE 不會自動切換模型。'):'';
    submit.disabled=!ready||blocked;
  }
  const close=()=>{controller.close();if(dialog.open)dialog.close();};
  dialog.querySelector('.invite-close').addEventListener('click',close);
  dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
  dialog.addEventListener('close',()=>{if(controller.visible)controller.close();});
  form.elements.known.addEventListener('change',()=>{if(form.elements.known.value)form.elements.workspace.value=form.elements.known.value;});
  form.elements.model.addEventListener('change',()=>paint(controller));
  refresh.addEventListener('click',()=>controller.refresh());
  form.addEventListener('submit',event=>{event.preventDefault();controller.create({name:form.elements.name.value,workspace:form.elements.workspace.value,create_folder:form.elements.create_folder.checked,model:form.elements.model.value});});
  dialog.querySelector('.codex-background-open').addEventListener('click',()=>{const created=controller.created;close();if(created)dispatch('office:codex-background-created',created);});
  const open=()=>{if(dialog.open)return;if(controller.created){form.reset();homeSignature='';}dialog.showModal();controller.open();};
  events.addEventListener(openEvent,open);
  return {dialog,controller,dispose(){events.removeEventListener(openEvent,open);close();dialog.remove();}};
}
