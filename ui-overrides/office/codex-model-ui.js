import {officeGet,officePost} from './office-api.js';

const STATUS='/api/office/codex/background/status',UPDATE='/api/office/codex/background/model';
export const validCodexModel=value=>typeof value==='string'&&/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value);
const uuid=value=>typeof value==='string'&&/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
const agentId=value=>typeof value==='string'&&value.startsWith('codex:bg-')&&uuid(value.slice(9));
export function codexModelError(error){return ({
  private_office_required:'請從你的私人 OFFICE 入口開啟。',
  invalid_model:'請選擇有效的 Codex 模型。',codex_model_invalid:'請選擇有效的 Codex 模型。',
  codex_model_unavailable:'目前本機 Codex 執行環境無法使用這個模型。請檢查版本及帳戶權限，或按「模型設定」自行選擇模型。',
  codex_model_selection_required:'目前沿用的模型曾被這個執行環境拒絕。請檢查本機 Codex 版本，或自行確認要使用的模型。',
  codex_model_change_busy:'這位 Agent 有尚未結束的指示，請等目前工作結束後再更改模型。',
  agent_has_active_commands:'這位 Agent 有尚未結束的指示，請等目前工作結束後再更改模型。',
  codex_model_change_unknown:'模型設定結果未確認。請核對結果，不會自動重做或重送指示。',
  codex_model_change_pending:'模型設定仍在處理。請核對結果，不會另開對話。',
  codex_model_change_not_applied:'上次模型設定沒有套用。現有對話及紀錄保留，可重新選擇。',
  codex_model_change_superseded:'這次設定之後已有其他模型設定。已載入目前設定，不會重做舊變更。',
  agent_membership_changed:'這位 Agent 已登出或接駁已改變；舊紀錄仍保留。',
  agent_not_selected:'這位 Agent 已經離開 OFFICE。',
  codex_background_agent_not_selected:'這位 Agent 已經離開 OFFICE，或不屬於 Codex 背景模式。',
  codex_background_recovery_required:'上次執行結果未確認，請先核對原來的工作；現在不會更改模型。',
  browser_storage_unavailable:'瀏覽器無法保存設定編號。尚未更改模型，請允許此頁的分頁儲存後再試。',
  request_id_conflict:'這次設定編號與內容不一致。請先核對已保存的結果。',
}[error?.code||error?.message]||'暫時未能更改模型。請核對目前設定後再試。');}

export function codexModelRequestStore(getStorage,scope,agent){
  const key='office-codex-model:'+scope+':'+agent;
  return {load(){const id=getStorage().getItem(key);return uuid(id)?id:null;},
    save(id){if(!uuid(id))throw new Error('invalid_request_id');getStorage().setItem(key,id);},
    clear(id){const storage=getStorage();if(storage.getItem(key)===id)storage.removeItem(key);}};
}
export function knownUnavailableModels(data){return (Array.isArray(data?.known_unavailable_models)?data.known_unavailable_models:[]).filter(validCodexModel).slice(0,100);}
export function codexModelSelectionBlocked(data,model){
  return !model&&data?.configured_model_requires_selection===true;
}
function home(data,agent){
  const item=data?.current_agent;
  return {current_agent:item?.agent_id===agent?{agent_id:agent,name:typeof item.name==='string'?item.name.slice(0,80):'Codex Agent',
    current_model:validCodexModel(item.current_model)?item.current_model:'',actual_model:validCodexModel(item.actual_model)?item.actual_model:'',
    can_change:item.can_change===true,reason:typeof item.reason==='string'&&/^[a-z_]{1,100}$/.test(item.reason)?item.reason:'',pending_model_change:item.pending_model_change===true}:null,
    models:[...new Map((Array.isArray(data?.models)?data.models:[]).filter(m=>validCodexModel(m?.id)).slice(0,100).map(m=>[m.id,{id:m.id,name:typeof m.name==='string'?m.name.slice(0,160):m.id}])).values()],
    known_unavailable_models:knownUnavailableModels(data)};
}
function savedReceipt(data,agent,id){return data?.request_id===id&&data.agent_id===agent&&data.model_saved===true&&validCodexModel(data.model)&&data.same_session===true&&data.native_thread_preserved===true;}
const publicReceipt=data=>({request_id:data.request_id,agent_id:data.agent_id,model:data.model,model_saved:true,same_session:true,native_thread_preserved:true,pending_next_instruction:true,execution_verified:false});

