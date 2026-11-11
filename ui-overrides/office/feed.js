// Bots-only mode: the office reads the user's bots straight from their channel. No server, no tunnel.
// The link is …/#<topic>. A plain link reads the same topic on two mailboxes and merges them: our relay (RELAY, relay/ in
// this repo, where bots set up with the current prompt post) and ntfy.sh (FEED_BASE in lib/feedbase.js, where bots set
// up with the first prompt post). #<topic>@<host>, ?feed=<host> or the older #<topic>&ntfy=<server> name one mailbox
// (a self-hosted ntfy) and read only that. The topic lives in the hash, so it never reaches any server but those. The
// same reducer the local server uses (lib/state.js) turns the lines into the office's state. The mailboxes keep 12 hours,
// so the newest lines are cached per topic in localStorage and replayed first: after a quiet night the office is not
// empty, every bot is still there. Cached history does not prove the service is live.
//
// Lines on the channel (one JSON object per message, at most 4 KB, anything else is dropped):
//   {"bot":"…","status":"working|idle|waiting_user|error|done","step":"…"}     a status change
//   {"roster":["Bot A","Bot B"]}                                                every bot appears, idle until it posts
//   {"looks":{"Bot A":{"shape":"sphere","color":"#RRGGBB","accessory":"none"}}} how each bot's blob looks
//   {"type":"usage","total_used_pct":37,"resets_at":"…","bots":{"Bot A":4.1}}  the weekly limit meter
//   {"groups":{"Bot A":"Content"}}                                            teams, one floor each
//   {"chat":{"Bot A":"grokbot://app/v1/agent?id=…"}}                         each bot's chat, for «Open chat»
//   {"routines":{"bot":"Bot A","items":[{"name":"…","schedule":"…","next_run":"ISO"}]}}   a bot's routines
//   {"check":{"bots":["Bot A","Bot B"]}}                                     the setup bot starts a status check
// On top of the server's state the office gets state.finished (done lines of the last 24 h, newest first),
// state.routines (every bot's routines, soonest first) and state.check (the latest status check: who has answered), for
// the «Finished», «Routines» and «Status check» panels.
import { StateStore, botIdFor } from './lib/state.js';
import { FEED_BASE, feedFromLink } from './lib/feedbase.js';
import { agentPageUrl, officeGet } from './office-api.js';
import { withGroundFloor } from './overview.js';
import {GROK_DEFAULT_LOOK,isLegacyGrokPlaceholder} from './grok-avatar.js';
import {isObservedChild,withObservedSubagents,childVisualId} from './subagent-view.js';

const RELAY = 'https://relay.0xcodila.com', NTFY = 'https://ntfy.sh';
const MAX_LINE = 4096, KEEP = 600, TICK_MS = 30e3, DAY_MS = 24 * 3600e3, PING_MS = 60e3;
const QUIET_MS = Infinity, CHECK_MS = 6 * 3600e3, HEALTH_MS = 5000;
// Everyone's bots, ours included, are built from their look (the app's 14 avatars and 11 colours, read from a
// screenshot), so a bot in the office is the bot in the app. The hand-made GLBs in assets/blobs/ stay for the local server.
const HANDMADE = new Set(['grok-bot']);

/** The page's hash and query -> { topic, servers: [mailboxes, no trailing slash] }, or null without a topic.
 *  A plain link reads both mailboxes; a link that names one reads that one. */
export function feedFromHash(hash, search = '') {
  const f = feedFromLink({ hash: hash || '', search });
  if (!f) return null;
  return { topic: f.topic, servers: [location.origin] };
}

