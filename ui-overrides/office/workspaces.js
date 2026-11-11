const key=decodeURIComponent(location.hash.slice(1));
export async function workspaceRequest(path,body){
  const response=await fetch(path,{method:body?'POST':'GET',cache:'no-store',credentials:'same-origin',headers:{'X-Office-Key':key,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined,signal:AbortSignal.timeout(5000)});
  const data=await response.json();
  if(!response.ok)throw new Error(response.status===403?'請從私人 OFFICE 入口開啟。':'未能儲存，請檢查資料夾絕對路徑後再試。');
  return data;
}
export function workspaceChooser({allowUnassigned=false}={}){
  const root=document.createElement('div');root.className='office-workspace-chooser';
  root.innerHTML=`<label>Workspace<select aria-label="Workspace"></select></label><div class="workspace-new" hidden><label>專案資料夾絕對路徑<input class="workspace-path" placeholder="例如：/projects/my-project" autocomplete="off"></label><label>樓層名稱（可選）<input class="workspace-name" maxlength="80" placeholder="例如：網站專案" autocomplete="off"></label></div>`;
  const select=root.querySelector('select'),extra=root.querySelector('.workspace-new'),path=root.querySelector('.workspace-path'),name=root.querySelector('.workspace-name');
  let loadIntent=0,editRevision=0;
  const add=(value,text,title)=>{const option=document.createElement('option');option.value=value;option.textContent=text;if(title)option.title=title;select.append(option);};
  const toggle=()=>{extra.hidden=select.value!=='new';path.required=!extra.hidden;};
  select.addEventListener('change',()=>{editRevision++;toggle();});
  root.addEventListener('input',()=>editRevision++);
  function fill(rows,current){select.replaceChildren();add('auto','跟隨 Agent 工作目錄');if(allowUnassigned)add('unassigned','未指定 Workspace');for(const row of rows)add(row.id,row.name,row.path);add('new','＋ 新增 Workspace…');select.value=current?.mode==='manual'?(current.id==='workspace-unassigned'?'unassigned':current.id):'auto';toggle();}
  fill([]);
  return {
    root,invalidate(){loadIntent++;},
    reset(){loadIntent++;editRevision++;path.value='';name.value='';fill([]);},
    async load(agent,{accept=()=>true}={}){
      const intent=++loadIntent,revision=editRevision;
      const data=await workspaceRequest('/api/office/workspaces'+(agent?'?agent='+encodeURIComponent(agent):''));
      if(intent!==loadIntent||!accept())return false;
      if(revision===editRevision)fill(data.workspaces,data.current);
      return true;
    },
    async value(){
      let value=select.value;
      if(value==='new'){const body={path:path.value};if(name.value.trim())body.name=name.value.trim();value=(await workspaceRequest('/api/office/workspaces',body)).id;}
      return value==='auto'?{mode:'auto'}:{mode:'manual',workspace_id:value==='unassigned'?null:value};
    },
  };
}
if(key){
  const dialog=document.createElement('dialog');dialog.className='office-invite-dialog';dialog.setAttribute('aria-labelledby','workspace-heading');
  dialog.innerHTML=`<button class="invite-close" type="button" aria-label="關閉 Workspace 視窗">×</button><div class="invite-eyebrow">WORKSPACE</div><h2 id="workspace-heading">安排所在樓層</h2><p class="invite-intro"></p><form><button class="invite-primary" type="submit">儲存樓層</button></form><p class="invite-footnote">同一 Workspace 的 Agent 會坐在同一層，對話及工作記錄會保留。手動指定後會固定在該樓層，選「跟隨 Agent 工作目錄」可恢復自動。</p><p class="invite-notice" role="alert"></p>`;
  document.body.append(dialog);const form=dialog.querySelector('form'),button=form.querySelector('button'),notice=dialog.querySelector('.invite-notice'),chooser=workspaceChooser({allowUnassigned:true});form.prepend(chooser.root);let agent=null,busy=false,intent=0,ready=false;
  const capture=()=>({agent,intent}),current=ticket=>dialog.open&&ticket.agent===agent&&ticket.intent===intent;
  dialog.querySelector('.invite-close').addEventListener('click',()=>dialog.close());
  dialog.addEventListener('close',()=>{if(dialog.open)return;intent++;chooser.invalidate();ready=false;busy=false;button.disabled=true;});
  window.addEventListener('office:workspace',async(event)=>{
    if(typeof event.detail?.id!=='string'||!event.detail.id)return;
    intent++;chooser.invalidate();agent=event.detail.id;ready=false;busy=false;const ticket=capture();
    dialog.querySelector('.invite-intro').textContent=event.detail.name;notice.textContent='';button.disabled=true;if(!dialog.open)dialog.showModal();
    try{const loaded=await chooser.load(agent,{accept:()=>current(ticket)});if(current(ticket)&&loaded){ready=true;button.disabled=false;}}
    catch(error){if(current(ticket))notice.textContent=error.message;}
  });
  form.addEventListener('submit',async(event)=>{
    event.preventDefault();if(busy||!ready)return;const ticket=capture();busy=true;button.disabled=true;notice.textContent='';
    try{const chosen=await chooser.value();if(!current(ticket))return;await workspaceRequest('/api/office/workspaces/assign',{agent_id:ticket.agent,...chosen});if(current(ticket))dialog.close();}
    catch(error){if(current(ticket))notice.textContent=error.message;}
    finally{if(current(ticket)){busy=false;button.disabled=false;}}
  });
}
