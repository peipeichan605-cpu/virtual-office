import {officeGet,officePost} from './office-api.js';
import {groundLayout} from './overview.js';
import {canManageAgent} from './subagent-view.js';

const key=()=>decodeURIComponent(location.hash.slice(1));
const errors={desk_occupied:'呢個位置已有人使用，請揀另一個。',invalid_desk:'位置不存在，請重新選擇。',invalid_seating:'位置不存在，請重新選擇。',agent_not_selected:'呢位 Agent 已經離開 OFFICE。',active_commands:'工作仲未結束，完成或核對結果後先可以登出。',agent_has_active_commands:'工作仲未結束，完成或核對結果後先可以登出。',logout_has_active_commands:'工作仲未結束，完成或核對結果後先可以登出。',private_office_required:'請從私人 OFFICE 入口開啟。'};
export const controlError=error=>errors[error.message]||'暫時未能保存，請稍後再試。';

function shell(title){
  const dialog=document.createElement('dialog');dialog.className='office-agent-dialog';
  const heading=document.createElement('h2');heading.textContent=title;
  const description=document.createElement('p'),notice=document.createElement('p');notice.setAttribute('role','alert');
  const actions=document.createElement('div');actions.className='office-agent-actions';
  const cancel=document.createElement('button');cancel.type='button';cancel.textContent='取消';cancel.addEventListener('click',()=>dialog.close());
  actions.append(cancel);dialog.append(heading,description,notice,actions);dialog.setAttribute('aria-label',title);
  dialog.addEventListener('close',()=>dialog.remove());document.body.append(dialog);
  return {dialog,heading,description,notice,actions,cancel};
}

export function logoutAgent(bot){
  if(!canManageAgent(bot))return;
  const ui=shell('登出 '+bot.name);ui.description.textContent='讓這位 Agent 離開 OFFICE。交付歷史會保留；未開始的留言會取消，原 App 的聊天仍可繼續使用。';
  const submit=document.createElement('button');submit.type='button';submit.className='office-agent-danger';submit.textContent='確認登出';ui.actions.append(submit);
  submit.addEventListener('click',async()=>{
    submit.disabled=true;ui.cancel.disabled=true;ui.notice.textContent='正在登出…';
    try{await officePost('/api/office/agents/logout',key(),{agent_id:bot.agent_id});window.dispatchEvent(new CustomEvent('office:refresh'));window.dispatchEvent(new CustomEvent('office:agent-logged-out',{detail:{agent_id:bot.agent_id}}));ui.dialog.close();}
    catch(error){ui.notice.textContent=controlError(error);submit.disabled=false;ui.cancel.disabled=false;}
  });
  ui.dialog.showModal();ui.cancel.focus();
}

export async function chooseDesk(bot){
  if(!canManageAgent(bot))return;
  const ui=shell('安排位置 · '+bot.name);ui.description.textContent='選擇休息區嘅座位或辦公室工作台，亦可以直接拖曳角色到綠色位置。';
  const label=document.createElement('label');label.textContent='座位';const select=document.createElement('select');label.append(select);ui.notice.before(label);
  const submit=document.createElement('button');submit.type='button';submit.textContent='搬到這裏';submit.disabled=true;ui.actions.append(submit);ui.dialog.showModal();
  try{
    const data=await officeGet('/api/office/seating',key());if(!ui.dialog.open)return;
    const placements=data.placements||{},occupied=new Map(Object.entries(placements).map(([agent,desk])=>[desk,agent]));
    const count=Math.max(36,Object.keys(placements).length,...Object.values(placements).map(id=>Number(/^gf-main-(\d{3})$/.exec(id)?.[1]||0)));
    for(const seat of groundLayout(count).seats){const option=document.createElement('option');option.value=seat.id;option.textContent=seat.label||seat.id;option.disabled=occupied.has(seat.id)&&occupied.get(seat.id)!==bot.agent_id;option.textContent+=option.disabled?' · 已有人':'';select.append(option);}
    select.value=placements[bot.agent_id]||bot.ground_desk_id||select.options[0]?.value;submit.disabled=false;
  }catch(error){ui.notice.textContent=controlError(error);}
  submit.addEventListener('click',async()=>{
    submit.disabled=true;ui.notice.textContent='正在保存位置…';
    try{await officePost('/api/office/seating',key(),{agent_id:bot.agent_id,desk_id:select.value});window.dispatchEvent(new CustomEvent('office:desk-moved',{detail:{agent_id:bot.agent_id,desk_id:select.value}}));window.dispatchEvent(new CustomEvent('office:refresh'));ui.dialog.close();}
    catch(error){ui.notice.textContent=controlError(error);submit.disabled=false;}
  });
}