/** Rebuild from cache + each mailbox's last 12 h, then follow them live. Calls onState(state) on every change. */
export async function connectFeed({ topic, servers }, onState) {
  const sources = servers.map((server) => ({ server, tag: server.startsWith(RELAY) ? 'relay' : 'ntfy', base: `${server}/${encodeURIComponent(topic)}`, lastId: null, connected: false }));
  const usual = servers.length > 1;                      // the plain link: both mailboxes, one cache
  const cacheKey = usual ? `bothq.feed.${topic}` : `bothq.feed.${servers[0]}.${topic}`;
  const oldKey = `bothq.feed.${NTFY}.${topic}`;         // the cache of the first single-mailbox office (ntfy.sh only)
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Europe/Sofia';
  const store = new StateStore({ tz, quietIdleMs: QUIET_MS, statusIdleMs: Infinity, hasAvatar: (id) => HANDMADE.has(id) });   // Local observer determines stale/unknown states.
  const cfg = {};                                        // per bot name: { look, chat_url } -> the store's bots config
  const routines = new Map();                            // bot id -> { name, items }
  let check = null;                                      // the latest status check: { ms, names }
  let lines = [], saveTimer = 0;
  let health = null, connectionKey = '', healthPending = false;
  let privateWork = null, workKey = '', workPending = false, workAvailable = false;
  const localStates = new Map();
  const streams = [];
  const seen = new Set();
  const refreshOffice=()=>loadWork();
  globalThis.addEventListener?.('office:refresh',refreshOffice);

  await loadWork(false);
  lines = cached(cacheKey).map((l) => ({ ...l, src: l.src || sources[0].tag }));
  if (usual) lines.push(...cached(oldKey).map((l) => ({ ...l, src: l.src || 'ntfy' })));
  await Promise.all((privateWork?.feed_enabled === false ? [] : sources).map(async (s) => {
    try {
      const response = await fetch(`${s.base}/json?poll=1&since=12h`, { cache: 'no-store', signal: AbortSignal.timeout(5000) });
      if (!response.ok) throw new Error('History unavailable');
      const text = await response.text();
      for (const row of text.split('\n')) if (row) keep(s, JSON.parse(row));
      s.connected = true;
    } catch { /* offline or down: the other mailbox and the cache still show the office */ }
  }));
  lines = lines.filter(fresh).sort((a, b) => a.time - b.time);   // stable: lines of one second keep the mailbox's order
  for (const l of lines) ingest(l, true);
  for (const s of sources) s.lastId = lines.findLast((l) => l.src === s.tag)?.id ?? null;
  save();
  if (usual) try { localStorage.removeItem(oldKey); } catch { /* private mode */ }
  publish();
  // Private local deployment: no third-party analytics or relay.
  if (privateWork?.feed_enabled !== false) for (const s of sources) follow(s);
  const tickTimer = setInterval(() => { const v = store.version, s = store.snapshot(); if (store.version !== v) onState(extras(s)); }, TICK_MS);
  const healthTimer = setInterval(checkHealth, HEALTH_MS);
  const workTimer = setInterval(loadWork, 2000);
  await checkHealth();
  return { close() { globalThis.removeEventListener?.('office:refresh',refreshOffice); clearInterval(tickTimer); clearInterval(healthTimer); clearInterval(workTimer); clearTimeout(saveTimer); streams.forEach((s) => s.close()); } };

  async function loadWork(emit = true) {
    if (workPending) return;
    workPending = true;
    try {
      const data = await officeGet('/api/office/work', topic);
      const key = JSON.stringify(data);
      const recovered = !workAvailable;
      workAvailable = true;
      if (key === workKey) { if (emit && recovered) publish(); return; }
      workKey = key; privateWork = data;
      const members = (data.agents || []).filter(agent => !isObservedChild(agent));
      const selected = new Set(members.map((agent) => botIdFor(agent.name)));
      for (const [id, bot] of store.bots) if (!selected.has(id)) {
        if (bot.assign) store.members.get(bot.assign.floor)?.delete(bot.assign.desk);
        store.bots.delete(id); localStates.delete(bot.name);
      }
      store.feed = store.feed.filter((row) => selected.has(row.id));
      const floors = new Map();
      for (const agent of members) {
        const workspace = agent.workspace || {id:'workspace-unassigned',name:'未指定 Workspace'};
        floors.set(workspace.id, {id:workspace.id,name:workspace.name});
      }
      store.setFloorsConfig({floors:[...floors.values()]});
      for (const agent of members) {
        const config = (cfg[agent.name] ||= {});
        config.chat_url = agentPageUrl(agent.id, topic);
        config.floor = agent.workspace?.id || 'workspace-unassigned';
        if (agent.provider) {
          const codex = agent.provider === 'codex';
          if(agent.provider==='grok_bot'&&isLegacyGrokPlaceholder(config.look)&&config.look_origin!=='reported')config.look=null;
          config.look ||= agent.provider==='grok_bot'?{...GROK_DEFAULT_LOOK}:{ shape: codex ? 'rounded_cube' : 'sphere', color: codex ? '#4087E8' : '#6CAC72', accessory: codex ? 'antenna' : 'glasses' };
          config.look_origin ||= 'platform-default';
        }
        if (!['working', 'done', 'idle', 'waiting_user', 'error'].includes(agent.state)) continue;
        const stateKey = JSON.stringify([agent.state, agent.step, agent.state_changed_at]);
        if (localStates.get(agent.name) === stateKey) continue;
        localStates.set(agent.name, stateKey);
        store.apply({ bot: agent.name, status: agent.state, step: agent.step,
          t: agent.state_changed_at || new Date().toISOString() });
      }
      store.setBotsConfig(cfg);
      if (emit) publish();
    } catch { workAvailable = false; if (emit) publish(); }
    finally { workPending = false; }
  }

  function connection() {
    const local = privateWork?.local_ok !== undefined;
    const rootLive = local && health?.ok && health.local_ok && privateWork.local_ok && workAvailable;
    const childrenPending = rootLive && privateWork.subagents_ok === false;
    const status = health === null ? 'connecting' : !health.ok ? 'offline'
      : local ? (rootLive && !childrenPending ? 'live' : 'syncing')
      : !health.sync_ok || !sources.some((s) => s.connected) ? 'syncing' : 'live';
    return { status, label: childrenPending ? '主 Agent 已同步 · 子 Agent 狀態待確認' : { connecting: '連線中', offline: '連線中斷・狀態未更新', syncing: '同步中・狀態待確認', live: local ? '本機即時同步' : '即時同步' }[status] };
  }
  function connectionChanged() {
    const key = JSON.stringify(connection());
    if (key !== connectionKey) { connectionKey = key; publish(); }
  }
  async function checkHealth() {
    if (healthPending) return;
    healthPending = true;
    try {
      const response = await fetch('/health', { cache: 'no-store', signal: AbortSignal.timeout(3500) });
      if (!response.ok) throw new Error('Service unavailable');
      health = await response.json();
    } catch { health = { ok: false, sync_ok: false }; }
    finally { healthPending = false; connectionChanged(); }
  }

  function cached(key) {
    try { return JSON.parse(localStorage.getItem(key)) || []; } catch { return []; }
  }
  function fresh(l) {                                    // once per mailbox id, and once per (time, text) across mailboxes
    const a = `${l.src} ${l.id}`, b = `${l.time} ${l.message}`;
    if (seen.has(a) || seen.has(b)) return false;
    seen.add(a).add(b);
    return true;
  }
  function keep(s, m) {                                  // a message of a history request, into `lines`
    if (m?.event === 'message' && typeof m.id === 'string') lines.push({ id: m.id, time: Number(m.time) || 0, message: m.message, src: s.tag });
  }
  function take(s, m) {                                  // a live message: new -> ingest; true if the office changed
    if (m?.event !== 'message' || typeof m.id !== 'string') return false;
    s.lastId = m.id;
    const l = { id: m.id, time: Number(m.time) || Date.now() / 1000, message: m.message, src: s.tag };
    if (!fresh(l)) return false;
    lines.push(l);
    if (lines.length > KEEP * 1.5) lines = lines.slice(-KEEP);
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, 2000);
    return ingest(l, false);
  }
  function follow(s) {                                   // ntfy: an event stream; the relay: a WebSocket its server sleeps through
    const on = (data) => { try { if (take(s, JSON.parse(data))) publish(); } catch { /* keepalive or junk */ } };
    if (s.tag !== 'relay') {
      const live = new EventSource(`${s.base}/sse`);
      streams.push(live);
      live.onopen = () => catchUp(s);                    // also after a reconnect: fetch what was missed
      live.onmessage = (e) => on(e.data);
      live.onerror = () => { s.connected = false; connectionChanged(); };
      return;
    }
    let wait = 1e3, heard = Date.now();
    (function open() {
      const ws = new WebSocket(`${s.base.replace(/^http/, 'ws')}/ws`);
      const ping = setInterval(() => {                   // "pong" comes back without waking the relay
        if (Date.now() - heard > 2.5 * PING_MS) ws.close();   // silent: a dead link after sleep, start over
        else if (ws.readyState === 1) ws.send('ping');
      }, PING_MS);
      ws.onopen = () => { wait = 1e3; heard = Date.now(); catchUp(s); };
      ws.onmessage = (e) => { heard = Date.now(); if (e.data !== 'pong') on(e.data); };
      ws.onclose = () => { clearInterval(ping); setTimeout(open, wait); wait = Math.min(wait * 2, 60e3); };
    })();
  }
  async function catchUp(s) {
    try {
      const response = await fetch(`${s.base}/json?poll=1&since=${s.lastId ?? '12h'}`, { cache: 'no-store', signal: AbortSignal.timeout(5000) });
      if (!response.ok) throw new Error('History unavailable');
      const text = await response.text();
      let changed = false;
      for (const row of text.split('\n')) if (row && take(s, JSON.parse(row))) changed = true;
      s.connected = true;
      if (changed) publish();
    } catch { s.connected = false; }
    connectionChanged();
  }
  function ingest({ message, time }, replay) {
    if (typeof message !== 'string' || message.length > MAX_LINE) return false;
    let rec;
    try { rec = JSON.parse(message); } catch { return false; }
    if (!rec || typeof rec !== 'object' || Array.isArray(rec)) return false;
    const allowed = (name) => !privateWork || privateWork.agents.some((agent) => !isObservedChild(agent) && agent.name === name);
    const t = typeof rec.t === 'string' ? rec.t : new Date(time * 1000).toISOString();
    if (Array.isArray(rec.roster)) {
      for (const name of rec.roster.slice(0, 400)) {
        if (typeof name === 'string' && name.trim() && allowed(name) && !store.bots.has(botIdFor(name))) store.apply({ t, bot: name, status: 'idle', step: '', listed: true }, { replay });
      }
      return true;
    }
    if (isMap(rec.looks)) {
      for (const [name, look] of Object.entries(rec.looks).slice(0, 400)) if (allowed(name)) {
        const provider=privateWork?.agents.find(agent=>agent.name===name)?.provider;
        if(replay&&provider==='grok_bot'&&isLegacyGrokPlaceholder(look))continue;
        const config=(cfg[name] ||= {});config.look=look;config.look_origin=replay?'cached':'reported';
      }
      store.setBotsConfig(cfg);
      return true;
    }
    if (!privateWork && isMap(rec.groups)) {             // Local workspace assignments take precedence over mailbox groups.
      for (const [name, team] of Object.entries(rec.groups).slice(0, 400)) {
        if (allowed(name) && typeof team === 'string' && team.trim()) (cfg[name] ||= {}).floor = team.trim().slice(0, 40);
      }
      store.setBotsConfig(cfg);
      return true;
    }
    if (isMap(rec.chat)) {                               // only app deep links and web pages (the store refuses scripts)
      for (const [name, url] of Object.entries(rec.chat).slice(0, 400)) {
        if (!privateWork && typeof url === 'string' && /^(grokbot|https):\/\//i.test(url.trim())) (cfg[name] ||= {}).chat_url = url.trim();
      }
      store.setBotsConfig(cfg);
      return true;
    }
    if (isMap(rec.routines)) {
      const name = typeof rec.routines.bot === 'string' ? rec.routines.bot.trim().slice(0, 80) : '';
      if (!name || !Array.isArray(rec.routines.items)) return false;
      const items = rec.routines.items.slice(0, 50).filter(isMap).map((r) => ({
        routine: String(r.name ?? '').slice(0, 80), schedule: String(r.schedule ?? '').slice(0, 80),
        next_run: Number.isFinite(Date.parse(r.next_run)) ? new Date(r.next_run).toISOString() : null,
      })).filter((r) => r.routine);
      routines.set(botIdFor(name), { name, items });      // a bot's latest list replaces its previous one
      return true;
    }
    if (isMap(rec.check)) {                              // the setup bot asks every bot for a status line: who answers?
      const names = (Array.isArray(rec.check.bots) ? rec.check.bots : []).filter((n) => typeof n === 'string' && n.trim())
        .slice(0, 400).map((n) => n.trim().slice(0, 80));
      if (!names.length) return false;
      const ms = Number.isFinite(Date.parse(t)) ? Date.parse(t) : time * 1000;
      if (!check || ms >= check.ms) check = { ms, names };
      return true;
    }
    // The local observer is authoritative; delayed mailbox events cannot undo it.
    if (!allowed(rec.bot) || localStates.has(rec.bot)) return false;
    store.apply({ ...rec, t }, { replay });
    return true;
  }
  function extras(s) {                                   // the panels' data on top of the server-shaped state
    s.connection = connection();
    s.brand = privateWork?.brand || 'VIRTUAL OFFICE';
    for (const agent of (privateWork?.agents || []).filter(agent=>!isObservedChild(agent))) {
      const bot = s.bots[botIdFor(agent.name)];
      if (bot) { bot.agent_id=agent.id; bot.provider=agent.provider; bot.look_origin=cfg[agent.name]?.look_origin||null; bot.workspace=agent.workspace || null; bot.latest_message=agent.latest_message || null; bot.command_capability=agent.command_capability || null; bot.ground_desk_id=agent.ground_desk_id || null; }
    }
    s.history_ok = privateWork?.history_ok !== false;
    s.subagents_ok = privateWork?.subagents_ok !== false;
    const since = Date.now() - DAY_MS, finished = [];
    for (let i = lines.length - 1; i >= 0 && finished.length < 100; i--) {
      const l = lines[i];
      if (l.time * 1000 < since) break;
      if (typeof l.message !== 'string' || l.message.length > MAX_LINE || !l.message.includes('"done"')) continue;
      let rec;
      try { rec = JSON.parse(l.message); } catch { continue; }
      if (rec?.status !== 'done' || typeof rec.bot !== 'string') continue;
      finished.push({ t: typeof rec.t === 'string' ? rec.t : new Date(l.time * 1000).toISOString(), bot: botIdFor(rec.bot), name: rec.bot.slice(0, 80), step: String(rec.step ?? '').slice(0, 80) });
    }
    s.finished = privateWork ? (privateWork.finished || []).map((r) => ({
      t: r.finished_at || r.started_at, bot: r.is_subagent ? childVisualId(r.agent_id) : botIdFor(r.name), name: (r.is_subagent ? '↳ ' : '') + r.name,
      step: r.delivery_preview || (r.status === 'done' ? '完成；此來源未提供文字交付。' : '工作未完成；查看記錄。'),
      persisted: true, run_id: r.id, status: r.status, url: agentPageUrl(r.agent_id, topic, r.id),
    })) : finished;
    s.routines = [...routines].flatMap(([bot, { name, items }]) => items.map((r) => ({ bot, name, ...r })))
      .sort((a, b) => (a.next_run ? Date.parse(a.next_run) : Infinity) - (b.next_run ? Date.parse(b.next_run) : Infinity));
    s.check = check && Date.now() - check.ms < CHECK_MS ? {
      t: new Date(check.ms).toISOString(),
      bots: check.names.map((name) => {                  // answered = a status line of its own at or after the check
        const id = botIdFor(name), seen = Date.parse(s.bots[id]?.last_seen ?? '');
        return { id, name, at: seen >= check.ms ? new Date(seen).toISOString() : null };
      }),
    } : null;
    return privateWork ? withGroundFloor(withObservedSubagents(s,privateWork.agents || [],id=>agentPageUrl(id,topic))) : s;
  }
  function publish() { store.commit(); onState(extras(store.snapshot())); }
  function save() { try { localStorage.setItem(cacheKey, JSON.stringify(lines.slice(-KEEP))); } catch { /* private mode: not cached */ } }
}

