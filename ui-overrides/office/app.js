// Glue: polls /api/state (CONTRACT §4), keeps the view (floor, selection), feeds the scene and the UI.
// With #<topic> in the link it reads the user's bots from their channel instead (feed.js, lib/feedbase.js): no server.
// Without either (static preview) it runs on web/data/demo-state.json with a local simulation.
import { initScene, renderScene, zoom, focusBot } from './scene/scene.js';
import { initSound, sfx } from './audio/sound.js';
import { selectionFloor,isGround,GROUND_ID } from './overview.js';
import { initSimpleMode } from './simple-mode.js';
import {officePost} from './office-api.js';
import {controlError,chooseDesk} from './office-controls.js';
import {canManageAgent} from './subagent-view.js';

const POLL_MS = 1000;
const view = { currentFloor: null, selectedBot: null, simpleMode: false };
let state = null;
let ui = { initUI() {}, renderUI() {} };
let demoTimer = null;

// Wallpaper mode (?mode=wallpaper, for Lively): office full screen + one status line, no interaction except hover.
//   &floor=<id>   pin the floor (default: the floor with the most bots)
//   &cycle=<sec>  go to the next floor every <sec> seconds through the normal elevator switch
const params = new URLSearchParams(location.search);
const WALLPAPER = params.get('mode') === 'wallpaper';
if (WALLPAPER) document.documentElement.classList.add('wallpaper');

try {
  ui = await import(WALLPAPER ? './wallpaper.js' : './ui/ui.js');
} catch (err) {
  console.warn('[app] UI module not available yet:', err.message);
}

initScene(document.getElementById('scene'), {
  onSelectBot: (id) => select(id),
  onHoverBot: (id) => sfx.hover(id),
  onMoveBot: async (id,deskId) => {
    const bot=state?.bots[id];if(!canManageAgent(bot))throw new Error('agent_not_selected');
    try{
      await officePost('/api/office/seating',decodeURIComponent(location.hash.slice(1)),{agent_id:bot.agent_id,desk_id:deskId});
      window.dispatchEvent(new CustomEvent('office:desk-moved',{detail:{agent_id:bot.agent_id,desk_id:deskId}}));
      window.dispatchEvent(new CustomEvent('office:refresh'));
    }catch(error){throw new Error(controlError(error));}
  },
  onChooseDesk: id=>chooseDesk(state?.bots[id]),
});

addEventListener('office:desk-moved',event=>{const bot=Object.values(state?.bots||{}).find(bot=>bot.agent_id===event.detail.agent_id);if(bot){bot.ground_desk_id=event.detail.desk_id;view.currentFloor=GROUND_ID;view.selectedBot=bot.id;render();requestAnimationFrame(()=>focusBot(bot.id));}});
addEventListener('office:agent-logged-out',event=>{if(state?.bots[view.selectedBot]?.agent_id===event.detail.agent_id){view.selectedBot=null;render();}});

ui.initUI({
  onSelectBot: (id) => select(id),
  onSelectFloor: (id) => setFloor(id),
  onZoom: (dir) => { zoom(dir); sfx.zoom(dir); },
  onOpenChat: (id, url) => { if (url) location.href = url; },
});

// music and sound effects (not on the wallpaper): the speaker button sits in the top bar's right group
if (!WALLPAPER) initSound({ slot: document.querySelector('#topbar .bhq-right') });
if (!WALLPAPER) initSimpleMode((enabled) => {
  view.simpleMode = enabled;
  view.currentFloor = GROUND_ID;
  zoom('reset');
  render();
});

addEventListener('keydown', (e) => { if (e.key === 'Escape') select(null); });

function select(id) {
  const before = view.selectedBot;
  view.selectedBot = id && state?.bots[id] ? id : null;
  if (view.selectedBot !== before) sfx.select(!!view.selectedBot);
  if (view.selectedBot) {
    const bot = state.bots[view.selectedBot];
    const target=selectionFloor(state,view.currentFloor,bot.id);
    if (target !== view.currentFloor) setFloor(target, true);
    render();
    requestAnimationFrame(() => focusBot(view.selectedBot));
  } else {
    render();
  }
}

