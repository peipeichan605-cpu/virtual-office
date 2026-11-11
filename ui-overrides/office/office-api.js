export function agentPageUrl(agent, topic, run = '') {
  const query = new URLSearchParams({ agent });
  if (run) query.set('run', run);
  return `/office/agent.html?${query}#${encodeURIComponent(topic)}`;
}
export function mergeRunRows(previous, incoming) {
  const rows = new Map(previous.map((row) => [row.id, row]));
  for (const row of incoming) rows.set(row.id, row);
  return [...rows.values()].sort((a, b) => b.started_at.localeCompare(a.started_at) || b.id.localeCompare(a.id));
}
export async function officeGet(path, key) {
  if (!path.startsWith('/api/office/')) throw new Error('Invalid office endpoint');
  const response = await fetch(path, { cache: 'no-store', credentials: 'same-origin', headers: { 'X-Office-Key': key }, signal: AbortSignal.timeout(5000) });
  if (!response.ok) throw new Error(response.status === 403 ? '請從私人 OFFICE 入口開啟。' : '暫時未能讀取工作記錄。');
  return response.json();
}
export async function officePost(path, key, body) {
  if (!['/api/office/commands','/api/office/commands/cancel','/api/office/permissions','/api/office/agents/logout','/api/office/seating'].includes(path)) throw new Error('Invalid command endpoint');
  const response=await fetch(path,{method:'POST',cache:'no-store',credentials:'same-origin',
    headers:{'X-Office-Key':key,'Content-Type':'application/json'},body:JSON.stringify(body),signal:AbortSignal.timeout(10000)});
  const data=await response.json();
  if(!response.ok){const error=new Error(data.error || 'commands_unavailable');error.status=response.status;throw error;}
  return data;
}