/** Two anonymous pings for the private usage count: one when the office opens (a hash of the topic, never the topic; how
 *  many bots and teams; which mailbox; phone or not; which parts of the prompt are set up), one when the visit ends. */
async function beacon(topic, { bots, floors, tags, has }) {
  try {
    const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(topic)));
    const h = [...digest.slice(0, 8)].map((b) => b.toString(16).padStart(2, '0')).join('');
    const src = tags.has('relay') ? (tags.has('ntfy') ? 'both' : 'relay') : tags.has('ntfy') ? 'ntfy' : 'none';
    const phone = navigator.userAgentData?.mobile ?? /Mobi|Android|iPhone|iPad/i.test(navigator.userAgent || '');
    const send = (data) => {                             // a string goes as text/plain: no CORS preflight
      const body = JSON.stringify(data);
      if (!navigator.sendBeacon?.(`${RELAY}/_beacon`, body)) fetch(`${RELAY}/_beacon`, { method: 'POST', body, keepalive: true }).catch(() => {});
    };
    send({ h, bots, floors, src, phone, has });
    let ended = false;
    const end = () => { if (!ended) { ended = true; send({ h, secs: Math.min(Math.round(performance.now() / 1000), 7 * 86400) }); } };
    globalThis.addEventListener?.('pagehide', end);
    globalThis.document?.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') end(); });
  } catch { /* stats are optional */ }
}

function isMap(v) { return !!v && typeof v === 'object' && !Array.isArray(v); }
