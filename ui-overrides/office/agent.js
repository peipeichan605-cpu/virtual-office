import { officeGet, mergeRunRows } from './office-api.js';
const params = new URLSearchParams(location.search), agentId = params.get('agent');
const key = decodeURIComponent(location.hash.slice(1));
const $ = (id) => document.getElementById(id);
const labels = { working: '工作中', done: '已完成', error: '執行失敗', cancelled: '已中止', unknown: '狀態待確認' };
let selected = params.get('run'), rows = [], next = null, busy = false, detailRequest = 0;
let listSignature = '';
$('back').href = '/office/#' + encodeURIComponent(key);
const date = (stamp) => stamp ? new Date(stamp).toLocaleString('zh-HK', { month:'short',day:'numeric',hour:'2-digit',minute:'2-digit' }) : '';
function notice(text, error=false) { $('connection').textContent=text; $('connection').classList.toggle('error',error); }
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
  if (!selected) return;
  const id=selected, request=++detailRequest;
  try {
    const row=await officeGet('/api/office/run?id='+encodeURIComponent(id),key);
    if(request!==detailRequest||id!==selected)return;
    $('run-status').textContent=labels[row.status]||'狀態待確認';$('run-status').dataset.status=row.status;
    $('run-time').textContent=date(row.started_at)+(row.finished_at?' → '+date(row.finished_at):'');
    $('prompt').textContent=row.prompt||'這筆舊紀錄未取得原始任務文字。';
    $('delivery').textContent=row.delivery||(row.status==='working'?'工作進行中，尚未交付。':row.status==='unknown'?'未能確認來源狀態；沒有推斷完成。':'來源沒有提供文字交付。');
    $('run-ref').textContent='工作編號 '+row.run_id;
  } catch(error) {notice(error.message,true);}
}
async function refresh(more=false) {
  if(busy)return;busy=true;
  try {
    if(!agentId||!key)throw new Error('請從私人 OFFICE 的 Agent 卡片開啟。');
    const suffix=more&&next?'&before='+encodeURIComponent(next):'';
    const data=await officeGet('/api/office/history?agent='+encodeURIComponent(agentId)+suffix,key);
    $('name').textContent=data.agent.name;document.title=data.agent.name+' · 對話與交付';
    $('provider-note').textContent=({codex:'查看本聊天收到的任務及交付；需要繼續對話時，可開啟 Codex 原聊天。',cursor:'這裡保存選定 Cursor Agent 的公開任務、回覆及交付。',grok_bot:'這裡保存 Bot 主動回報的任務及交付；Webhook 接收不代表完成。',hermes:'這裡保存同一個 Hermes 原生 session 的公開任務及交付。',crouton:'Crouton 按需執行工作；這裡保留各次任務與回覆，並非持續在線的聊天視窗。'}[data.agent.provider]||'這裡保存選定 Agent 的公開任務與交付紀錄。');
    if(data.agent.is_subagent)$('provider-note').textContent='子 Agent · 這裡保存主 Agent 指派的任務、公開進度及交付。角色收起後，紀錄仍會保留。';
    const native=data.agent.native_url;
    const nativePrefix='codex://threads/';
    $('native').hidden=!(typeof native==='string'&&((native.startsWith(nativePrefix)&&/^[a-f0-9-]{36}$/.test(native.slice(nativePrefix.length)))||/^https:\/\/cursor\.com\/agents\/bc-[a-f0-9-]{36}$/.test(native)));
    if(!$('native').hidden)$('native').href=native;
    rows=mergeRunRows(rows,data.runs);
    if(!more&&rows.length<=60||more)next=data.next;
    if(!selected)selected=rows[0]?.id;
    drawList();notice('本機記錄已更新 · '+new Date().toLocaleTimeString('zh-HK'));
    await loadDetail();
  } catch(error) {notice(error.message+' 畫面保留最後一次取得的內容。',true);}
  finally {busy=false;}
}
$('more').addEventListener('click',()=>refresh(true));
refresh();setInterval(()=>refresh(),5000);
