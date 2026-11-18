import { officeGet, mergeRunRows } from './office-api.js';
import { renderHistorySyncWarning } from './history-detail.js';
import {initAgentManagement} from './agent-management.js';
import {isMobileAccess,waitForMobileSession,isCurrentMobileSession} from './mobile-access.js';
async function startAgentPage() {
const mobile=isMobileAccess();
if(mobile){
  document.documentElement.classList.add('office-mobile');
  if(location.hash)history.replaceState(null,'',location.pathname+location.search);
  const status=state=>{const label=document.getElementById('connection');if(label)label.textContent=state==='paused'?'暫時連不到 OFFICE。私人連線恢復或返回此頁後會再試。':state==='retrying'?'暫時連不到 OFFICE，正在重試…':'正在連接私人 OFFICE…';};
  try { const current=await waitForMobileSession({onStatus:status});if(!isCurrentMobileSession(current))return; }
  catch(error){if(error.status!==401&&error.code!=='mobile_session_changed'){const label=document.getElementById('connection');if(label)label.textContent='手機存取未獲准，請檢查 OFFICE 授權。';}return;}
}
const params = new URLSearchParams(location.search), agentId = params.get('agent');
const key = decodeURIComponent(location.hash.slice(1));
const $ = (id) => document.getElementById(id);
const management=mobile?{update(){}}:initAgentManagement($('agent-management'),agentId);
const labels = { working: '工作中', done: '已完成', error: '執行失敗', cancelled: '已中止', unknown: '狀態待確認' };
let selected = params.get('run'), rows = [], next = null, busy = false, detailRequest = 0;
let listSignature = '';
let stopped = false, interval = null, suspended = false;
const paused = () => mobile && (suspended || document.visibilityState==='hidden');
const pause = () => {if(interval!==null)clearInterval(interval);interval=null;detailRequest++;};
const stop = () => {
  stopped = true;pause();
  globalThis.removeEventListener?.('office:mobile-locked',stop);
  globalThis.removeEventListener?.('pagehide',pagehide);
  globalThis.removeEventListener?.('pageshow',pageshow);
  document.removeEventListener?.('visibilitychange',visibility);
};
function resume(){if(stopped||paused())return;if(interval===null)interval=setInterval(()=>refresh(),5000);refresh();}
function pagehide(event){if(event?.persisted){suspended=true;pause();}else stop();}
function pageshow(){if(suspended){suspended=false;resume();}}
function visibility(){if(paused())pause();else resume();}
if(mobile){globalThis.addEventListener?.('office:mobile-locked',stop);globalThis.addEventListener?.('pagehide',pagehide);globalThis.addEventListener?.('pageshow',pageshow);document.addEventListener?.('visibilitychange',visibility);}
$('back').href = mobile?'/mobile/':'/office/#' + encodeURIComponent(key);
const date = (stamp) => stamp ? new Date(stamp).toLocaleString('zh-HK', { month:'short',day:'numeric',hour:'2-digit',minute:'2-digit' }) : '';
function notice(text, error=false) { const label=$('connection');if(!stopped&&label){label.textContent=text;label.classList.toggle('error',error);} }
function drawList() {
  const signature=JSON.stringify([rows,selected,next]);
  if(signature===listSignature)return;
  listSignature=signature;
  $('run-list').replaceChildren();
  if (!rows.length) { const p=document.createElement('p');p.textContent='尚未取得工作記錄。';$('run-list').append(p); }
  for (const row of rows) {
    const button=document.createElement('button');button.className='run'+(row.id===selected?' selected':'');
    const title=document.createElement('strong');title.textContent=(row.prompt_preview || row.delivery_preview || '工作記錄').slice(0,88);
    const sub=document.createElement('small');sub.textContent=date(row.started_at)+' · '+(labels[row.status]||'待確認');
    button.append(title,sub);button.addEventListener('click',()=>{selected=row.id;const q=new URLSearchParams(location.search);q.set('run',selected);history.replaceState(null,'','?'+q+location.hash);drawList();loadDetail();});
    $('run-list').append(button);
  }
  $('more').hidden=!next;
}
async function loadDetail() {
  if (stopped || paused() || !selected) return;
  const id=selected, request=++detailRequest;
  try {
    const row=await officeGet('/api/office/run?id='+encodeURIComponent(id),key);
    if(stopped||paused()||request!==detailRequest||id!==selected)return;
    $('run-status').textContent=labels[row.status]||'狀態待確認';$('run-status').dataset.status=row.status;
    $('run-time').textContent=date(row.started_at)+(row.finished_at?' → '+date(row.finished_at):'');
    renderHistorySyncWarning($('sync-warning'),row.sync_warning);
    $('prompt').textContent=row.prompt||'這筆舊紀錄未取得原始任務文字。';
    $('delivery').textContent=row.delivery||(row.status==='working'?'工作進行中，尚未交付。':row.status==='unknown'?'未能確認來源狀態；沒有推斷完成。':'來源沒有提供文字交付。');
    $('run-ref').textContent='工作編號 '+row.run_id;
  } catch(error) {notice(error.message,true);}
}
async function refresh(more=false) {
  if(stopped||paused()||busy)return;busy=true;
  try {
    if(!agentId||(!mobile&&!key))throw new Error('請從私人 OFFICE 的 Agent 卡片開啟。');
    const suffix=more&&next?'&before='+encodeURIComponent(next):'';
    const data=await officeGet('/api/office/history?agent='+encodeURIComponent(agentId)+suffix,key);
    if(stopped||paused())return;
    $('name').textContent=data.agent.name;document.title=data.agent.name+' · 對話與交付';
    management.update(data.agent);
    $('provider-note').textContent=({codex:'查看本聊天收到的任務及交付；需要繼續對話時，可開啟 Codex 原聊天。',cursor:'這裡保存選定 Cursor Agent 的公開任務、回覆及交付。',grok_bot:'這裡保存 Bot 主動回報的任務及交付；Webhook 接收不代表完成。',hermes:'這裡保存同一個 Hermes 原生 session 的公開任務及交付。',crouton:'Crouton 按需執行工作；這裡保留各次任務與回覆，並非持續在線的聊天視窗。'}[data.agent.provider]||'這裡保存選定 Agent 的公開任務與交付紀錄。');
    if(data.agent.is_subagent)$('provider-note').textContent='子 Agent · 這裡保存主 Agent 指派的任務、公開進度及交付。角色收起後，紀錄仍會保留。';
    else if(mobile)$('provider-note').textContent='這裡保存你獲准查看的公開任務、對話與交付。若已獲准發訊息，可回到 3D 辦公室，點 Agent 的對話入口繼續。';
    const native=data.agent.native_url;
    const nativePrefix='codex://threads/';
    $('native').hidden=mobile||!(typeof native==='string'&&((native.startsWith(nativePrefix)&&/^[a-f0-9-]{36}$/.test(native.slice(nativePrefix.length)))||/^https:\/\/cursor\.com\/agents\/bc-[a-f0-9-]{36}$/.test(native)));
    if(!$('native').hidden)$('native').href=native;
    rows=mergeRunRows(rows,data.runs);
    if(!more&&rows.length<=60||more)next=data.next;
    if(!selected)selected=rows[0]?.id;
    drawList();notice((mobile?'私人紀錄已更新 · ':'本機記錄已更新 · ')+new Date().toLocaleTimeString('zh-HK'));
    await loadDetail();
  } catch(error) {notice(error.message+' 畫面保留最後一次取得的內容。',true);}
  finally {busy=false;}
}
$('more').addEventListener('click',()=>refresh(true));
resume();
}
export const agentPageReady = startAgentPage();