function setFloor(id, keepSelection = false) {
  if (!state?.floors.some((f) => f.id === id)) return;
  if (id !== view.currentFloor) {                        // the elevator's chime rings higher on higher floors
    const order = [...state.floors].sort((a, b) => (a.order ?? 0) - (b.order ?? 0)).findIndex((f) => f.id === id);
    sfx.floor(order);
  }
  view.currentFloor = id;
  if (!keepSelection) view.selectedBot = null;
  if (!WALLPAPER) { try { localStorage.setItem('bothq.floor', id); } catch {} }
  zoom('reset');
  render();
}

function pickDefaultFloor() {
  const fullest = () => [...state.floors].sort((a, b) => b.bots.length - a.bots.length)[0]?.id ?? null;
  if (WALLPAPER) {
    const pinned = params.get('floor');
    return pinned && state.floors.some((f) => f.id === pinned) ? pinned : fullest();
  }
  if (state.floors.some(f => f.id === GROUND_ID)) return GROUND_ID;
  let saved = null;
  try { saved = localStorage.getItem('bothq.floor'); } catch {}
  if (saved && state.floors.some((f) => f.id === saved)) return saved;
  return fullest();
}

// &cycle=<sec>: rotate through the floors (by `order`) with the usual elevator ride; paused while the page is hidden.
const cycleSec = WALLPAPER ? Number(params.get('cycle')) : 0;
if (cycleSec >= 5) {
  setInterval(() => {
    if (document.hidden || !state || state.floors.length < 2) return;
    const floors = [...state.floors].sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
    const i = floors.findIndex((f) => f.id === view.currentFloor);
    setFloor(floors[(i + 1) % floors.length].id);
  }, cycleSec * 1000);
}

function render() {
  if (!state) return;
  if (view.simpleMode && state.floors.some(f => f.id === GROUND_ID)) view.currentFloor = GROUND_ID;
  if (!view.currentFloor || !state.floors.some((f) => f.id === view.currentFloor)) view.currentFloor = pickDefaultFloor();
  if (view.selectedBot && !state.bots[view.selectedBot]) view.selectedBot = null;
  if (view.selectedBot) view.currentFloor = selectionFloor(state,view.currentFloor,view.selectedBot);
  document.documentElement.classList.toggle('office-ground-view',isGround(view.currentFloor));
  renderScene(state, view);
  ui.renderUI(state, view);
  if (!WALLPAPER) soundStatusChanges();
}

// A bot that just started waiting for you, failed or finished gets a short sound (one per batch, the most urgent).
// The first state is the baseline, so opening the page or replaying the channel's history stays quiet.
let lastStatus = null;
function soundStatusChanges() {
  const now = {};
  for (const [id, b] of Object.entries(state.bots)) now[id] = b.effective_status;
  if (lastStatus) {
    const fresh = new Set();
    for (const [id, s] of Object.entries(now)) if (lastStatus[id] && lastStatus[id] !== s) fresh.add(s);
    const kind = ['waiting_user', 'error', 'done'].find((k) => fresh.has(k));
    if (kind) sfx.status(kind);
  }
  lastStatus = now;
}

// ---------- live polling ----------

async function poll() {
  try {
    const since = state && !state.demoLocal ? `?since=${state.version}` : '';
    const res = await fetch(`api/state${since}`, { cache: 'no-store', credentials: 'same-origin' });   // relative: works under a sub-path too
    if (res.status === 401) { location.href = 'login'; return; }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (demoTimer) { clearInterval(demoTimer); demoTimer = null; }
    if (!data.unchanged) { state = data; render(); }
  } catch {
    if (!state) await startLocalDemo();
    if (state?.demoLocal) return;          // static preview: no server, stop polling
  }
  setTimeout(poll, POLL_MS);
}

// ---------- the user's own bots: #<topic> ----------

