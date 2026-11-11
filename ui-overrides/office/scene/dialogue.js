import { officeGet, agentPageUrl } from '../office-api.js';
import { CommandComposer } from '../commands.js';
import {logoutAgent,chooseDesk} from '../office-controls.js';
import {canManageAgent,childEvidenceLabel} from '../subagent-view.js';
import {SingleHeadPanel,headStatus} from './dialogue-layout.js';

const activePanel = new SingleHeadPanel();

export const roleLabel = (message,bot) => message?.role === 'user' ? (bot?.is_subagent?'主 Agent 指派':'你') : message?.phase === 'final' ? '交付' : '進度';
export const previewText = (message) => String(message?.text || '').replace(/\s+/g, ' ').trim().slice(0, 120);
export function publicEvidenceLabel(bot){
  if(bot?.is_subagent)return childEvidenceLabel(bot)||'公開回報';
  const sync=bot?.command_capability?.public_sync;
  if(sync?.mode==='cooperative')return 'Agent 合作回報';
  if(sync?.scope==='office_dispatched_runs')return 'OFFICE 工作同步';
  if(sync?.configured===true&&sync.state==='configured'&&sync.scope==='selected_conversation'&&
      ['native_observer','native_hook'].includes(sync.mode))return '原生公開同步';
  return '公開回報';
}
export function mergeMessages(previous, incoming) {
  const key=row=>row.event_id || row.seq;
  const rows = new Map(previous.map((row) => [key(row), row]));
  for (const row of incoming) if(!rows.has(key(row)) || rows.get(key(row)).seq<=row.seq)rows.set(key(row), row);
  const ordered=[...rows.values()].sort((a,b) => a.seq-b.seq);
  const finalKey=row=>row.role==='assistant'&&row.phase==='final'&&
    typeof row.agent_id==='string'&&typeof row.run_id==='string'
    ?JSON.stringify([row.agent_id,row.run_id,row.text]):null;
  const finals=new Map();
  for(const row of ordered){const body=finalKey(row);if(body!==null)finals.set(body,row);}
  // A native final envelope can arrive before a stable public bubble's last
  // revision. Apply that revision first, then keep its latest final projection.
  return ordered.filter(row=>{const body=finalKey(row);return body===null||finals.get(body)===row;});
}

export function dialoguePlacement(anchorY, viewportHeight, chromeHeight) {
  const top = Math.min(84, viewportHeight * 0.18), bottom = 16;
  const above = anchorY - top, below = viewportHeight - bottom - anchorY;
  const useBelow = above < chromeHeight + 80 && below > above;
  return { below: useBelow, height: Math.floor(Math.max(40, Math.min(210, viewportHeight * 0.3,
    (useBelow ? below : above) - chromeHeight - 12))) };
}

