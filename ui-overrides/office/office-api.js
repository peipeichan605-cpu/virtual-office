import {isMobileAccess,mobileRoute,mobileRequest} from './mobile-access.js';

export function agentPageUrl(agent, topic, run = '') {
  const query = new URLSearchParams({ agent });
  if (run) query.set('run', run);
  return isMobileAccess() ? `/mobile/agent?${query}` : `/office/agent.html?${query}#${encodeURIComponent(topic)}`;
}
export function mergeRunRows(previous, incoming) {
  const rows = new Map(previous.map((row) => [row.id, row]));
  for (const row of incoming) rows.set(row.id, row);
  return [...rows.values()].sort((a, b) => b.started_at.localeCompare(a.started_at) || b.id.localeCompare(a.id));
}
export async function officeGet(path, key) {
  if(isMobileAccess())return mobileRequest(mobileRoute(path));
  if (!path.startsWith('/api/office/')) throw new Error('Invalid office endpoint');
  const sdk=path.startsWith('/api/office/grok/sdk/');
  const codexStatus=path.split('?')[0]==='/api/office/codex/background/status';
  // Codex metadata has one 45s server budget plus bounded owned-process cleanup.
  // The normal 5s read budget must not cancel a healthy cold runtime first.
  const signal=AbortSignal.timeout(sdk||codexStatus?60000:5000);
  let response;
  try{response=await fetch(path,{cache:'no-store',credentials:'same-origin',headers:{'X-Office-Key':key},signal});}
  catch(cause){
    if(!codexStatus)throw cause;
    const timedOut=cause?.name==='TimeoutError'||(signal.aborted&&signal.reason?.name==='TimeoutError');
    const error=new Error(timedOut?'Codex status request timed out':'Codex status request unavailable');
    error.code=timedOut?'codex_status_timeout':'codex_status_unavailable';throw error;
  }
  if (!response.ok) {
    let detail;try{detail=await response.json();}catch{}
    const vendorDenied=sdk&&response.status===403&&detail?.error==='grok_sdk_access_denied';
    const error=new Error(vendorDenied?'Grok Bot 未允許這次連接。':response.status === 403 ? '請從私人 OFFICE 入口開啟。' : '暫時未能讀取工作記錄。');
    error.status=response.status;error.code=vendorDenied?'grok_sdk_access_denied':response.status===403?'private_office_required':typeof detail?.error==='string'&&/^[a-z_]{1,100}$/.test(detail.error)?detail.error:'office_read_unavailable';throw error;
  }
  return response.json();
}
export async function officePost(path, key, body) {
  if(isMobileAccess())return mobileRequest(mobileRoute(path,'POST'),{method:'POST',body});
  if (!['/api/office/commands','/api/office/commands/cancel','/api/office/permissions','/api/office/agents/logout','/api/office/seating','/api/office/cursor/local/create','/api/office/codex/background/create','/api/office/codex/background/model','/api/office/grok/sdk/login','/api/office/grok/sdk/login/cancel','/api/office/grok/sdk/connect','/api/office/mobile/pairings','/api/office/mobile/revoke'].includes(path)) throw new Error('Invalid command endpoint');
  const response=await fetch(path,{method:'POST',cache:'no-store',credentials:'same-origin',
    headers:{'X-Office-Key':key,'Content-Type':'application/json'},body:JSON.stringify(body),signal:AbortSignal.timeout(path.startsWith('/api/office/grok/sdk/')?60000:['/api/office/cursor/local/create','/api/office/codex/background/create'].includes(path)?30000:10000)});
  const data=await response.json();
  if(!response.ok){const error=new Error(data.error || 'commands_unavailable');error.status=response.status;error.code=data.error||'commands_unavailable';
    if(typeof data.logout_token==='string'&&/^[0-9a-f]{64}$/.test(data.logout_token))error.logoutToken=data.logout_token;
    throw error;}
  return data;
}
