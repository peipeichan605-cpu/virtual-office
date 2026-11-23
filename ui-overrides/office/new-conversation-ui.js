import {initOpenRouterUI} from './openrouter-ui.js';
import {initCursorSdkUI} from './cursor-sdk-ui.js';
import {initCodexBackgroundUI} from './codex-background-ui.js';
import {initGrokSdkUI} from './grok-sdk-ui.js';

/** Platform-neutral entry: no platform is selected or contacted until the user chooses it. */
export function initNewConversationUI({document:doc=document,events=window,key,cursorOptions={},codexOptions={},grokOptions={}}={}){
  const cursor=initCursorSdkUI({...cursorOptions,document:doc,events,key,openEvent:'office:new-cursor-conversation'});
  const codex=initCodexBackgroundUI({...codexOptions,document:doc,events,key});
  const grok=initGrokSdkUI({...grokOptions,document:doc,events,key});
  let router;
  function openRouter(){
    if(!router)router=initOpenRouterUI({document:doc,events,key});
    router.open();
  }
  events.addEventListener('office:new-openrouter-conversation',openRouter);
  const dialog=doc.createElement('dialog');dialog.className='office-invite-dialog office-new-conversation-dialog';dialog.setAttribute('aria-labelledby','office-new-conversation-heading');
  dialog.innerHTML=`<button class="invite-close" type="button" aria-label="關閉平台選擇">×</button>
    <div class="invite-eyebrow">VIRTUAL OFFICE</div><h2 id="office-new-conversation-heading">加入 Agent</h2>
    <p class="invite-intro">直接建立背景 Agent，或接入你已有的 App／Cloud Agent。請選擇加入方式。</p>
    <div class="office-platform-choices"><button class="invite-primary office-platform-cursor" type="button">Cursor SDK · 純對話</button>
      <p class="invite-footnote">直接在 OFFICE 建立及對話，毋須複製入場 Prompt。</p>
      <button class="invite-primary office-platform-codex" type="button">Codex · 背景模式</button>
      <p class="invite-footnote">沿用本機 Codex 登入，優先使用 Desktop 內置執行器；毋須複製入場 Prompt。</p>
      <button class="invite-primary office-platform-grok" type="button">Grok Bot SDK · 實驗模式</button>
      <p class="invite-footnote">登入後選擇已有的 Bot 及聊天；使用非官方 SDK，接駁後需驗證實際收發。</p>
      <button class="invite-primary office-platform-openrouter" type="button">OpenRouter · 免費對話</button>
      <p class="invite-footnote">選擇免費模型，在 OFFICE 建立獨立 Agent。</p>
      <button class="invite-primary office-platform-existing" type="button">接入現有 App／Cloud Agent</button>
      <p class="invite-footnote">保留已有的 Codex App、Cursor Cloud、Grok Bot 或其他 Agent，按平台完成接入。</p></div>`;
  doc.body.append(dialog);
  const close=()=>{if(dialog.open)dialog.close();};
  const open=()=>{if(!dialog.open&&!cursor.dialog.open&&!codex.dialog.open&&!grok.dialog.open&&!router?.dialog.open)dialog.showModal();};
  dialog.querySelector('.invite-close').addEventListener('click',close);
  dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
  for(const [selector,event] of [['.office-platform-cursor','office:new-cursor-conversation'],['.office-platform-codex','office:new-codex-conversation'],['.office-platform-grok','office:new-grok-sdk-conversation'],['.office-platform-openrouter','office:new-openrouter-conversation'],['.office-platform-existing','office:invite-existing-agent']]){
    dialog.querySelector(selector).addEventListener('click',()=>{close();events.dispatchEvent(new CustomEvent(event));});
  }
  events.addEventListener('office:new-conversation',open);
  return {dialog,cursor,codex,grok,dispose(){events.removeEventListener('office:new-conversation',open);events.removeEventListener('office:new-openrouter-conversation',openRouter);router?.dispose();close();dialog.remove();cursor.dispose();codex.dispose();grok.dispose();}};
}