/** One request identity per selected Agent; no prompt, key or model is stored. */
export class CodexModelController{
  constructor({agent,transport,pendingStore,makeId=()=>crypto.randomUUID(),onChange=()=>{},onSaved=()=>{}}){
    Object.assign(this,{agent,transport,pendingStore,makeId,onChange,onSaved});this.visible=false;this.epoch=0;this.revision=0;
    this.phase='closed';this.home=null;this.error='';this.requestId=null;this.inFlight=null;this.uncertain=false;this.notifiedId=null;this.saved=null;this.settledId=null;
  }
  emit(){this.onChange(this);}
  current(epoch,revision=this.revision){return this.visible&&this.epoch===epoch&&this.revision===revision;}
  forget(id){try{this.pendingStore.clear(id);}catch{}}
  complete(id,response){
    if(this.settledId===id)return;
    this.forget(id);if(this.requestId===id){this.saved=publicReceipt(response);this.uncertain=false;if(this.visible)this.phase='saved';}
    if(this.notifiedId!==id){this.notifiedId=id;this.onSaved({agent_id:this.agent,model:response.model});}
  }
  async open(){
    this.visible=true;this.epoch++;this.error='';this.saved=null;
    try{const id=this.pendingStore.load();if(id){this.requestId=id;this.uncertain=true;}}catch{this.error=codexModelError({code:'browser_storage_unavailable'});}
    await this.refresh();
  }
  close(){this.visible=false;this.epoch++;this.revision++;this.phase='closed';this.emit();}
  readyPhase(){return this.inFlight?'saving':this.uncertain?'unknown':this.home?.current_agent?.can_change?'ready':'unavailable';}
  async refresh(){
    if(!this.visible)return;const epoch=this.epoch,revision=++this.revision;
    const id=this.uncertain||this.inFlight?this.requestId:null;this.phase='loading';this.error='';this.emit();
    try{
      const data=await this.transport.read(this.agent,id);if(!this.current(epoch,revision))return;
      this.home=home(data,this.agent);const r=data.request;
      if(id&&r?.request_id===id&&(r.operation==='model_change'||r.state==='not_found'&&!r.operation)){
        if(r.state==='completed'&&savedReceipt(r.response,this.agent,id)){this.complete(id,r.response);this.emit();return;}
        if(['revoked','superseded'].includes(r.state)||['failed','not_found'].includes(r.state)&&this.inFlight?.id!==id){
          this.settledId=id;this.forget(id);this.uncertain=false;this.requestId=null;if(r.state!=='not_found')this.error=codexModelError({code:r.reason});
          if(this.inFlight?.id===id)this.inFlight=null;
        }
      }
      this.phase=this.readyPhase();
      if(!this.home.current_agent)this.error=codexModelError({code:'agent_not_selected'});
    }catch(error){if(!this.current(epoch,revision))return;this.phase=this.uncertain||this.inFlight?'unknown':'failed';this.error=codexModelError(error);}
    if(this.current(epoch,revision))this.emit();
  }
  async save(model){
    if(!this.visible||this.phase!=='ready'||this.inFlight||this.uncertain||!this.home?.current_agent?.can_change)return false;
    if(!validCodexModel(model)){this.error=codexModelError({code:'invalid_model'});this.emit();return false;}
    try{const pending=this.pendingStore.load();if(pending){this.requestId=pending;this.uncertain=true;this.phase='unknown';this.emit();return false;}this.requestId=this.makeId();this.pendingStore.save(this.requestId);}
    catch{this.error=codexModelError({code:'browser_storage_unavailable'});this.emit();return false;}
    const id=this.requestId,operation={id};this.inFlight=operation;this.uncertain=true;this.revision++;this.phase='saving';this.error='';this.emit();
    try{
      const response=await this.transport.update({request_id:id,agent_id:this.agent,model});
      if(this.settledId===id||this.requestId!==id)return false;
      if(!savedReceipt(response,this.agent,id)||response.model!==model)throw new Error('invalid_model_receipt');
      this.revision++;this.complete(id,response);return true;
    }catch(error){
      if(this.settledId===id)return false;if(this.notifiedId===id)return true;if(this.requestId!==id)return false;this.revision++;
      this.uncertain=!error.status||error.status>=500||['request_id_conflict','codex_model_change_pending','codex_model_change_unknown'].includes(error.code||error.message);
      if(!this.uncertain){this.forget(id);this.requestId=null;}
      if(this.visible){this.phase=this.uncertain?'unknown':this.home?.current_agent?.can_change?'ready':'unavailable';this.error=codexModelError(this.uncertain?{code:'codex_model_change_unknown'}:error);}
      return false;
    }finally{if(this.inFlight===operation)this.inFlight=null;if(this.visible){if(this.phase==='saving')this.phase=this.readyPhase();this.emit();}}
  }
}

