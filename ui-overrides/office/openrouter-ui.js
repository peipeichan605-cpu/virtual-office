import {officeGet, officePost} from './office-api.js';

const ROOT = '/api/office/openrouter/';
const errors = {
  invalid_name:'請填寫 1 至 80 字的名稱。', name_in_use:'已有同名 Agent，請換一個名字。',
  invalid_platform_key:'請填寫有效的 OpenRouter API key。',
  platform_auth_required:'OpenRouter 未接受這把金鑰，請核對後再試。',
  platform_access_denied:'OpenRouter 拒絕存取，請核對帳戶或資料使用設定。',
  openrouter_inference_key_required:'請使用 OpenRouter 的一般 API key。',
  openrouter_model_unavailable:'所選免費模型已不可用，請更新清單再選擇。',
  openrouter_catalog_unavailable:'暫時無法核對免費模型，請再讀取清單。',
  openrouter_key_required:'請設定這個 OFFICE 使用的 OpenRouter API key。',
  platform_rate_limited:'OpenRouter 目前限制流量，請稍後再試。',
  private_office_required:'請從這部電腦的私人 OFFICE 管理入口開啟。',
};

export function initOpenRouterUI({document:doc=document, events=window, key=()=>decodeURIComponent(location.hash.slice(1))}={}) {
  const dialog=doc.createElement('dialog');
  dialog.className='office-invite-dialog';
  dialog.setAttribute('aria-label','OpenRouter 免費對話');
  dialog.innerHTML=`<button class="invite-close" type="button" aria-label="關閉">×</button>
    <h2>OpenRouter 免費對話</h2>
    <form><label>Agent 名稱<input name="name" required maxlength="80" autocomplete="off"></label>
      <label>免費文字模型<select name="model" required></select></label>
      <button type="button" class="invite-resume" data-refresh>更新模型清單</button>
      <button type="button" class="invite-resume" data-change hidden>更換本 OFFICE 的 API key</button>
      <div data-credentials><label>OpenRouter API key<input name="api_key" type="password" autocomplete="off"></label>
        <a href="https://openrouter.ai/settings/keys" target="_blank" rel="noopener noreferrer">到 OpenRouter 建立金鑰</a>
        <p class="invite-footnote">機構帳戶如強制使用收費外掛，請先在 OpenRouter 停用，再使用免費模式。</p>
        <button type="button" class="invite-resume" data-save hidden>儲存新金鑰</button></div>
      <p class="invite-footnote">此 Agent 的訊息和近期對話會送往 OpenRouter 及模型供應商。免費端點可能記錄內容，請只輸入適合分享的文字。</p>
      <button class="invite-primary" type="submit">加入 OFFICE</button></form>
    <p class="invite-notice" role="status" aria-live="polite"></p>`;
  doc.body.append(dialog);
  const get=selector=>dialog.querySelector(selector);
  const form=get('form'), name=form.elements.name, model=form.elements.model, field=form.elements.api_key;
  const credentials=get('[data-credentials]'), change=get('[data-change]'), save=get('[data-save]');
  const refresh=get('[data-refresh]'), submit=get('[type="submit"]'), notice=get('[role="status"]');
  let epoch=0, busy=false, saved=false, pending=null;
  function editKey(edit) {
    credentials.hidden=!edit;
    field.required=edit;
    save.hidden=!saved||!edit;
    change.hidden=!saved||edit;
    field.value='';
  }
  function lock(value) {
    busy=value;
    for (const element of [name, model, change, save, refresh, field]) element.disabled=value;
    submit.disabled=value||!model.value;
  }
  function describe(error) {
    return errors[error?.code] || '暫時未能完成。請核對連線，再用原來的名稱重試。';
  }
  async function load() {
    const token=++epoch, selected=model.value;
    lock(true);
    notice.textContent='正在核對免費模型…';
    try {
      const state=await officeGet(ROOT+'status',key());
      if(token!==epoch)return;
      model.replaceChildren();
      for(const item of state.models){
        const option=doc.createElement('option');option.value=item.id;option.textContent=item.name;model.append(option);
      }
      if(state.models.some(item=>item.id===selected))model.value=selected;
      saved=state.key_saved===true;editKey(!saved);
      notice.textContent=model.value?'':'暫時沒有可用的免費文字模型。';
    } catch(error) {
      if(token===epoch){model.replaceChildren();notice.textContent=describe(error);}
    } finally {if(token===epoch)lock(false);}
  }
  async function storeKey(token) {
    const apiKey=field.value.trim();
    if(!apiKey)throw Object.assign(new Error(),{code:'invalid_platform_key'});
    await officePost(ROOT+'key',key(),{api_key:apiKey});
    if(token!==epoch)return;
    saved=true;editKey(false);
  }
  function close() {
    epoch++;field.value='';if(dialog.open)dialog.close();
  }
  get('.invite-close').addEventListener('click',close);
  dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
  refresh.addEventListener('click',load);
  change.addEventListener('click',()=>{editKey(true);field.focus();});
  save.addEventListener('click',async()=>{
    if(busy)return;
    const token=epoch;lock(true);
    try {await storeKey(token);if(token===epoch)notice.textContent='本 OFFICE 的金鑰已更新。';}
    catch(error){if(token===epoch)notice.textContent=describe(error);}
    finally{if(token===epoch)lock(false);}
  });
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(busy||!model.value)return;
    const token=epoch, displayName=name.value.trim(), chosen=model.value;
    if(!pending||pending.name!==displayName||pending.model!==chosen)
      pending={name:displayName,model:chosen,request_id:crypto.randomUUID()};
    lock(true);notice.textContent='正在登記…';
    try{
      if(!credentials.hidden)await storeKey(token);
      if(token!==epoch)return;
      const result=await officePost(ROOT+'create',key(),pending);
      events.dispatchEvent(new CustomEvent('office:refresh'));
      if(token===epoch){pending=null;name.value='';close();events.dispatchEvent(new CustomEvent('office:openrouter-created',{detail:result}));}
    }catch(error){if(token===epoch)notice.textContent=describe(error);}
    finally{if(token===epoch)lock(false);}
  });
  return {dialog,open(){if(!dialog.open){dialog.showModal();load();}},dispose(){close();dialog.remove();}};
}
