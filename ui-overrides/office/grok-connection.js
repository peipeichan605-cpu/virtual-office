// Human pairing stays in this page's memory. Only the selected Office route is used.
export function grokProofVerified(capability,check=capability?.connection_check){
  return !!(check?.verified===true&&check.received===true&&check.replied===true);
}
function historicalProof(check){
  const value=check?.verified_at,stamp=typeof value==='number'?value:typeof value==='string'&&value.trim()?Date.parse(value):NaN;
  return check?.received===true&&check.replied===true&&Number.isFinite(stamp)&&stamp>0;
}
export function grokConnectionView(capability,check=capability?.connection_check,{uncertain=false,saveUncertain=false}={}){
  if(capability?.reason==='agent_not_selected')return {state:'disabled',title:'這位 Agent 的身份已失效',message:'請由擁有人選擇有效邀請；沒有派送新的測試。'};
  if(capability?.reason==='dispatch_disabled'||check?.state==='disabled')return {state:'disabled',title:'派工已停用',message:'由 OFFICE 擁有人恢復派工後，才可繼續連接。'};
  if(uncertain)return {state:'unconfirmed',title:'測試送達未確認',message:'不會自動重送。請核對這次測試，沿用同一個測試編號。'};
  if(saveUncertain)return {state:'unconfirmed',title:'入口儲存結果需核對',message:'請明確按測試，確認目前入口屬於這位 Bot；沒有再次傳送連接資料。'};
  if(capability?.native_ready===false&&(check?.reason==='native_unavailable'||capability?.reason==='native_unavailable'))return historicalProof(check)
    ?{state:'waiting_platform',title:'已驗證 · 等待平台恢復',message:'先前已收到相配回覆；目前平台入口暫時不可用。'}
    :{state:'waiting_platform',title:'入口已設定 · 等待平台恢復',message:'目前平台入口暫時不可用；恢復後，再由你按測試連接。'};
  if(grokProofVerified(capability,check))return capability?.native_ready===true&&capability?.can_send===true
    ?{state:'verified',title:'已連接',message:'OFFICE 指示收發已通過測試。App 直接工作仍需 Agent 合作上報，未有原生自動同步。'}
    :{state:'waiting_platform',title:'已驗證 · 等待平台恢復',message:'先前已收到相配回覆；目前平台入口暫時不可用。'};
  const states={
    unconfigured:['尚未連接','按下面步驟，將 Grok 的兩項連接資料帶回這裡。'],
    ready_to_test:['入口已設定 · 尚未測試','需要收到這位 Bot 的測試回覆，才會顯示已連接。'],
    queued:['正在送出測試','測試已排隊；尚未確認 Bot 收到及回覆。'],
    waiting_reply:['Bot 已收到 · 等待回覆','OFFICE 正在等候這次測試的相配回覆。'],
    failed:['測試未成功','請查看原 Bot 的提示；確認原因後，再由你決定是否重新測試。'],
    unconfirmed:['測試結果未確認','不會自動重送。請查看原 Bot，再更新這裡的狀態。'],
    unsupported:['目前沒有可用入口','這個平台入口未能使用，不能以等待工具代替。'],
  };
  const state=check?.state||(!capability?.persistent?'unconfigured':'ready_to_test');
  if(state==='waiting_reply'&&check?.replied===true)return {state,title:'已收到回覆 · 正在確認完成',message:'回覆已保存；仍要等 Bot 的正常完成回報，才算驗證成功。'};
  if(state==='ready_to_test'&&check?.can_test===false&&capability?.persistent)return {state,title:'入口已設定 · 等待目前工作結束',message:'現有工作仍未結束。完成後，再由你按「測試連接」，不會自動另派測試。'};
  const [title,message]=states[state]||['測試結果未確認','請更新狀態；未有實際回覆前，不會顯示已連接。'];
  return {state,title,message};
}
export function grokAssistantSelected(target){
  return !!(target?.provider==='grok_bot'&&typeof target.agent_id==='string'&&target.agent_id.startsWith('grok_bot:')&&target.state==='connected');
}
export function grokAssistantEnabled(target){
  return grokAssistantSelected(target)&&target.conversation?.can_send===true&&target.conversation.reason!=='dispatch_disabled';
}
export function pairingDetailsReady({url='',key=''}={}){
  return pairingUrlReady(url)&&!!normalizePairingKey(key);
}
export function pairingUrlReady(value){try{const parsed=new URL(value.trim());return parsed.protocol==='https:'&&!parsed.username&&!parsed.password;}catch{return false;}}
export function normalizePairingKey(value){const text=value.trim();return text.match(/^(?:Authorization:\s*)?Bearer\s+(\S+)$/i)?.[1]||text;}