const dialogs=new WeakMap();
/** Lazy and shared by the head composer and history page; no polling loop. */
export function openCodexModelDialog(agent,{document:doc=document,events=window,key=()=>decodeURIComponent(location.hash.slice(1)),pendingStore}={}){
  if(!agentId(agent))return null;
  let items=dialogs.get(doc);if(!items){items=new Map();dialogs.set(doc,items);}
  const ownerKey=key(),previous=items.get(agent);
  if(previous&&previous.ownerKey===ownerKey){previous.open();return previous;}
  if(previous)previous.dispose();
  pendingStore ||= codexModelRequestStore(()=>sessionStorage,location.origin+location.pathname.replace(/[^/]*$/,''),agent);
  const dialog=doc.createElement('dialog');dialog.className='office-invite-dialog codex-model-dialog';dialog.setAttribute('aria-label','更改 Codex 模型');
  dialog.innerHTML='<button class="invite-close" type="button" aria-label="關閉模型設定">×</button><div class="invite-eyebrow">CODEX / 模型設定</div><h2>更改 Codex 模型</h2><p class="invite-intro">保留同一位 Agent、原生對話及歷史。只在下一次指示使用新模型，不會重送之前失敗的指示，也不會更改 Codex App 的全域設定。</p><p class="codex-model-current"></p><p class="codex-model-status" role="status"></p><form class="invite-form" hidden><label>使用模型<select name="model" required aria-label="使用模型"></select></label><p class="invite-footnote">模型清單不保證目前帳戶或本機 Codex 版本可用。OFFICE 不會自動選擇替代模型。</p><button class="invite-primary" type="submit">儲存模型，保留對話</button></form><button class="codex-model-refresh invite-primary" type="button" hidden>重新檢查</button><p class="invite-notice" role="alert"></p>';
  doc.body.append(dialog);const form=dialog.querySelector('form'),select=form.elements.model,submit=form.querySelector('button[type="submit"]'),current=dialog.querySelector('.codex-model-current'),status=dialog.querySelector('.codex-model-status'),notice=dialog.querySelector('.invite-notice'),refresh=dialog.querySelector('.codex-model-refresh');
  let signature='',chosen=false;
  const controller=new CodexModelController({agent,pendingStore,transport:{read:(id,requestId)=>officeGet(STATUS+'?'+new URLSearchParams({agent_id:id,...(requestId?{request_id:requestId}:{})}),ownerKey),update:body=>officePost(UPDATE,ownerKey,body)},
    onSaved:detail=>{events.dispatchEvent(new CustomEvent('office:refresh'));events.dispatchEvent(new CustomEvent('office:codex-model-saved',{detail}));},onChange:paint});
  function paint(state){
    const info=state.home?.current_agent,ready=state.phase==='ready';
    current.textContent=info?`${info.name} · 目前設定：${info.current_model||'沿用 Codex 設定'}`:'';
    status.textContent=({loading:'正在讀取模型設定…',ready:'請自行選擇模型。',saving:'正在保存模型設定…',saved:'模型已保存。下一項指示會沿用同一條對話；尚未執行新的指示。',unknown:'設定結果未確認，請核對結果。',failed:'暫時未能讀取模型設定。',unavailable:codexModelError({code:info?.reason||'agent_not_selected'})}[state.phase]||'');
    form.hidden=!['ready','saving'].includes(state.phase);refresh.hidden=!['ready','unknown','unavailable','failed'].includes(state.phase);refresh.textContent=state.phase==='unknown'?'核對設定結果':'重新檢查';
    if(state.home&&signature!==JSON.stringify(state.home)){
      signature=JSON.stringify(state.home);const selected=chosen?select.value:info?.current_model||'';select.replaceChildren();
      const options=[...state.home.models];if(selected&&!options.some(m=>m.id===selected))options.unshift({id:selected,name:selected});
      const empty=doc.createElement('option');empty.value='';empty.textContent='請選擇模型';select.append(empty);
      for(const model of options){const option=doc.createElement('option');option.value=model.id;option.textContent=model.name!==model.id?`${model.name}（${model.id}）`:model.id;if(knownUnavailableModels(state.home).includes(model.id))option.textContent+=' · 曾被此版本拒絕';select.append(option);}
      select.value=selected;
    }
    select.disabled=!ready;submit.disabled=!ready||!validCodexModel(select.value);
    notice.textContent=state.error||(ready&&knownUnavailableModels(state.home).includes(select.value)?'這個模型曾被目前版本拒絕；帳戶或設定可能已有變更。請自行確認，OFFICE 不會自動切換模型。':'');
  }
  const close=()=>{controller.close();if(dialog.open)dialog.close();};
  dialog.querySelector('.invite-close').addEventListener('click',close);dialog.addEventListener('cancel',e=>{e.preventDefault();close();});dialog.addEventListener('close',()=>{if(controller.visible)controller.close();});
  select.addEventListener('change',()=>{chosen=true;paint(controller);});form.addEventListener('submit',e=>{e.preventDefault();controller.save(select.value);});refresh.addEventListener('click',()=>controller.refresh());
  const onLogout=event=>{if(event.detail?.agent_id===agent)close();};events.addEventListener('office:agent-logged-out',onLogout);
  const handle={ownerKey,dialog,controller,open(){if(dialog.open)return;dialog.showModal();controller.open();},dispose(){events.removeEventListener('office:agent-logged-out',onLogout);close();dialog.remove();items.delete(agent);}};
  items.set(agent,handle);handle.open();return handle;
}