/** DOM stays anchored to the actor through CSS2DObject; no message text becomes HTML. */
export class DialogueBubble {
  constructor(onSelect, onOpenChild) {
    this.onSelect=onSelect;this.rows=[];this.cursor=0;this.before=null;this.open=false;this.pending=false;this.signature='';this.nodes=new Map();this.requestEpoch=0;this.pendingRequest=null;
    this.onOpenChild=onOpenChild;this.children=[];this.teamOpen=false;this.teamSignature='';
    this.element=document.createElement('div');this.element.className='office-speech';
    this.element.innerHTML='<button class="speech-peek" type="button" aria-expanded="false"><span class="speech-name"></span><span class="speech-meta"></span><span class="speech-preview"></span></button><section class="speech-detail" hidden><div class="speech-toolbar"><span role="status">公開對話</span><button type="button" class="speech-close" aria-label="收起對話">×</button></div><button type="button" class="speech-older" hidden>較早訊息</button><div class="speech-messages" role="log" aria-live="polite" aria-relevant="additions"></div><a class="speech-history">工作與交付記錄 ↗</a></section><span class="speech-tail"></span>';
    this.peek=this.element.querySelector('.speech-peek');this.detail=this.element.querySelector('.speech-detail');
    this.status=document.createElement('span');this.status.className='speech-state';this.peek.prepend(this.status);
    this.team=document.createElement('button');this.team.type='button';this.team.className='speech-team';this.team.hidden=true;
    this.team.setAttribute('aria-expanded','false');this.team.addEventListener('click',()=>this.setTeamOpen(!this.teamOpen));
    this.element.insertBefore(this.team,this.detail);
    this.family=document.createElement('section');this.family.className='speech-family';this.family.hidden=true;
    this.family.setAttribute('aria-label','子 Agent 名單');this.family.setAttribute('role','group');
    this.element.insertBefore(this.family,this.detail);
    const history=this.element.querySelector('.speech-history');history.textContent='記錄 ↗';history.setAttribute('aria-label','工作與交付記錄');
    this.element.querySelector('.speech-toolbar').insertBefore(history,this.element.querySelector('.speech-close'));
    this.move=document.createElement('button');this.move.type='button';this.move.className='speech-move';this.move.textContent='⇄ 移位';this.move.title='拖曳到休息位或辦公室工作台；點一下可選擇位置';
    this.logout=document.createElement('button');this.logout.type='button';this.logout.className='speech-logout';this.logout.textContent='登出';
    this.element.querySelector('.speech-toolbar').insertBefore(this.move,this.element.querySelector('.speech-close'));
    this.element.querySelector('.speech-toolbar').insertBefore(this.logout,this.element.querySelector('.speech-close'));
    this.move.addEventListener('pointerdown',event=>{if(!canManageAgent(this.bot)||event.button!==0||!document.documentElement.classList.contains('office-ground-view'))return;event.preventDefault();window.dispatchEvent(new CustomEvent('office:agent-drag-start',{detail:{id:this.bot?.id,event}}));});
    this.move.addEventListener('click',event=>{if(canManageAgent(this.bot)&&(event.detail===0||!document.documentElement.classList.contains('office-ground-view')))chooseDesk(this.bot);});
    this.logout.addEventListener('click',()=>logoutAgent(this.bot));
    this.composer=new CommandComposer();this.detail.append(this.composer.element);
    this.list=this.element.querySelector('.speech-messages');this.notice=this.element.querySelector('[role=status]');this.older=this.element.querySelector('.speech-older');
    this.peek.addEventListener('click',()=>{this.setOpen(!this.open);if(this.open)this.onSelect?.();});
    this.element.querySelector('.speech-close').addEventListener('click',()=>{this.setOpen(false);this.peek.focus({preventScroll:true});});
    this.older.addEventListener('click',()=>this.refresh(true));
    for(const type of ['pointerdown','pointerup','click','wheel','touchstart','touchmove'])this.element.addEventListener(type,event=>event.stopPropagation());
  }
  update(bot, selected) {
    if(this.bot?.agent_id&&this.bot.agent_id!==bot.agent_id){
      this.setOpen(false);this.rows=[];this.cursor=0;this.before=null;this.signature='';this.nodes.clear();this.list.replaceChildren();this.older.hidden=true;
    }
    this.bot=bot;
    this.element.dataset.status=bot.effective_status||bot.status||'waiting_user';
    this.element.classList.toggle('is-subagent',!!bot.is_subagent);
    this.composer.bind(bot.agent_id,bot.command_capability);
    this.composer.element.hidden=!!bot.is_subagent;
    this.move.hidden=!canManageAgent(bot);this.logout.hidden=!canManageAgent(bot);
    this.element.querySelector('.speech-name').textContent=bot.name;
    const evidence=bot.is_subagent?childEvidenceLabel(bot):'';
    const relation=bot.is_subagent?[`${bot.parent_name||'主 Agent'} 的子 Agent`,evidence].filter(Boolean).join(' · '):'';
    this.peek.setAttribute('aria-label',[bot.name+' 的即時對話',headStatus(bot.effective_status||bot.status),relation].filter(Boolean).join(' · '));
    this.peek.title=[bot.name,headStatus(bot.effective_status||bot.status),relation].filter(Boolean).join(' · ');
    this.status.textContent='';this.status.setAttribute('aria-hidden','true');
    this.move.setAttribute('aria-label','移動 '+bot.name+' 的位置');this.logout.setAttribute('aria-label','登出 '+bot.name);
    this.element.querySelector('.speech-meta').textContent=bot.is_subagent?relation:bot.latest_message ? roleLabel(bot.latest_message,bot) : bot.child_count?`子 Agent · ${bot.child_count}`:'即時對話';
    this.element.querySelector('.speech-preview').textContent=previewText(bot.latest_message)||'等待公開訊息…';
    this.element.querySelector('.speech-history').href=bot.chat_url || '#';
    if(this.selected && !selected)this.closePanels();
    this.selected=selected;
    this.visible=!!bot.agent_id;
    if(this.open)this.refresh();
  }
  get expanded(){return this.open||this.teamOpen;}
  closePanels(){this.setOpen(false);this.setTeamOpen(false);}
  setChildren(children) {
    this.children=children;
    this.element.classList.toggle('has-family',!!children.length);
    this.team.hidden=!children.length;
    this.team.textContent='子 Agent '+children.length;
    this.team.setAttribute('aria-label',`查看 ${this.bot?.name||'主 Agent'} 的 ${children.length} 位子 Agent`);
    if(!children.length){this.setTeamOpen(false);return;}
    const signature=JSON.stringify(children.map(child=>[child.id,child.name,child.effective_status||child.status,child.depth]));
    if(signature===this.teamSignature)return;
    const focusedChild=this.family.contains(document.activeElement)?document.activeElement.dataset.childId:null;
    this.teamSignature=signature;this.family.replaceChildren();
    const heading=document.createElement('div');heading.className='speech-family-title';
    const title=document.createElement('strong');title.textContent='子 Agent · '+children.length;
    const close=document.createElement('button');close.type='button';close.textContent='×';close.className='speech-close';close.setAttribute('aria-label','收起子 Agent 名單');close.addEventListener('click',()=>{this.setTeamOpen(false);this.team.focus({preventScroll:true});});
    heading.append(title,close);this.family.append(heading);
    const list=document.createElement('div');list.className='speech-family-list';
    for(const child of children){
      const button=document.createElement('button');button.type='button';button.className='speech-child';
      button.dataset.childId=child.id;
      const name=document.createElement('span');name.textContent=(child.depth>1?'↳ ':'')+child.name;
      const state=document.createElement('small');state.textContent=headStatus(child.effective_status||child.status);
      button.append(name,state);button.setAttribute('aria-label',`開啟 ${child.name} 的對話 · ${state.textContent}`);
      button.addEventListener('click',()=>{if(this.disabled)return;this.setTeamOpen(false);this.onOpenChild?.(child.id);});list.append(button);
    }
    this.family.append(list);
    if(focusedChild&&this.teamOpen){const target=[...list.children].find(button=>button.dataset.childId===focusedChild);(target||this.team).focus({preventScroll:true});}
  }
  setTeamOpen(value) {
    if(value&&this.disabled)return;
    if(value&&!this.children.length)return;
    if(value){this.setOpen(false);activePanel.claim(this);}
    this.teamOpen=value;this.family.hidden=!value;this.team.setAttribute('aria-expanded',String(value));
    this.element.classList.toggle('expanded',this.expanded);
    if(value)this.onSelect?.();
    if(!this.expanded)activePanel.release(this);
  }
  setOpen(value) {
    if(value&&this.disabled)return;
    if(value){this.setTeamOpen(false);activePanel.claim(this);}
    else this.invalidateRequests();
    this.open=value;this.detail.hidden=!value;this.peek.setAttribute('aria-expanded',String(value));this.element.classList.toggle('expanded',this.expanded);
    if(value){this.refresh();this.timer ||= setInterval(()=>this.refresh(),2000);}
    else{clearInterval(this.timer);this.timer=null;this.element.classList.remove('below');}
    if(!this.expanded)activePanel.release(this);
  }
  invalidateRequests(){
    this.requestEpoch=(this.requestEpoch||0)+1;this.pendingRequest=null;this.pending=false;
  }
  acceptsRequest(ticket){
    return this.pendingRequest===ticket&&ticket.epoch===this.requestEpoch&&
      ticket.agent===this.bot?.agent_id&&this.open&&!this.disabled;
  }
  fitViewport(anchorY, viewportHeight) {
    if(!this.expanded)return false;
    const above=anchorY-Math.min(76,viewportHeight*.12),below=viewportHeight-12-anchorY;
    const available=Math.max(90,Math.min(viewportHeight-Math.min(76,viewportHeight*.12)-12,Math.max(above,below)-24));
    const bodyHeight=Math.max(45,available-(this.peek.offsetHeight||60)-12);
    this.detail.style.maxHeight=bodyHeight+'px';this.family.style.maxHeight=bodyHeight+'px';
    if(this.teamOpen){
      const familyList=this.family.querySelector('.speech-family-list');
      if(familyList)familyList.style.maxHeight=Math.min(240,Math.max(40,bodyHeight-42))+'px';
      return above<available&&below>above;
    }
    const chrome=this.element.offsetHeight-this.list.offsetHeight;
    const placement=dialoguePlacement(anchorY,viewportHeight,chrome);
    if(this.fitHeight!==placement.height){
      const atBottom=this.list.scrollHeight-this.list.scrollTop-this.list.clientHeight<48;
      this.list.style.maxHeight=placement.height+'px';this.fitHeight=placement.height;
      if(atBottom)this.list.scrollTop=this.list.scrollHeight;
    }
    this.element.classList.toggle('below',placement.below);
    return placement.below;
  }
  async refresh(older=false) {
    if(this.pending||!this.open||this.disabled||!this.bot?.agent_id)return;
    const ticket={epoch:this.requestEpoch,agent:this.bot.agent_id};this.pendingRequest=ticket;this.pending=true;
    this.composer.refresh();
    const agent=this.bot.agent_id;
    try{
      const key=decodeURIComponent(location.hash.slice(1));
      const query=new URLSearchParams({agent});
      if(older&&this.before)query.set('before',this.before);else if(this.cursor)query.set('after',this.cursor);
      const data=await officeGet('/api/office/messages?'+query,key);
      if(!this.acceptsRequest(ticket))return;
      this.cursor=Math.max(this.cursor,data.cursor||0);
      if(older||!this.rows.length)this.before=data.before;
      this.rows=mergeMessages(this.rows,data.messages);
      this.notice.textContent=publicEvidenceLabel(this.bot)+' · 已遮蓋敏感內容';this.older.hidden=!this.before;
      const signature=JSON.stringify(this.rows.map(row=>row.seq));
      if(signature!==this.signature){
        this.signature=signature;
        const bottom=this.list.scrollHeight-this.list.scrollTop-this.list.clientHeight<48;
        const oldHeight=this.list.scrollHeight, oldTop=this.list.scrollTop;
        const visible=new Set(this.rows.map(row=>row.event_id||row.seq));
        for(const [id,article] of this.nodes){
          if(!visible.has(id)){article.remove();this.nodes.delete(id);}
        }
        for(const [index,row] of this.rows.entries()){
          const key=row.event_id || row.seq;
          let article=this.nodes.get(key);
          if(!article){
            article=document.createElement('article');article.className='speech-message';article.dataset.role=row.role;
            article.append(document.createElement('small'),document.createElement('p'));this.nodes.set(key,article);
          }
          article.querySelector('small').textContent=roleLabel(row,this.bot)+' · '+new Date(row.at).toLocaleTimeString('zh-HK',{hour:'2-digit',minute:'2-digit'});
          article.querySelector('p').textContent=row.text;
          if(this.list.children[index]!==article)this.list.insertBefore(article,this.list.children[index]||null);
        }
        if(older)this.list.scrollTop=oldTop+this.list.scrollHeight-oldHeight;
        else if(bottom)this.list.scrollTop=this.list.scrollHeight;
        else this.list.scrollTop=oldTop;
      }
      if(!this.rows.length)this.notice.textContent='尚未收到公開訊息';
    }catch{if(this.acceptsRequest(ticket))this.notice.textContent='連線中斷 · 保留已讀訊息';}
    finally{if(this.pendingRequest===ticket){this.pendingRequest=null;this.pending=false;}}
  }
  disable(){
    this.disabled=true;this.closePanels();this.composer?.dispose?.();
    this.element.inert=true;this.element.setAttribute('aria-hidden','true');this.element.style.display='none';
  }
  dispose(){this.disable();this.element.remove();}
}
