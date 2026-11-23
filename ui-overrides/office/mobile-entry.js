import {waitForMobileSession,isCurrentMobileSession,currentMobileSessionFor} from './mobile-access.js';
import {mountMobileSessionControls} from './mobile-session-controls.js';

const ASSET_RETRY_KEY='office.mobile.sceneRetried';

async function assetRetrySeconds(){
  // Browsers hide the HTTP status when importing a module fails. Check one
  // small known asset on the same gateway budget; ignore its body entirely.
  try{
    const response=await fetch('/office/mobile-entry.js',{cache:'no-store',credentials:'same-origin',redirect:'error',signal:AbortSignal.timeout(5000)});
    response.body?.cancel().catch(()=>{});
    if(response.status!==429)return 0;
    const seconds=response.headers.get('Retry-After');
    return /^(?:[1-9]|[1-5][0-9]|60)$/.test(seconds)?Number(seconds):0;
  }catch{return 0;}
}

function reserveAutoRetry(){
  try{
    if(sessionStorage.getItem(ASSET_RETRY_KEY))return false;
    sessionStorage.setItem(ASSET_RETRY_KEY,'1');return true;
  }catch{return false;}
}

async function showStartupError(doc,error,current) {
  if(error?.code==='mobile_session_changed')return;
  const label=doc.querySelector('.hq-loading-label');
  if(!label)return;
  if(error?.status===401||error?.status===403){
    label.textContent=error.status===401?'請重新配對手機。':'手機存取未獲准，請檢查 OFFICE 授權。';
    return;
  }

  // Match browser import errors and the pinned renderer without displaying
  // their raw messages, which can contain the private host or resource URL.
  const message=String(error?.message||'');
  const moduleFailed=current&&error instanceof TypeError&&/^(?:failed to fetch dynamically imported module|error loading dynamically imported module|importing a module script failed)\b/i.test(message);
  if(!current)label.textContent='未能讀取手機登入狀態。請確認手機的 Tailscale 已連線，再試一次。';
  else if(/^Error creating WebGL context(?: with your selected attributes)?\.?$/.test(message)){
    label.textContent='瀏覽器未能啟動 3D 顯示。請關閉其他使用 3D 的分頁，再試一次。';
  }else if(moduleFailed){
    label.textContent='未能載入辦公室的畫面檔案。請確認手機的 Tailscale 已連線，再試一次。';
  }else label.textContent='辦公室畫面未能啟動，請重新開啟。';

  const panel=label.parentElement;
  panel.classList.add('mobile-start-error');
  const retry=doc.createElement('button');
  retry.type='button';retry.textContent='重新開啟 OFFICE';
  retry.addEventListener('click',()=>location.reload(),{once:true});
  panel.append(retry);
  if(!moduleFailed)return;
  retry.disabled=true;
  const seconds=await assetRetrySeconds();
  if(!currentMobileSessionFor(current))return;
  if(!seconds){retry.disabled=false;return;}
  const auto=reserveAutoRetry();
  label.textContent=auto?`短時間內開啟次數較多。約 ${seconds} 秒後會自動重新開啟 OFFICE。`
    :`短時間內開啟次數較多。請等候約 ${seconds} 秒，再按下方按鈕。`;
  setTimeout(()=>{
    if(!currentMobileSessionFor(current))return;
    if(auto&&doc.visibilityState!=='hidden'){location.reload();return;}
    label.textContent='現在可以重新開啟 OFFICE。';retry.disabled=false;
  },seconds*1000);
}

export async function startMobileOffice({load = () => import('./app.js'), doc = document} = {}) {
  doc.documentElement.classList.add('office-mobile');
  // Pairing tokens belong only on the pairing screen. Never keep a secret hash.
  if(location.hash)history.replaceState(null,'',location.pathname+location.search);
  const status = state => {
    const label=doc.querySelector('.hq-loading-label');
    if(label)label.textContent=state==='paused'?'暫時連不到 OFFICE。私人連線恢復或返回此頁後會再試。'
      :state==='retrying'?'暫時連不到 OFFICE，正在重試…':'正在連接私人 OFFICE…';
  };
  let current;
  try {
    current=await waitForMobileSession({onStatus:status,document:doc});
  } catch (error) {
    await showStartupError(doc,error,null);return;
  }
  if(!isCurrentMobileSession(current))return;
  try {
    await load();
  } catch (error) {
    if(currentMobileSessionFor(current))await showStartupError(doc,error,current);
    return;
  }
  const latest=currentMobileSessionFor(current);
  if(latest){
    try{sessionStorage.removeItem(ASSET_RETRY_KEY);}catch{/* Storage can be disabled. */}
    try { mountMobileSessionControls({doc,initial:latest}); }
    catch { console.warn('OFFICE 手機登入設定暫時未能開啟。'); }
  }
}

if(typeof document!=='undefined')startMobileOffice();