/** State machine is separate from the DOM so delivery/fencing can be checked without native Apps. */
function testIdentity(check){return typeof check?.test_id==='string'&&check.test_id?check.test_id:null;}
function routeIdentity(capability,check=capability?.connection_check){
  const value=capability?.connection_check?.route_revision??check?.route_revision;
  return typeof value==='string'&&value?value:null;
}
function proofSignature(capability,check){return JSON.stringify([capability?.persistent,capability?.native_ready,capability?.can_send,capability?.reason,check?.state,testIdentity(check),check?.verified,check?.received,check?.replied,check?.can_test,check?.reason]);}
export class GrokConnectionController{
  constructor({transport,makeId=()=>crypto.randomUUID(),onChange=()=>{},onSnapshot=()=>{}}){
    this.transport=transport;this.makeId=makeId;this.onChange=onChange;this.onSnapshot=onSnapshot;
    this.target=null;this.capability=null;this.check=null;this.epoch=0;this.revision=0;this.visible=false;this.busy=false;this.refreshing=false;this.uncertain=false;this.saveUncertain=false;this.pendingId=null;this.error='';
    this.routeRevision=null;this.retiredRoutes=new Set();this.retiredTests=new Set();this.revokedAgents=new Set();this.awaitingAuthority=false;this.candidateTestId=null;this.previousTestId=null;
  }
  resetScope(){this.epoch++;this.revision++;this.busy=false;this.refreshing=false;this.pendingId=null;this.uncertain=false;this.saveUncertain=false;this.error='';this.awaitingAuthority=false;this.candidateTestId=null;this.previousTestId=null;}
  publish(capability,check){this.check=check||null;this.capability=capability?{...capability,connection_check:this.check}:null;if(this.target)this.target={...this.target,conversation:this.capability};}
  emit(snapshot={}){if(this.target?.agent_id)this.onSnapshot({...snapshot,capability:this.capability,check:this.check},this.target.agent_id);this.onChange();}
  scheduleRead(){if(this.visible)queueMicrotask(()=>this.refresh());}
  switchRoute(token){
    if(this.routeRevision!==null&&token===null||token!==null&&this.retiredRoutes.has(token))return {accepted:false,changed:false};
    if(token===this.routeRevision)return {accepted:true,changed:false};
    if(this.routeRevision!==null)this.retiredRoutes.add(this.routeRevision);
    this.routeRevision=token;this.retiredTests.clear();this.resetScope();this.check=null;
    return {accepted:true,changed:true};
  }
  bind(target){
    const changed=this.target?.agent_id!==target?.agent_id;
    if(changed){this.resetScope();this.routeRevision=null;this.retiredRoutes.clear();this.retiredTests.clear();this.check=null;this.target=target;this.capability=null;}
    const agent=target?.agent_id;
    if(agent&&['revoked','expired'].includes(target?.state)){this.target=target;this.revoke(agent);return false;}
    if(agent&&this.revokedAgents.has(agent)){
      this.target={...target,state:'revoked'};this.publish({...target.conversation,can_send:false,native_ready:false,reason:'agent_not_selected'},null);this.onChange();return false;
    }
    const incoming=target?.conversation||null,incomingCheck=incoming?.connection_check||null;
    const route=this.switchRoute(routeIdentity(incoming,incomingCheck));
    if(!route.accepted){this.onChange();return false;}
    if(!changed&&!route.changed&&grokAssistantEnabled(this.target)&&!grokAssistantEnabled(target)){
      this.resetScope();this.target=target;this.publish(incoming,incomingCheck);this.onChange();return true;
    }
    if(!changed&&!route.changed&&!grokAssistantEnabled(this.target)&&grokAssistantEnabled(target)){
      if(!this.refreshing){this.revision++;this.scheduleRead();}return false;
    }
    const incomingId=testIdentity(incomingCheck),currentId=testIdentity(this.check);
    if(!changed&&!route.changed&&incomingId&&this.retiredTests.has(incomingId)){this.onChange();return false;}
    this.target=target;
    if(!changed&&!route.changed&&this.pendingId&&incomingId!==this.pendingId){this.publish(incoming,this.check);this.onChange();return true;}
    if(!changed&&!route.changed&&(this.uncertain||this.saveUncertain)){this.publish(incoming,this.check);this.onChange();return true;}
    if(!changed&&!route.changed&&this.awaitingAuthority){this.onChange();return false;}
    if(!changed&&!route.changed&&this.check&&incomingId!==currentId&&(incomingId||currentId)){
      this.resetScope();this.awaitingAuthority=true;this.previousTestId=currentId;this.candidateTestId=incomingId;
      const candidate=incomingCheck?.verified?{...incomingCheck,state:'unconfirmed',verified:false,can_test:false,can_retry:false}:incomingCheck;
      this.publish(incoming,candidate);this.onChange();this.scheduleRead();return true;
    }
    if(!changed&&!route.changed&&proofSignature(this.capability,this.check)!==proofSignature(incoming,incomingCheck)){this.revision++;this.refreshing=false;this.busy=false;}
    this.publish(incoming,incomingCheck);
    if(this.pendingId&&incomingId===this.pendingId)this.resolvePending(incomingCheck);
    this.onChange();if((changed||route.changed)&&this.visible&&grokAssistantSelected(this.target))this.scheduleRead();return true;
  }
  setVisible(visible){const changed=this.visible!==visible;this.visible=visible;if(changed&&visible&&grokAssistantSelected(this.target))this.refresh();}
  current(agent,epoch,revision){return this.target?.agent_id===agent&&this.epoch===epoch&&(revision===undefined||revision===this.revision)&&!this.revokedAgents.has(agent);}
  resolvePending(receipt){
    if(!this.pendingId)return;
    this.uncertain=testIdentity(receipt)!==this.pendingId;
    if(!this.uncertain){this.saveUncertain=false;if(grokProofVerified(this.capability,receipt)||historicalProof(receipt)||receipt?.state==='failed'&&receipt.can_retry===true)this.pendingId=null;}
  }
  apply(snapshot,agent,epoch,revision,{ownSave=false}={}){
    if(!this.current(agent,epoch,revision))return false;
    const capability=snapshot.capability||this.capability,latest=capability?.connection_check||snapshot.check||null;
    const route=this.switchRoute(routeIdentity(capability,latest));
    if(!route.accepted)return false;
    const id=testIdentity(latest);
    if(id&&this.retiredTests.has(id))return false;
    if(this.awaitingAuthority){
      if(this.previousTestId&&this.previousTestId!==id)this.retiredTests.add(this.previousTestId);
      if(this.candidateTestId&&this.candidateTestId!==id)this.retiredTests.add(this.candidateTestId);
      this.awaitingAuthority=false;this.previousTestId=null;this.candidateTestId=null;
    }else{const old=testIdentity(this.check);if(old&&old!==id)this.retiredTests.add(old);}
    this.publish(capability,latest);
    if(!route.changed||ownSave)this.resolvePending(snapshot.check||latest);
    if(!this.uncertain&&!this.saveUncertain&&(this.check?.can_test===true||grokProofVerified(this.capability,this.check)))this.error='';
    this.emit(snapshot);return !route.changed||ownSave;
  }
  revoke(agent){
    this.revokedAgents.add(agent);this.resetScope();this.target={...this.target,state:'revoked'};
    this.publish({...this.capability,can_send:false,native_ready:false,reason:'agent_not_selected'},null);
    this.error='這位 Agent 的身份已失效；請重新選擇有效邀請。';this.emit();
  }
  async refresh(force=false){
    if(!this.visible||this.refreshing||this.busy&&!force||!grokAssistantSelected(this.target)||this.revokedAgents.has(this.target.agent_id))return;
    const agent=this.target.agent_id,epoch=this.epoch,revision=this.revision;this.refreshing=true;
    try{this.apply(await this.transport.read(agent),agent,epoch,revision);}
    catch(error){if(this.current(agent,epoch,revision)){if(error.status===403||error.status===404)this.revoke(agent);else{this.error='暫時未能更新連接狀態，沒有重新發送測試。';this.onChange();}}}
    finally{if(this.current(agent,epoch,revision))this.refreshing=false;}
  }
  canStart(details){
    if(!this.visible||this.busy||this.awaitingAuthority||this.uncertain||this.saveUncertain&&!this.capability?.persistent||!grokAssistantEnabled(this.target)||this.revokedAgents.has(this.target.agent_id))return false;
    return this.capability?.persistent?this.check?.can_test===true:pairingDetailsReady(details);
  }
  async connectAndTest(details){
    if(!this.canStart(details))return false;
    const agent=this.target.agent_id;let epoch=this.epoch;this.revision++;let revision=this.revision;this.busy=true;this.error='';this.onChange();
    let phase=this.capability?.persistent?'test':'save';
    try{
      if(!this.capability?.persistent){
        await this.transport.save(agent,{url:details.url.trim(),key:normalizePairingKey(details.key)});
        if(!this.current(agent,epoch,revision)||!this.visible)return false;
        const snapshot=await this.transport.read(agent);
        if(!this.apply(snapshot,agent,epoch,revision,{ownSave:true})||!this.visible)return false;
        epoch=this.epoch;revision=this.revision;this.busy=true;
        if(this.check?.can_test!==true){this.error='入口已設定；請等待目前工作結束，再按「測試連接」。';return false;}
      }
      if(!this.visible||!this.current(agent,epoch,revision))return false;
      phase='test';this.pendingId=this.makeId();this.publish(this.capability,null);this.onChange();
      return this.apply(await this.transport.start(agent,this.pendingId),agent,epoch,revision);
    }catch(error){
      if(this.current(agent,epoch,revision)){
        if(phase==='test'&&(!error.status||error.status>=500)){this.uncertain=true;this.error='測試送達未確認；不會自動重送或建立另一個測試。';}
        else if(phase==='save'&&(!error.status||error.status>=500)){this.saveUncertain=true;this.error='儲存結果未確認，正在查回入口狀態；沒有再次傳送連接資料。';}
        else this.error=error.status===409?'目前仍有工作或測試未結束；請先更新狀態。':'未能完成這一步，請核對 Grok 顯示的資料及平台提示。';
        await this.refresh(true);
      }
      return false;
    }finally{if(this.current(agent,epoch,revision)){this.busy=false;this.onChange();}}
  }
  async resolve(){
    if(!this.visible||this.busy||!this.uncertain||!this.pendingId||!grokAssistantEnabled(this.target))return;
    const agent=this.target.agent_id,epoch=this.epoch,id=this.pendingId;this.revision++;const revision=this.revision;this.busy=true;this.error='';this.onChange();
    try{this.apply(await this.transport.start(agent,id),agent,epoch,revision);}
    catch{if(this.current(agent,epoch,revision))this.error='這次測試仍未確認；已沿用同一編號，沒有另建測試。';}
    finally{if(this.current(agent,epoch,revision)){this.busy=false;this.onChange();}}
  }
}

