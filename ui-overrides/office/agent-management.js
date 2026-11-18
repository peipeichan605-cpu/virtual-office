import {logoutAgent} from './office-controls.js';
import {openCodexModelDialog} from './codex-model-ui.js';

/** History is retained after logout, but management belongs only to current members. */
export function initAgentManagement(element,agentId,{document:doc=document,events=window,logout=logoutAgent,model=openCodexModelDialog}={}){
  const leave=doc.createElement('button'),settings=doc.createElement('button');
  leave.type=settings.type='button';leave.textContent='登出 OFFICE';settings.textContent='模型設定';
  element.append(settings,leave);element.hidden=true;
  let member=null,loggedOut=false;
  leave.addEventListener('click',()=>{if(member&&!loggedOut)logout({...member});});
  settings.addEventListener('click',()=>{if(member&&!loggedOut&&!settings.hidden)model(agentId);});
  const onLogout=event=>{if(event.detail?.agent_id===agentId){loggedOut=true;member=null;element.hidden=true;settings.hidden=true;leave.disabled=true;}};
  events.addEventListener('office:agent-logged-out',onLogout);
  return {update(agent){
    member=!loggedOut&&agent?.id===agentId&&agent.selected===true&&!agent.is_subagent?{agent_id:agentId,name:agent.name}:null;
    element.hidden=!member;settings.hidden=!member||!/^codex:bg-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(agentId);
    leave.disabled=!member;
  },dispose(){events.removeEventListener('office:agent-logged-out',onLogout);member=null;loggedOut=true;settings.remove();leave.remove();element.hidden=true;}};
}