const receiveState = (data) => {
  const empty = Object.keys(data.bots || {}).length === 0;
  if (empty) {
    const counts = { working: 0, idle: 0, waiting_user: 0, error: 0, stuck: 0, done: 0 };
    data.floors = [{ id: GROUND_ID, name: '全員工作總覽', kind: 'overview', order: 0, bots: [], counts, free_desks: 0 }];
    data.summary = { ...counts, total: 0 };
  }
  document.getElementById('enrollment-empty').hidden = !empty;
  document.body.classList.toggle('office-empty', empty);
  state = data;
  document.title=(data.brand || 'Virtual Office')+' · 3D 辦公室';
  render();
};
const { feedFromLink } = await import('./lib/feedbase.js');
if (feedFromLink(location)) {
  const { feedFromHash, connectFeed } = await import('./feed.js');
  connectFeed(feedFromHash(location.hash, location.search), receiveState);
  addEventListener('hashchange', () => location.reload());
} else {
  // An office without an invitation link is empty. Never inject demo agents.
  const { StateStore } = await import('./lib/state.js');
  receiveState(new StateStore({ tz: Intl.DateTimeFormat().resolvedOptions().timeZone }).snapshot());
}

// ---------- static preview: local simulation ----------

async function startLocalDemo() {
  const res = await fetch('./data/demo-state.json', { cache: 'no-store' });
  state = await res.json();
  state.demoLocal = true;
  render();
  demoTimer = setInterval(simulate, 1600);
}

const STEPS = {
  working: ['scanning X, 7 searches', 'reading 40 fresh posts', 'drafting a thread', 'checking the numbers', 'compiling the report', 'drawing the cover', 'cross-checking sources'],
  waiting_user: ['waiting for your answer: publish?', 'need a budget OK', 'which option should we pick?'],
  error: ['X rate limit 429, retry in 15 min', "couldn't open the report file"],
  done: ['done: report sent', 'draft ready', 'task closed'],
  idle: ['waiting for a new task'],
};
const TASKS = ['Thread about AI agents for Monday', 'Covers for the post series', 'Weekly reach report',
  'Competitor post breakdown', 'Summary across all floors', 'Plan for the week'];
const NEXT = {
  working: [['working', 5], ['done', 3], ['waiting_user', 1], ['error', 0.5]],
  done: [['idle', 3], ['working', 2]],
  idle: [['working', 3], ['idle', 2]],
  waiting_user: [['working', 2], ['waiting_user', 2]],
  error: [['working', 2], ['error', 1]],
  stuck: [['working', 2], ['stuck', 2]],
};

function simulate() {
  const bots = Object.values(state.bots);
  const bot = bots[Math.floor(Math.random() * bots.length)];
  const cur = bot.effective_status;
  const next = weighted(NEXT[cur] || NEXT.idle);
  const step = pickOne(STEPS[next] || STEPS.working);
  const now = new Date().toISOString();
  if (next !== cur) bot.since = now;
  bot.status = next;
  bot.effective_status = next;
  bot.step = step;
  bot.last_event = now;
  if (next === 'idle') bot.task = null;
  else if (!bot.task || /^tsk-/.test(bot.task)) bot.task = pickOne(TASKS);
  const today = (bot.today ||= { working_min: 0, events: 0, tasks_done: 0 });
  today.events++;
  if (next === 'done') today.tasks_done++;
  (today.hourly ||= new Array(24).fill(0))[new Date().getHours()]++;
  bot.history.unshift({ t: now, status: next, step });
  bot.history.length = Math.min(bot.history.length, 20);
  state.feed.unshift({ t: now, bot: bot.id, name: bot.name, status: next, step });
  state.feed.length = Math.min(state.feed.length, 100);
  recount();
  state.now = now;
  state.version++;
  render();
}

function recount() {
  const zero = () => ({ working: 0, idle: 0, waiting_user: 0, error: 0, stuck: 0, done: 0 });
  state.summary = { ...zero(), total: 0 };
  for (const f of state.floors) f.counts = zero();
  for (const b of Object.values(state.bots)) {
    state.summary[b.effective_status]++;
    state.summary.total++;
    const f = state.floors.find((x) => x.id === b.floor);
    if (f) f.counts[b.effective_status]++;
  }
}

function weighted(list) {
  let r = Math.random() * list.reduce((s, [, w]) => s + w, 0);
  for (const [v, w] of list) if ((r -= w) <= 0) return v;
  return list[0][0];
}

function pickOne(arr) { return arr[Math.floor(Math.random() * arr.length)]; }