export class GrokConnectionAssistant{
  constructor({transport,isVisible=()=>true,onSnapshot=()=>{},onDone=()=>{},clipboard=()=>navigator.clipboard.readText()}){
    this.isVisible=isVisible;this.clipboard=clipboard;this.details={url:'',key:''};this.identity=null;this.generation=0;this.timer=null;this.stage=1;
    this.element=document.createElement('section');this.element.className='grok-connection-panel';this.element.hidden=true;
    this.element.innerHTML=`<div class="connection-heading"><span class="connection-symbol" aria-hidden="true">↔</span><div><h3>Grok 連接助手</h3><p>收到測試回覆，先算連接成功。</p></div></div>
      <div class="connection-state" role="status" aria-live="polite"><strong></strong><p></p></div>
      <ol class="connection-stage-indicator" aria-label="連接進度"><li data-stage="1">1 找到流程</li><li data-stage="2">2 收訊地址</li><li data-stage="3">3 連接金鑰</li><li data-stage="4">4 測試</li></ol>
      <div class="connection-pairing">
        <section class="connection-pane" data-pane="1"><h4>找到 Bot 回覆嘅「OFFICE 連接卡」</h4><p>按卡片所列流程嘅「開啟例行任務」。亦可點 Grok 頂部 Bot 名稱 → 動態 → 即將進行，打開卡片所列嘅完整流程名稱。</p><p class="connection-routine-name"></p><p>列表顯示嘅係流程名稱；打開正確流程後，入面先有收訊地址。已有入口請以 Bot 連接卡列出嘅實際名稱為準。</p><div class="connection-illustration" aria-label="按連接卡的流程名稱開啟"><span>OFFICE 連接卡</span><b aria-hidden="true">→</b><span>同名例行任務</span></div><button class="invite-primary connection-next" data-next="2" type="button">我已找到指定流程或地址</button></section>
        <section class="connection-pane" data-pane="2" hidden><h4>貼上 Grok 收訊地址</h4><p>如果 Bot 連接卡已列出地址，直接複製。否則打開卡片指定嘅流程，在 Webhook 區按「複製 Webhook URL」。</p><small>欄位亦可能叫「POST 到」或「POST to」。流程名稱本身唔係地址。</small><label>收訊地址（Webhook URL）<input class="connection-first" type="password" maxlength="8192" autocomplete="off" placeholder="貼上 https:// 開頭的收訊地址" spellcheck="false"></label><button class="connection-paste" data-paste="url" type="button">貼上收訊地址</button><button class="invite-primary connection-next" data-next="3" type="button">下一步</button></section>
        <section class="connection-pane" data-pane="3" hidden><h4>貼上同一流程嘅連接金鑰</h4><p>在剛才同一個流程內按「複製 Webhook 金鑰」，返嚟貼上。金鑰毋須貼入聊天，呢度會保持隱藏。</p><small>Grok 欄位：金鑰／key</small><label>連接金鑰（Webhook key）<input class="connection-second" type="password" maxlength="8192" autocomplete="off" placeholder="貼上同一流程的連接金鑰" spellcheck="false"></label><button class="connection-paste" data-paste="key" type="button">貼上連接金鑰</button><button class="invite-primary connection-next" data-next="4" type="button">下一步</button></section>
        <details class="connection-advanced" hidden><summary>進階設定（手動貼上）</summary><label>Webhook URL<input class="connection-url" type="url" maxlength="8192" autocomplete="off" spellcheck="false"></label><label>Webhook key<input class="connection-key" type="password" maxlength="8192" autocomplete="off" spellcheck="false"></label></details>
      </div>
      <section class="connection-pane connection-test" data-pane="4" hidden><h4>確認 Bot 真正收到</h4><p class="connection-existing" hidden>沿用已保存的入口，毋須再複製資料。</p><p class="connection-warning">按「連接並測試」會傳送無害測試；原本待處理嘅留言亦可能開始交付。</p><button class="invite-primary connection-start" type="button">連接並測試</button><button class="connection-refresh" type="button">更新狀態</button><ul class="connection-proof"><li data-proof="sent">OFFICE 已建立測試</li><li data-proof="received">Bot 已收到測試</li><li data-proof="replied">OFFICE 已收到相配回覆</li></ul><button class="invite-primary connection-done" type="button" hidden>完成</button></section>
      <button class="connection-back" type="button" hidden>← 上一步</button><p class="connection-notice" role="alert"></p>`;
    this.state=this.element.querySelector('.connection-state');this.notice=this.element.querySelector('.connection-notice');
    this.pairing=this.element.querySelector('.connection-pairing');this.existing=this.element.querySelector('.connection-existing');
    this.start=this.element.querySelector('.connection-start');this.refreshButton=this.element.querySelector('.connection-refresh');
    this.back=this.element.querySelector('.connection-back');this.done=this.element.querySelector('.connection-done');this.advanced=this.element.querySelector('.connection-advanced');
    this.inputs={url:[this.element.querySelector('.connection-first'),this.element.querySelector('.connection-url')],key:[this.element.querySelector('.connection-second'),this.element.querySelector('.connection-key')]};
    this.controller=new GrokConnectionController({transport,onChange:()=>this.render(),onSnapshot:(snapshot,agent)=>{if(snapshot.capability?.persistent){this.clearDetails();this.stage=4;}onSnapshot(snapshot,agent);}});
    for(const [name,fields] of Object.entries(this.inputs))for(const field of fields)field.addEventListener('input',()=>{this.details[name]=field.value;for(const sibling of fields)if(sibling!==field)sibling.value=field.value;this.render();});
    for(const button of this.element.querySelectorAll('[data-paste]'))button.addEventListener('click',()=>this.paste(button.dataset.paste));
    this.start.addEventListener('click',()=>this.controller.connectAndTest(this.details));
    this.refreshButton.addEventListener('click',()=>this.controller.uncertain?this.controller.resolve():this.controller.refresh());
    for(const button of this.element.querySelectorAll('[data-next]'))button.addEventListener('click',()=>this.setStage(Number(button.dataset.next)));
    this.back.addEventListener('click',()=>this.setStage(this.stage-1));this.done.addEventListener('click',onDone);
  }
  setStage(stage){
    if(this.controller.busy||!grokAssistantEnabled(this.controller.target)||stage<1||stage>4)return;
    if(stage>this.stage&&(this.stage===2&&!pairingUrlReady(this.details.url)||this.stage===3&&!normalizePairingKey(this.details.key)))return;
    this.stage=stage;this.render();
  }
  clearDetails(){this.details={url:'',key:''};for(const fields of Object.values(this.inputs))for(const field of fields)field.value='';}
  bind(target){
    const routineName=this.element.querySelector('.connection-routine-name');
    routineName.textContent=typeof target?.grok_setup?.suggested_routine_name==='string'?'新入口名稱：'+target.grok_setup.suggested_routine_name:'';
    routineName.hidden=!routineName.textContent;
    if(this.identity!==target?.agent_id){this.identity=target?.agent_id;this.generation++;this.clearDetails();this.notice.textContent='';this.stage=target?.conversation?.persistent?4:1;}
    const previousRoute=this.controller.routeRevision;
    this.controller.bind(target);
    if(this.controller.capability?.persistent)this.stage=4;
    else if(previousRoute!==this.controller.routeRevision){this.clearDetails();this.stage=1;}
    this.element.hidden=!grokAssistantSelected(target);this.syncVisibility();this.render();
  }
  syncVisibility(){
    const visible=!this.element.hidden&&this.isVisible(),wasVisible=this.controller.visible;this.controller.setVisible(visible);
    if(!visible){this.generation++;if(wasVisible)this.clearDetails();clearInterval(this.timer);this.timer=null;}
    else if(!this.timer)this.timer=setInterval(()=>{if(!this.isVisible()){this.syncVisibility();return;}this.controller.refresh();},2000);
  }
  async paste(name){
    if(!this.controller.visible||!grokAssistantEnabled(this.controller.target)||this.controller.busy||this.controller.capability?.persistent)return;
    const identity=this.identity,generation=this.generation,epoch=this.controller.epoch;
    const current=()=>identity===this.identity&&generation===this.generation&&epoch===this.controller.epoch&&this.controller.visible&&grokAssistantEnabled(this.controller.target)&&!this.controller.busy&&!this.controller.capability?.persistent;
    try{
      const value=await this.clipboard();
      if(!current())return;
      this.details[name]=value.trim().slice(0,8192);for(const input of this.inputs[name])input.value=this.details[name];
      this.notice.textContent=name==='url'?'已貼上收訊地址。':'已貼上連接金鑰，內容保持隱藏。';this.render();
    }catch{
      if(!current())return;
      this.notice.textContent='未能讀取剪貼簿。請手動貼上剛才複製的資料。';this.inputs[name][0].focus();
    }
  }
  render(){
    const c=this.controller;if(!c)return;
    const view=grokConnectionView(c.capability,c.check,{uncertain:c.uncertain,saveUncertain:c.saveUncertain}),enabled=grokAssistantEnabled(c.target);
    this.state.dataset.state=view.state;this.state.querySelector('strong').textContent=view.title;this.state.querySelector('p').textContent=view.message;
    this.pairing.hidden=!!c.capability?.persistent;this.existing.hidden=!c.capability?.persistent;
    for(const pane of this.element.querySelectorAll('[data-pane]'))pane.hidden=Number(pane.dataset.pane)!==this.stage;
    for(const item of this.element.querySelectorAll('[data-stage]')){item.dataset.active=String(Number(item.dataset.stage)===this.stage);item.setAttribute('aria-current',Number(item.dataset.stage)===this.stage?'step':'false');}
    this.advanced.hidden=!([2,3].includes(this.stage))||!!c.capability?.persistent;
    this.back.hidden=this.stage===1||!!c.capability?.persistent||c.busy;this.back.disabled=!enabled;
    for(const button of this.element.querySelectorAll('[data-next]'))button.disabled=!enabled||c.busy||(Number(button.dataset.next)===3&&!pairingUrlReady(this.details.url))||(Number(button.dataset.next)===4&&!normalizePairingKey(this.details.key));
    this.start.disabled=!c.canStart(this.details);
    this.start.textContent=c.busy?'正在連接及核對…':c.capability?.persistent?(c.check?.can_retry?'重新測試':'測試連接'):'連接並測試';
    this.start.hidden=view.state==='verified';this.done.hidden=view.state!=='verified';this.element.querySelector('.connection-warning').hidden=view.state==='verified';
    this.refreshButton.disabled=c.busy||!enabled;this.refreshButton.textContent=c.uncertain?'核對這次測試':'更新狀態';
    this.refreshButton.hidden=view.state==='verified';
    for(const input of this.element.querySelectorAll('input,[data-paste]'))input.disabled=c.busy||!enabled||!!c.capability?.persistent;
    const proof=c.uncertain?{}:c.check||{};
    for(const [name,ok] of Object.entries({sent:!!proof.test_id,received:proof.received===true,replied:proof.replied===true}))this.element.querySelector('[data-proof="'+name+'"]').dataset.done=String(ok);
    if(c.error)this.notice.textContent=c.error;
  }
}
