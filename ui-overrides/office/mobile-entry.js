import {waitForMobileSession,isCurrentMobileSession,currentMobileSessionFor} from './mobile-access.js';
import {mountMobileSessionControls} from './mobile-session-controls.js';

export async function startMobileOffice({load = () => import('./app.js'), doc = document} = {}) {
  doc.documentElement.classList.add('office-mobile');
  // Pairing tokens belong only on the pairing screen. Never keep a secret hash.
  if(location.hash)history.replaceState(null,'',location.pathname+location.search);
  const status = state => {
    const label=doc.querySelector('.hq-loading-label');
    if(label)label.textContent=state==='paused'?'暫時連不到 OFFICE。私人連線恢復或返回此頁後會再試。'
      :state==='retrying'?'暫時連不到 OFFICE，正在重試…':'正在連接私人 OFFICE…';
  };
  try {
    const current=await waitForMobileSession({onStatus:status, document:doc});
    if(!isCurrentMobileSession(current))return;
    await load();
    const latest=currentMobileSessionFor(current);
    if(latest)mountMobileSessionControls({doc,initial:latest});
  } catch (error) {
    if(error.code==='mobile_session_changed')return;
    const label=doc.querySelector('.hq-loading-label');
    if(label)label.textContent=error.status===401?'請重新配對手機。':error.status===403?'手機存取未獲准，請檢查 OFFICE 授權。':'暫時未能開啟 OFFICE，請重新載入。';
  }
}

if(typeof document!=='undefined')startMobileOffice();
