import {deskLabel} from '../seating.js';
import {canManageAgent} from '../subagent-view.js';
/*
 * Bot HQ UI: live cards on top of the full-bleed 3D scene (CONTRACT v2.3; look = the wallpaper's cards, web/face/face.css).
 *   top-left   brand + live clock         top-center  status counters        top-right  DEMO + usage meter
 *   left       floors "elevator"          right       floor team / bot card
 *   left, under the floors   dock: Status check (while one runs) / Waiting on me / Finished / Routines
 *   bottom-left activity feed (last 4)    bottom-right (left of the card)  zoom - / +
 *
 *   initUI({ onSelectBot, onSelectFloor, onZoom, onOpenChat })
 *   renderUI(state, { currentFloor, selectedBot })   // idempotent, cheap, safe to call every second
 *
 * State fields read: bots, floors, feed, usage, demo, now, summary, plus (bots-only mode, may be missing):
 *   bots[id].chat_url, state.finished [{ t, bot, name, step }], state.routines [{ bot, name, routine, schedule, next_run }],
 *   bots[id].last_seen (its own last status line), bots[id].unreported { from, to } (usage rose, no status line),
 *   state.check { t, bots: [{ id, name, at }] } (a status check: who has answered).
 *
 * Every DOM node is created once and then patched in place (text / attributes / classes only when the
 * value changed), so hover, focus and scroll positions survive the 1 Hz re-render.
 * All data goes into the DOM through textContent / setAttribute, never innerHTML.
 * The UI text is English. The cards are graphite at every hour; no day/night switch here (see ui.css).
 *
 * Phones (<= 600 px wide, and landscape phones): <html> gets .bhq-phone (+ .bhq-land). The floors become a chip row,
 * and the floor team, "Waiting on me", Finished, Routines and the activity feed move into one bottom sheet (a grabber, a
 * peek with the floor name and counts, a spring snap between peek and ~60% of the height); the bot card becomes a sheet
 * of its own (swipe down or x to close). Landscape: the sheet is a side column. Desktop is untouched: the same nodes move
 * back when the window grows. ?sheet=open starts with the sheet expanded (for screenshots).
 */

// Resolved against this file, so it works from /web/index.html, from a server rooted at web/, and from the harness.
const THUMB_BASE = new URL('../../assets/blobs/thumbs/', import.meta.url).href;
// the product logo, exactly as is (a 400x400 square), shown small in the brand pill
const MARK_SRC = new URL('../../assets/brand/logo-mark.png', import.meta.url).href;
const FEED_ROWS = 4;
const STEPS_SHOWN = 3;
const FEED_OUT_MS = 420;
const FINISHED_MAX = 60;
const DAY_MS = 24 * 3600 * 1000;
const LS_BLUR = 'bothq.waitBlur';
const LS_DOCK = 'bothq.dock';

const LABEL = {
  working: '工作中',
  idle: '休息中',
  waiting_user: '等待你',
  error: '錯誤',
  stuck: '停滯',
  done: '完成',
};
const CHIP = {
  working: '工作中',
  idle: '休息中',
  waiting_user: '等待你',
  error: '錯誤',
  stuck: '停滯',
  done: '完成',
};

// Short counter words [one, many] per status.
const WORDS = {
  working: ['工作中', '工作中'],
  waiting_user: ['等待', '等待'],
  error: ['錯誤', '錯誤'],
  stuck: ['停滯', '停滯'],
  idle: ['休息中', '休息中'],
  done: ['完成', '完成'],
};

// Counters pill. "attention" folds stuck into waiting_user: both need a human.
const SUMMARY = [
  { key: 'working', s: 'working', lead: true, n: (c) => c.working },
  { key: 'attention', s: 'waiting_user', alert: true, n: (c) => c.waiting_user + c.stuck },
  { key: 'error', s: 'error', alert: true, n: (c) => c.error },
  { key: 'idle', s: 'idle', n: (c) => c.idle },
  { key: 'done', s: 'done', n: (c) => c.done, hideZero: true },
];

const USAGE_TITLE = { week: 'Week', day: 'Day', month: 'Month' };
const USAGE_SPENT = { week: 'Usage this week', day: 'Usage today', month: 'Usage this month' };

const ICON = {
  close: '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
  plus: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M8 3v10M3 8h10" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
  minus: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M3 8h10" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
  chev: '<svg class="bhq-chev" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  eyeOn: '<svg class="bhq-eye-on" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M1.6 8s2.4-4.4 6.4-4.4S14.4 8 14.4 8 12 12.4 8 12.4 1.6 8 1.6 8z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><circle cx="8" cy="8" r="1.9" fill="currentColor"/></svg>',
  eyeOff: '<svg class="bhq-eye-off" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M1.6 8s2.4-4.4 6.4-4.4S14.4 8 14.4 8 12 12.4 8 12.4 1.6 8 1.6 8z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" opacity=".55"/><path d="M3 13L13 3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
  check: '<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"><path d="M3.5 8.6l3 3 6-6.6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  share: '<svg viewBox="0 0 20 20" width="20" height="20" aria-hidden="true"><path d="M10 12.2V2.9M6.7 6.1L10 2.8l3.3 3.3" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/><path d="M7.2 8.7H6A1.8 1.8 0 0 0 4.2 10.5v5A1.8 1.8 0 0 0 6 17.3h8a1.8 1.8 0 0 0 1.8-1.8v-5A1.8 1.8 0 0 0 14 8.7h-1.2" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
};

const reducedMotion = typeof matchMedia === 'function' ? matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };

/* ------------------------------------------------------------------ module state */

let U = null; // set by initUI

function freshState(handlers) {
  return {
    h: handlers || {},
    top: null, fl: null, panel: null, feed: null, zoom: null, dock: null, owned: [],
    last: { state: null, opts: { currentFloor: null, selectedBot: null } },
    skew: 0, nowStr: null,
    floors: new Map(), activeFloorId: null,
    cells: new Map(),
    feedRows: new Map(), feedOut: new Set(), feedMode: null, feedFirst: true,
    failedThumbs: new Set(),
    mode: null, cardBot: null, teamFloor: null, teamReady: false, ready: false,
    keyHandler: null, ro: null, ro2: null, onResize: null, timer: null, fades: [],
    sh: null, phone: false, land: false, mqs: null, mqFn: null, ro3: null, cardY: 0, cardStop: null, chipFloor: null,
  };
}

/* ------------------------------------------------------------------ tiny helpers */

function h(tag, props, ...kids) {
  const n = document.createElement(tag);
  if (props) {
    for (const k of Object.keys(props)) {
      const v = props[k];
      if (v == null || v === false) continue;
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = v;
      else n.setAttribute(k, v === true ? '' : String(v));
    }
  }
  for (const c of kids) if (c != null && c !== false) n.append(c);
  return n;
}

function svg(markup) {
  const t = document.createElement('template');
  t.innerHTML = markup.trim();
  return t.content.firstChild;
}

function setText(n, v) {
  v = v == null ? '' : String(v);
  if (n.textContent !== v) n.textContent = v;
}
function setAttr(n, k, v) {
  if (v == null || v === false) {
    if (n.hasAttribute(k)) n.removeAttribute(k);
  } else {
    v = v === true ? '' : String(v);
    if (n.getAttribute(k) !== v) n.setAttribute(k, v);
  }
}
function setHidden(n, hidden) {
  if (n.hidden !== !!hidden) n.hidden = !!hidden;
}
function setVar(n, name, v) {
  const cache = n._vars || (n._vars = {});
  if (cache[name] === v) return;
  cache[name] = v;
  n.style.setProperty(name, v);
}
function setCls(n, cls, on) {
  if (n.classList.contains(cls) !== !!on) n.classList.toggle(cls, !!on);
}
function setPx(n, prop, v) {
  const k = '_px_' + prop;
  if (n[k] === v) return;
  n[k] = v;
  n.style[prop] = v + 'px';
}
function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }
function pad2(n) { return n < 10 ? '0' + n : String(n); }

// A number that rolls to its new value instead of jumping (the wallpaper's counters). animate=false sets it at once.
function setNum(n, v, animate) {
  v = Math.max(0, Math.round(Number(v) || 0));
  if (n._to === v) return;
  const from = n._to == null ? 0 : n._to;
  n._to = v;
  if (n._raf) { cancelAnimationFrame(n._raf); n._raf = 0; }
  if (n._tm) { clearTimeout(n._tm); n._tm = 0; }
  if (animate === false || reducedMotion.matches || from === v) { n.textContent = String(v); return; }
  const t0 = performance.now(), dur = 700;
  const tick = (now) => {
    const k = clamp((now - t0) / dur, 0, 1), e = 1 - Math.pow(1 - k, 3);
    n.textContent = String(Math.round(from + (v - from) * e));
    n._raf = k < 1 ? requestAnimationFrame(tick) : 0;
  };
  n._raf = requestAnimationFrame(tick);
  // a page that is not painting (a hidden tab) never runs the tick: land on the number anyway
  n._tm = setTimeout(() => { n._tm = 0; if (n._to === v) n.textContent = String(v); }, dur + 150);
}

function plural(n, one, many) {
  return Math.abs(Math.trunc(n)) === 1 ? one : many;
}
function statusWord(s, n) {
  const f = WORDS[s] || [s, s];
  return plural(n, f[0], f[1]);
}

function parseTs(s) {
  const t = typeof s === 'number' ? s : Date.parse(s);
  return Number.isFinite(t) ? t : null;
}
const TIME_GB = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false });
function hhmm(ms) {
  if (ms == null) return '';
  return TIME_GB.format(new Date(ms));
}
const WEEKDAY = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

// Server "now" estimated from the client clock: skew is refreshed whenever state.now changes.
function syncClock(state) {
  if (state && state.now && state.now !== U.nowStr) {
    const t = parseTs(state.now);
    if (t != null) { U.skew = Date.now() - t; U.nowStr = state.now; }
  }
}
function nowMs() { return Date.now() - U.skew; }

function fmtDur(ms) {
  const min = Math.floor(Math.max(0, ms) / 60000);
  if (min < 1) return 'under a minute';
  if (min < 60) return min + ' min';
  const hr = Math.floor(min / 60), m = min % 60;
  if (hr < 24) return m ? hr + ' h ' + m + ' min' : hr + ' h';
  const d = Math.floor(hr / 24), hh = hr % 24;
  return hh ? d + ' d ' + hh + ' h' : d + ' d';
}
function fmtLeft(ms) {
  const min = Math.floor(ms / 60000);
  if (min < 1) return 'under a minute';
  const d = Math.floor(min / 1440), hr = Math.floor((min % 1440) / 60), m = min % 60;
  if (d > 0) return d + ' d ' + hr + ' h';
  if (hr > 0) return hr + ' h ' + m + ' min';
  return m + ' min';
}
// "12 min ago"
function relAgo(ms) {
  const diff = nowMs() - ms;
  const min = Math.floor(diff / 60000);
  if (min < 1) return 'just now';
  if (min < 60) return min + ' min ago';
  const hr = Math.floor(min / 60);
  if (hr < 24) return hr + ' h ago';
  return Math.floor(hr / 24) + ' d ago';
}
// "now", "12m", "3h", "2d": how long a bot has been silent, small enough for every row
function relShort(ms) {
  const min = Math.floor((nowMs() - ms) / 60000);
  if (min < 1) return 'now';
  if (min < 60) return min + 'm';
  const hr = Math.floor(min / 60);
  return hr < 24 ? hr + 'h' : Math.floor(hr / 24) + 'd';
}
// "Worked (unreported) · 09:00–13:00": usage rose between two usage lines and the bot posted no status in between
function unreportedText(bot) {
  const u = bot && bot.unreported;
  if (!u) return '';
  const a = parseTs(u.from), b = parseTs(u.to);
  return 'Worked (unreported)' + (b == null ? '' : ' · ' + (a == null ? '' : hhmm(a) + '–') + hhmm(b));
}
// "seen 12 min ago" / "never reported", with the exact time in a title
function seenText(bot) {
  const t = parseTs(bot && bot.last_seen);
  return t == null ? 'never reported' : 'seen ' + relAgo(t);
}
// "in 12 min", "in 3 h", "Tue 09:00" (local) when it is more than a day away
function relIn(ms) {
  const diff = ms - nowMs();
  if (diff <= 0) return 'due now';
  const min = Math.ceil(diff / 60000);
  if (min < 60) return 'in ' + min + ' min';
  const hr = Math.floor(min / 60), m = min % 60;
  if (hr < 6) return m ? 'in ' + hr + ' h ' + m + ' min' : 'in ' + hr + ' h';
  if (hr < 24) return 'in ' + hr + ' h';
  const d = new Date(ms);
  return WEEKDAY[d.getDay()] + ' ' + hhmm(ms);
}
function fullLocal(ms) {
  const d = new Date(ms);
  return WEEKDAY[d.getDay()] + ' ' + d.getDate() + '/' + pad2(d.getMonth() + 1) + ' ' + hhmm(ms);
}
const NUM1 = new Intl.NumberFormat('en-GB', { maximumFractionDigits: 1 });
function fmtHours(min) {
  const hr = Math.max(0, Number(min) || 0) / 60;
  return hr < 10 ? NUM1.format(hr) : String(Math.round(hr));
}

function effStatus(bot) { return (bot && (bot.effective_status || bot.status)) || 'idle'; }
function statusLabel(s) { return LABEL[s] || s || ''; }
function isHex(c) { return /^#[0-9a-f]{3}([0-9a-f]{3})?$/i.test(c || ''); }

function initials(name) {
  const words = String(name || '').match(/[\p{L}\p{N}]+/gu) || [];
  if (!words.length) return '?';
  if (words.length === 1) return Array.from(words[0]).slice(0, 2).join('').toUpperCase();
  return (Array.from(words[0])[0] + Array.from(words[1])[0]).toUpperCase();
}

// Pick white or ink text for a background colour (WCAG contrast).
function lum(hex) {
  let c = hex.replace('#', '');
  if (c.length === 3) c = c.split('').map((x) => x + x).join('');
  const f = (i) => {
    const v = parseInt(c.slice(i, i + 2), 16) / 255;
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * f(0) + 0.7152 * f(2) + 0.0722 * f(4);
}
function textOn(hex) {
  const L = lum(hex);
  const white = 1.05 / (L + 0.05);
  const ink = (L + 0.05) / (lum('#1D1D1F') + 0.05);
  return white >= ink ? '#fff' : '#1D1D1F';
}

// Insert / move children so that parent.children[start..] equals `els`; untouched nodes are not moved.
function reconcile(parent, els, start) {
  for (let i = 0; i < els.length; i++) {
    const cur = parent.children[start + i];
    if (cur !== els[i]) parent.insertBefore(els[i], cur || null);
  }
}

function restartAnim(n, cls) {
  n.classList.remove(cls);
  void n.offsetWidth; // reflow so the animation restarts
  n.classList.add(cls);
}

function lsGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode: not remembered */ } }

// Scroll edge effect: a scroller fades out where more content continues, instead of cutting a row in half.
function watchFade(el) {
  el.classList.add('bhq-fade');
  el.addEventListener('scroll', () => updateFade(el), { passive: true });
  U.fades.push(el);
  return el;
}
function updateFade(el) {
  const max = el.scrollHeight - el.clientHeight;
  const t = max > 2 && el.scrollTop > 2, b = max > 2 && el.scrollTop < max - 2;
  setVar(el, '--fade-t', t ? '20px' : '0px');
  setVar(el, '--fade-b', b ? '22px' : '0px');
  setAttr(el, 'data-fade', t || b ? '' : null);
}
function updateFades() { for (const el of U.fades) updateFade(el); }

function counts(state) {
  const out = { working: 0, idle: 0, waiting_user: 0, error: 0, stuck: 0, done: 0 };
  const bots = state.bots;
  if (bots && typeof bots === 'object' && Object.keys(bots).length) {
    for (const id of Object.keys(bots)) {
      if(bots[id].is_subagent)continue;
      const s = effStatus(bots[id]);
      out[s] = (out[s] || 0) + 1;
    }
  } else if (state.summary && typeof state.summary === 'object') {
    for (const s of Object.keys(out)) out[s] = Number(state.summary[s]) || 0;
  }
  return out;
}

/* ------------------------------------------------------------------ avatars: the 3D thumbnail, a lit status orb */

function makeAva(opts) {
  opts = opts || {};
  const img = h('img', { class: 'bhq-av-img', alt: '', draggable: 'false', decoding: 'async' });
  const fb = h('span', { class: 'bhq-av-fb', 'aria-hidden': 'true' });
  const inner = h('span', { class: 'bhq-av' }, img, fb);
  const orb = opts.orb === false ? null : h('i', { class: 'bhq-orb', 'aria-hidden': 'true' });
  const root = h('span', { class: 'bhq-ava' + (opts.cls ? ' ' + opts.cls : '') }, inner, orb);
  img.addEventListener('error', () => {
    if (img._name) U.failedThumbs.add(img._name);
    inner.classList.add('is-fb');
  });
  img.addEventListener('load', () => inner.classList.remove('is-fb'));
  return { root, inner, img, fb };
}

function setAvatar(av, bot, status) {
  const name = bot.avatar || bot.id || '';
  const color = isHex(bot.color) ? bot.color : '#A1A1A6';
  setVar(av.root, '--c', color);
  setVar(av.root, '--fg', textOn(color));
  setCls(av.root, 'is-dark', lum(color) < 0.04); // a black bot gets a faint rim so it does not sink into the graphite
  setAttr(av.root, 'data-s', status || null);
  setText(av.fb, initials(bot.name));
  if (av.img._name !== name) {
    av.img._name = name;
    if (!name || U.failedThumbs.has(name)) {
      av.img.removeAttribute('src');
      av.inner.classList.add('is-fb');
    } else {
      av.inner.classList.remove('is-fb');
      // a hand-made thumb when the bot has one, else the pre-rendered 3D library (its shape, the nearest colour)
      av.img.src = bot.has_glb === false ? libThumb(bot) : THUMB_BASE + encodeURIComponent(name) + '.png';
    }
  }
}

// assets/blobs/lib (export_lib.mjs + build_blobs.py --lib-from): every shape in the Grok Bot app's 11 colours, so anyone's bot gets a 3D avatar
const LIB_BASE = new URL('../../assets/blobs/lib/', import.meta.url).href;
// The office's own blobs (web/scene/blobgen.js, traced from the Grok Bot app) in the app's 11 colours, rendered by
// assets/_src/blobs/export_lib.mjs + build_blobs.py --lib-from. Shapes = web/scene/looks.js SHAPES (13 app shapes, then
// 3 older ids), colours = the app's palette (blobgen APP_COLORS); sphere + stem has its own picture (the app's 14th).
const LIB_SHAPES = ['sphere', 'cloud', 'rounded_square', 'star4', 'clover', 'bubble', 'flower', 'drop', 'pill', 'triangle', 'pentagon', 'star6', 'hexagon', 'rounded_cube', 'cone', 'trio'];
const LIB_COLORS = ['000000', 'A06B39', 'FB2B3E', 'FD690F', 'F59312', '14C16F', '13B7A2', '1E86FC', '8F59F9', 'F93498', '959595'];
function libThumb(bot) {
  const fnv = (s) => { let h = 2166136261; for (const ch of s) h = Math.imul(h ^ ch.charCodeAt(0), 16777619) >>> 0; return h; };
  const id = String(bot.id || bot.name || '');
  const look = bot.look || {};
  // nobody described it: the same shape as its blob in the office (looks.js hashShape picks among the 13 app shapes)
  let shape = LIB_SHAPES.includes(look.shape) ? look.shape : LIB_SHAPES[(fnv(`shape:${id}`) >>> 4) % 13];
  if (shape === 'sphere' && look.accessory === 'stem') shape = 'sphere_stem';
  const m = /^#([0-9a-f]{6})$/i.exec(look.color || bot.color || '');
  let hex = LIB_COLORS[fnv(`color:${id}`) % 10];
  if (m) {                                               // nearest by weighted RGB; greys go to the app's black or gray
    const rgb = (x) => [0, 2, 4].map((i) => parseInt(x.slice(i, i + 2), 16)), [r, g, b] = rgb(m[1]);
    let best = Infinity;
    if (Math.max(r, g, b) - Math.min(r, g, b) < 28) { hex = (r + g + b) / 3 < 75 ? '000000' : '959595'; best = 0; }
    for (const p of LIB_COLORS) {
      const [pr, pg, pb] = rgb(p), rm = (r + pr) / 2, d = (2 + rm / 256) * (r - pr) ** 2 + 4 * (g - pg) ** 2 + (3 - rm / 256) * (b - pb) ** 2;
      if (d < best) { best = d; hex = p; }
    }
  }
  return `${LIB_BASE}${shape}-${hex}.png`;
}

/* ------------------------------------------------------------------ init */

function own(el) { U.owned.push(el); return el; }

export function initUI(handlers) {
  if (U) {
    if (U.keyHandler) document.removeEventListener('keydown', U.keyHandler);
    if (U.onResize) removeEventListener('resize', U.onResize);
    if (U.ro) U.ro.disconnect();
    if (U.ro2) U.ro2.disconnect();
    if (U.ro3) U.ro3.disconnect();
    if (U.mqs && U.mqFn) for (const m of U.mqs) m.removeEventListener('change', U.mqFn);
    if (U.phone) { document.documentElement.classList.remove('bhq-phone', 'bhq-land'); }
    if (U.timer) clearInterval(U.timer);
    for (const el of U.owned) if (el.parentNode) el.remove();
  }
  U = freshState(handlers);
  document.documentElement.lang = 'en';

  const topbar = document.getElementById('topbar');
  const floors = document.getElementById('floors');
  const panel = document.getElementById('panel');
  let feed = document.getElementById('feed');
  if (!feed) { feed = own(h('section', { id: 'feed' })); document.body.append(feed); }
  setAttr(feed, 'aria-label', 'Activity feed');
  const scene = document.getElementById('scene');
  if (scene) setAttr(scene, 'aria-label', 'Office');
  if (floors) setAttr(floors, 'aria-label', 'Floors');
  if (panel) setAttr(panel, 'aria-label', 'Bot card and floor team');

  if (topbar) buildTopbar(topbar); else console.warn('[ui] #topbar not found');
  if (floors) buildFloors(floors); else console.warn('[ui] #floors not found');
  if (panel) buildPanel(panel); else console.warn('[ui] #panel not found');
  buildFeed(feed);
  buildZoom();
  buildDock(floors, feed);
  buildSheet(feed);
  watchPhone();

  U.keyHandler = (e) => {
    if (e.key !== 'Escape' || e.defaultPrevented) return;
    const t = e.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    if (U.last.opts.selectedBot && U.h.onSelectBot) U.h.onSelectBot(null);
  };
  document.addEventListener('keydown', U.keyHandler);

  // relative times ("12 min ago", "in 3 h") move on even when no new state arrives
  U.timer = setInterval(() => {
    if (document.hidden || !U.last.state) return;
    safe('dock', () => U.dock && renderDock(U.last.state));
  }, 15000);
}

/* ------------------------------------------------------------------ build: top bar */

function buildTopbar(root) {
  root.textContent = '';

  const clock = h('span', { class: 'bhq-num bhq-clock', text: '--:--' });
  const connectionLabel = h('span', { text: '連線中' });
  const connection = h('span', { class: 'bhq-live', 'data-connection': 'connecting', role: 'status' },
    h('i', { class: 'bhq-dot bhq-live-dot', 'aria-hidden': 'true' }), connectionLabel, ' · ', clock);
  const brand = h('div', { class: 'bhq-brand bhq-surface' },
    h('img', { class: 'bhq-mark', src: MARK_SRC, alt: '', draggable: 'false' }),
    h('b', { class: 'bhq-word', text: 'VIRTUAL OFFICE' }),
    h('span', { class: 'bhq-sep', 'aria-hidden': 'true' }),
    connection);

  const chips = {};
  const summary = h('div', { class: 'bhq-summary bhq-surface', role: 'group', 'aria-label': 'Bot statuses' });
  for (const p of SUMMARY) {
    const num = h('b', { class: 'bhq-num' });
    const label = h('span');
    const el = h('span', { class: 'bhq-schip' + (p.lead ? ' bhq-schip--lead' : ''), 'data-s': p.s },
      h('i', { class: 'bhq-dot', 'data-s': p.s, 'aria-hidden': 'true' }), num, label);
    chips[p.key] = { el, num, label, cfg: p };
    summary.append(el);
  }

  const usageLabel = h('span', { class: 'bhq-usage-label', text: 'Week' });
  const fill = h('i');
  const meter = h('span', {
    class: 'bhq-slider', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-label': 'Usage limit',
  }, fill);
  const pct = h('b', { class: 'bhq-num bhq-usage-pct' });
  const reset = h('span', { class: 'bhq-reset' });
  const usage = h('div', { class: 'bhq-usage bhq-surface', hidden: true }, usageLabel, meter, pct, h('span', { class: 'bhq-sep', 'aria-hidden': 'true' }), reset);
  const demo = h('span', { class: 'bhq-demo bhq-surface', text: 'DEMO', hidden: true, title: 'Demo data, not real bots' });
  // share an image of the office (web/share/, loaded on first use); audio/sound.js puts the speaker in front of it
  const share = h('button', { class: 'bhq-share bhq-surface', type: 'button', 'aria-label': 'Share an image of your office', title: 'Share', 'aria-haspopup': 'dialog', 'aria-expanded': 'false' }, svg(ICON.share));
  const loadShare = () => import('../share/share.js');
  share.addEventListener('pointerenter', loadShare, { once: true });
  share.addEventListener('click', () => {
    loadShare().then((m) => m.toggleShare({ anchor: share, state: U.last.state }), (err) => console.warn('[ui] share', err));
  });
  const right = h('div', { class: 'bhq-right' }, demo, usage);

  root.append(brand, summary, right);
  U.top = { root, clock, connection, connectionLabel, chips, summary, usage, usageLabel, meter, fill, pct, reset, demo };
}

/* ------------------------------------------------------------------ build: floors */

function buildFloors(root) {
  root.textContent = '';
  root.classList.add('bhq-surface', 'bhq-glow');
  const car = h('div', { class: 'bhq-car', 'aria-hidden': 'true' });
  const shaft = h('div', { class: 'bhq-shaft' }, car);
  const count = h('b', { class: 'bhq-num' });
  const fscroll = h('div', { class: 'bhq-fscroll' }, shaft);
  root.append(h('div', { class: 'bhq-fhead' }, h('span', { text: '樓層' }), count), fscroll);
  U.fl = { root, shaft, car, count, fscroll, carTop: null, carH: null, carShown: false, ready: false };
  if (typeof ResizeObserver === 'function') {
    U.ro = new ResizeObserver(() => placeCar());
    U.ro.observe(shaft);
  }
}

function makeFloor(id) {
  const num = h('span', { class: 'bhq-num bhq-fnum' });
  const name = h('b', { class: 'bhq-fname' });
  const dots = h('span', { class: 'bhq-fdots', 'aria-hidden': 'true' });
  const count = h('span', { class: 'bhq-num bhq-fcount' });
  const btn = h('button', { class: 'bhq-floor', type: 'button', 'data-id': id },
    num, h('span', { class: 'bhq-ftext' }, name, dots), count);
  btn.addEventListener('click', () => { if (U.h.onSelectFloor) U.h.onSelectFloor(id); });
  return { btn, num, name, dots, count, dotEls: [] };
}

/* ------------------------------------------------------------------ build: panel (floor team / bot card) */

function buildPanel(root) {
  root.textContent = '';

  /* floor team: a team card (one big number) over the list of bots */
  const teamTitle = h('h2', { class: 'bhq-h2' });
  const teamSub = h('span', { class: 'bhq-tsub' });
  const teamHint = h('p', {class:'office-ground-hint',text:'所有已入駐 Agent · 狀態、對話與交付即時同步',hidden:true});
  const big = h('b', { class: 'bhq-num' });
  const nWait = h('b', { class: 'bhq-num' });
  const nErr = h('b', { class: 'bhq-num' });
  const lWait = h('em', { text: '成員等待' });
  const lErr = h('em', { text: '成員錯誤' });
  const rowWait = h('span', null, lWait, nWait);
  const rowErr = h('span', null, lErr, nErr);
  const head = h('header', { class: 'bhq-thead bhq-surface bhq-glow', 'data-s': 'idle' },
    h('div', { class: 'bhq-ttop' }, teamTitle, teamSub),
    h('div', { class: 'bhq-tbot' },
      h('div', { class: 'bhq-tbig' }, big, h('span', { text: '位成員工作中' })),
      h('div', { class: 'bhq-tcounts' }, rowWait, rowErr)));
  const list = watchFade(h('div', { class: 'bhq-members', role: 'list' }));
  const teamEmpty = h('p', { class: 'bhq-note', text: '目前沒有成員。只有你指定入駐的 Agent 才會顯示。', hidden: true });
  const tcard = h('div', { class: 'bhq-tcard bhq-surface bhq-glow' }, list, teamEmpty);
  head.append(teamHint);
  const team = h('section', { class: 'bhq-team', 'aria-label': 'Floor team' }, head, tcard);

  /* bot card: a hero tinted by the bot's status over a graphite details card */
  const close = h('button', { class: 'bhq-close', type: 'button', 'aria-label': 'Close bot card', title: 'Close (Esc)' }, svg(ICON.close));
  close.addEventListener('click', () => { if (U.h.onSelectBot) U.h.onSelectBot(null); });

  const av = makeAva({ cls: 'bhq-ava--lg' });
  const name = h('h2', { class: 'bhq-h2', tabindex: '-1' });
  const where = h('p', { class: 'bhq-sub' });
  const stDot = h('i', { class: 'bhq-dot', 'aria-hidden': 'true' });
  const stTxt = h('span');
  const status = h('span', { class: 'bhq-status' }, stDot, stTxt);
  const now = h('p', { class: 'bhq-now' });
  const hero = h('div', { class: 'bhq-hero bhq-surface bhq-glow', 'data-s': 'idle' },
    close,
    h('div', { class: 'bhq-head' }, av.root, h('div', { class: 'bhq-head-text' }, name, where)),
    status, now);

  const task = h('p', { class: 'bhq-task' });
  const taskBlock = h('div', { class: 'bhq-block', hidden: true }, h('div', { class: 'bhq-label', text: 'Task' }), task);

  const stat = (unit) => {
    const v = h('b', { class: 'bhq-num' }); const l = h('small', { text: unit || '' });
    return { root: h('div', { class: 'bhq-stat' }, v, l), v, l };
  };
  const sSteps = stat(), sTasks = stat(), sHours = stat('h worked');
  const stats = h('div', { class: 'bhq-stats' }, sSteps.root, sTasks.root, sHours.root);

  const usageLabel = h('div', { class: 'bhq-label' });
  const fillBar = h('i');
  const fuel = h('span', { class: 'bhq-slider', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-label': 'Share of total usage' }, fillBar);
  const shareNum = h('b', { class: 'bhq-num' });
  const share = h('span', { class: 'bhq-share' }, shareNum, ' of all');
  const useBlock = h('div', { class: 'bhq-useblock' }, usageLabel, h('div', { class: 'bhq-use' }, fuel, share));

  const hist = h('ol', { class: 'bhq-steps', role: 'list' });
  const stepsBlock = h('div', { class: 'bhq-stepsblock' }, h('div', { class: 'bhq-label', text: 'Recent steps' }), hist);

  const body = watchFade(h('div', { class: 'bhq-card-body' }, taskBlock, stats, useBlock, stepsBlock));

  const cta = h('button', { class: 'bhq-cta', type: 'button', text: '開啟對話／記錄', disabled: true, title: '正在載入記錄入口' });
  cta.addEventListener('click', () => {
    const b = U.last.state && U.last.state.bots && U.last.state.bots[U.cardBot];
    if (!b || !b.chat_url) return;
    openChat(b.id, b.chat_url);
  });

  const workspace = h('button', {class:'office-workspace-edit',type:'button',text:'設定 Workspace',hidden:true});
  workspace.addEventListener('click', () => {
    const b = U.last.state?.bots?.[U.cardBot];
    if (canManageAgent(b)) window.dispatchEvent(new CustomEvent('office:workspace',{detail:{id:b.agent_id,name:b.name}}));
  });
  const more = h('div', { class: 'bhq-more bhq-surface bhq-glow bhq-glow--still' }, body, h('div', { class: 'bhq-card-foot' }, cta,workspace));
  const cgrab = h('span', { class: 'bhq-cgrab', 'aria-hidden': 'true' });
  const card = h('section', { class: 'bhq-card', hidden: true, 'aria-label': 'Bot card' }, cgrab, hero, more);

  root.append(card, team);

  U.panel = {
    root, team, tcard, teamHead: head, teamTitle, teamSub, teamHint, teamBig: big, nWait, nErr, rowWait, rowErr, lErr, list, teamEmpty,
    card: {
      root: card, hero, body, av, name, where, status, stDot, stTxt, now, taskBlock, task,
      sSteps, sTasks, sHours, usageLabel, useBlock, fuel, fillBar, shareNum, hist, histSig: null, cta, workspace, fresh: true,
    },
  };
}

function openChat(id, url) {
  if (U.h.onOpenChat) U.h.onOpenChat(id, url);
  else window.location.href = url;
}

/* ------------------------------------------------------------------ build: feed + zoom */

function buildFeed(root) {
  root.textContent = '';
  root.classList.add('bhq-surface', 'bhq-glow', 'bhq-glow--still');
  const list = h('ol', { class: 'bhq-frows', role: 'list', 'aria-live': 'off' });
  const empty = h('p', { class: 'bhq-note', text: '未有活動。', hidden: true });
  root.append(h('div', { class: 'bhq-fhead' }, h('span', { text: 'Activity' })), list, empty);
  U.feed = { root, list, empty };
}

function buildZoom() {
  const mk = (cls, icon, label, dir) => {
    const b = h('button', { class: 'bhq-zbtn ' + cls, type: 'button', 'aria-label': label, title: label }, svg(icon));
    b.addEventListener('click', () => { if (U.h.onZoom) U.h.onZoom(dir); });
    return b;
  };
  const root = own(h('div', { class: 'bhq-zoom bhq-surface', role: 'group', 'aria-label': 'Scene zoom' },
    mk('z-out', ICON.minus, 'Zoom out', 'out'),
    h('span', { class: 'bhq-sep', 'aria-hidden': 'true' }),
    mk('z-in', ICON.plus, 'Zoom in', 'in')));
  document.body.append(root);
  U.zoom = { root };
}

/* ------------------------------------------------------------------ build: dock (waiting on me / finished / routines) */

function dockCard(id, title, opts) {
  const count = h('span', { class: 'bhq-dcount bhq-num', text: '0' });
  const inner = watchFade(h('div', { class: 'bhq-dbody-in' }));
  const toggle = h('button', { class: 'bhq-dtoggle', type: 'button', 'aria-expanded': 'false' },
    h('span', { class: 'bhq-dtitle', text: title }), count);
  const head = h('div', { class: 'bhq-dh' + (opts && opts.eye ? ' has-eye' : '') }, toggle);
  let eye = null;
  if (opts && opts.eye) {
    eye = h('button', { class: 'bhq-eye', type: 'button', 'aria-pressed': 'false', 'aria-label': 'Hide this panel for the camera', title: 'Hide for the camera' },
      svg(ICON.eyeOn), svg(ICON.eyeOff));
    head.append(eye);
  }
  head.append(svg(ICON.chev));
  const root = h('section', { class: 'bhq-dcard bhq-surface bhq-glow bhq-glow--still', 'aria-label': title, 'data-dock': id },
    head, h('div', { class: 'bhq-dbody' }, inner));
  const card = { id, root, toggle, count, inner, eye, open: false, shown: false, rows: 0 };
  toggle.addEventListener('click', () => setDockOpen(card, !card.shown));
  return card;
}

const DOCK_HEAD = 44, DOCK_OPEN_MIN = 140, DOCK_OPEN_EMPTY = 92, DOCK_GAP = 8;

// card.open is what the person asked for; card.shown is what fits. A card that does not fit stays folded until there is room.
function paintDockCard(card, shown) {
  card.shown = !!shown;
  setCls(card.root, 'is-open', card.shown);
  setAttr(card.toggle, 'aria-expanded', card.shown ? 'true' : 'false');
  card.inner.inert = !card.shown;
}

function setDockOpen(card, on, save = true) {
  card.open = !!on;
  const d = U.dock;
  if (card.open) { // most recently opened last
    d.lru = d.lru.filter((c) => c !== card);
    d.lru.push(card);
  }
  if (save) {
    const o = {};
    for (const k of ['waiting', 'finished', 'routines']) o[k] = !!d[k].open;
    lsSet(LS_DOCK, JSON.stringify(o));
  }
  fitDock(d.avail == null ? Infinity : d.avail);
}

// If the open cards do not all fit between the floors and the feed, the least recently opened ones stay folded.
function fitDock(avail) {
  const d = U.dock;
  d.avail = avail;
  const cards = [d.check, d.waiting, d.finished, d.routines];
  const live = cards.filter((c) => !c.root.hidden);
  const on = new Set(live.filter((c) => c.open));
  const need = () => live.reduce((n, c) => n + (on.has(c) ? (c.rows ? DOCK_OPEN_MIN : DOCK_OPEN_EMPTY) : DOCK_HEAD), 0) + DOCK_GAP * Math.max(0, live.length - 1);
  while (on.size && need() > avail) {
    const victim = d.lru.find((c) => on.has(c)) || [...on][0]; // lru is oldest first
    on.delete(victim);
  }
  for (const c of cards) paintDockCard(c, on.has(c));
}

function setWaitBlur(on, save = true) {
  const d = U.dock;
  d.blurred = !!on;
  setCls(d.waiting.root, 'is-blurred', d.blurred);
  setAttr(d.waiting.eye, 'aria-pressed', d.blurred ? 'true' : 'false');
  setAttr(d.waiting.eye, 'aria-label', d.blurred ? 'Show this panel' : 'Hide this panel for the camera');
  setAttr(d.waiting.eye, 'title', d.blurred ? 'Show' : 'Hide for the camera');
  d.wlist.inert = d.blurred;
  if (save) lsSet(LS_BLUR, d.blurred ? '1' : '0');
}

function buildDock(floors, feed) {
  const root = own(h('aside', { class: 'bhq-dock', 'aria-label': 'Waiting on me, finished work and routines' }));
  const check = dockCard('check', 'Status check');
  const waiting = dockCard('waiting', 'Waiting on me', { eye: true });
  const finished = dockCard('finished', '交付紀錄');
  const routines = dockCard('routines', 'Routines');

  const wlist = h('div', { class: 'bhq-wlist' });
  const wempty = h('p', { class: 'bhq-note', text: '沒有待確認事項。', hidden: true });
  waiting.inner.append(wlist, wempty);

  const tl = h('ol', { class: 'bhq-tl', role: 'list' });
  const fempty = h('p', { class: 'bhq-note', text: '尚未收到交付紀錄。', hidden: true });
  finished.inner.append(tl, fempty);

  const rl = h('ul', { class: 'bhq-rl', role: 'list' });
  const rempty = h('p', { class: 'bhq-note', text: 'No routines scheduled.', hidden: true });
  routines.inner.append(rl, rempty);

  const cl = h('ul', { class: 'bhq-ck', role: 'list' });
  const cnote = h('p', { class: 'bhq-note bhq-cknote' });
  check.inner.append(cl, cnote);
  check.root.hidden = true;                                   // only while a check runs

  root.append(check.root, waiting.root, finished.root, routines.root);
  document.body.append(root);

  U.dock = {
    root, check, waiting, finished, routines, wlist, wempty, tl, fempty, rl, rempty, cl, cnote,
    lru: [], wrows: new Map(), crows: new Map(), checkT: null, finSig: null, finRefs: [], routSig: null, routRefs: [], copied: new Map(), blurred: false,
    floors, feed,
  };

  // open / folded: what the person left last time (the waiting card is open by default)
  let saved = null;
  try { saved = JSON.parse(lsGet(LS_DOCK) || 'null'); } catch { saved = null; }
  for (const k of ['routines', 'finished', 'waiting']) { // waiting last: it is the one that stays open when there is no room for all
    setDockOpen(U.dock[k], saved && typeof saved[k] === 'boolean' ? saved[k] : k === 'waiting', false);
  }
  waiting.eye.addEventListener('click', () => setWaitBlur(!U.dock.blurred));
  setWaitBlur(lsGet(LS_BLUR) === '1', false);

  // keep the dock between the floors card and the feed, whatever their heights
  U.onResize = () => layoutDock();
  addEventListener('resize', U.onResize);
  if (typeof ResizeObserver === 'function') {
    U.ro2 = new ResizeObserver(() => layoutDock());
    if (floors) U.ro2.observe(floors);
    if (feed) U.ro2.observe(feed);
  }
  layoutDock();
}

function shown(el) { return !!el && el.getClientRects().length > 0; }

function layoutDock() {
  const d = U && U.dock;
  if (!d) return;
  if (U.phone) { fitDock(Infinity); updateFades(); return; } // in the sheet the cards simply stack and the sheet scrolls
  const gutter = 24;
  // offsetTop/offsetHeight ignore the cards' entrance transform
  const top = shown(d.floors) ? d.floors.offsetTop + d.floors.offsetHeight + 10 : 96;
  const bottom = shown(d.feed) ? d.feed.offsetTop - 10 : innerHeight - gutter;
  const avail = Math.max(0, bottom - top);
  setPx(d.root, 'top', top);
  setPx(d.root, 'maxHeight', avail);
  fitDock(avail);
  updateFades();
}

/* ------------------------------------------------------------------ phones: the floor sheet and the bot sheet */

// the same query as ui.css would use; JS owns it so the DOM and the CSS never disagree
const MQ_PHONE = '(max-width: 600px), (max-height: 500px) and (max-width: 950px)';
const MQ_LAND = '(max-height: 500px) and (orientation: landscape)';
const DRAG_HYST = 6;

// progressive resistance past an edge (Apple's rubber-band)
function rubber(over, dim) { return (over * dim * 0.55) / (dim + 0.55 * Math.abs(over)); }
// px/s over the last ~100 ms of a pointer history
function velocity(hist) {
  const a = hist[hist.length - 1];
  let b = hist[0];
  for (const p of hist) if (a.t - p.t <= 100) { b = p; break; }
  const dt = a.t - b.t;
  return dt > 0 ? ((a.y - b.y) / dt) * 1000 : 0;
}
// where a flick would come to rest (the deceleration projection of WWDC "Designing Fluid Interfaces")
function project(v) { return (v / 1000) * 0.998 / (1 - 0.998); }

// A spring from x to `to` that starts at the finger's velocity; critically damped unless it carries a flick. Returns cancel().
function springTo(from, to, v0, set, done, resp, zeta) {
  const k = Math.pow((2 * Math.PI) / (resp || 0.36), 2), c = 2 * (zeta || 1) * Math.sqrt(k);
  let x = from, v = v0 || 0, last = performance.now(), raf = 0, dead = false;
  const tick = (now) => {
    if (dead) return;
    const dt = Math.min(0.032, Math.max(0.001, (now - last) / 1000));
    last = now;
    v += (-k * (x - to) - c * v) * dt;
    x += v * dt;
    if (Math.abs(x - to) < 0.35 && Math.abs(v) < 12) { raf = 0; set(to); if (done) done(); return; }
    set(x);
    raf = requestAnimationFrame(tick);
  };
  raf = requestAnimationFrame(tick);
  return () => { dead = true; if (raf) cancelAnimationFrame(raf); };
}

// A vertical drag that follows the finger 1:1 (pointer capture, a small hysteresis, a short velocity history).
function onDragY(el, hooks) {
  let d = null;
  el.addEventListener('pointerdown', (e) => {
    if (!hooks.can(e) || (e.pointerType === 'mouse' && e.button !== 0)) return;
    d = { id: e.pointerId, y0: e.clientY, s: hooks.begin(), hist: [{ t: e.timeStamp, y: e.clientY }], moved: false };
    try { el.setPointerCapture(e.pointerId); } catch { /* not capturable: the drag still works inside the element */ }
  });
  el.addEventListener('pointermove', (e) => {
    if (!d || e.pointerId !== d.id) return;
    const dy = e.clientY - d.y0;
    if (!d.moved && Math.abs(dy) < DRAG_HYST) return;
    d.moved = true;
    d.hist.push({ t: e.timeStamp, y: e.clientY });
    if (d.hist.length > 8) d.hist.shift();
    hooks.move(d.s + dy);
  });
  const end = (e) => {
    if (!d || e.pointerId !== d.id) return;
    const g = d;
    d = null;
    hooks.end(g.moved, g.moved ? velocity(g.hist) : 0);
  };
  el.addEventListener('pointerup', end);
  el.addEventListener('pointercancel', end);
}

let rootVars = {};
function rootVar(name, v) {
  if (rootVars[name] === v) return;
  rootVars[name] = v;
  document.documentElement.style.setProperty(name, v);
}

function buildSheet(feed) {
  const grab = h('button', { class: 'bhq-sgrab', type: 'button', 'aria-expanded': 'false', 'aria-label': 'Expand panel' }, h('i', { 'aria-hidden': 'true' }));
  const headSlot = h('div', { class: 'bhq-shead' });
  const peek = h('div', { class: 'bhq-speek' }, grab, headSlot);
  const body = h('div', { class: 'bhq-sbody' });
  const probe = h('i', { class: 'bhq-sprobe', 'aria-hidden': 'true' }); // its height is the bottom safe area
  const root = own(h('div', { class: 'bhq-sheet', role: 'region', 'aria-label': 'Floor panel' }, peek, body, probe));
  document.body.append(root);
  const S = U.sh = { root, peek, body, grab, headSlot, probe, feed, y: 0, H: 0, travel: 0, vis0: 132, open: false, busy: false, cancel: null };
  S.start = new URLSearchParams(location.search).get('sheet') === 'open';

  const move = (y) => {
    const t = S.travel;
    sheetSet(y < 0 ? -rubber(-y, 160) : y > t ? t + rubber(y - t, 160) : y);
  };
  const end = (moved, v) => {
    S.busy = false;
    if (!moved) { sheetGo(!S.open); return; }
    sheetGo(S.y + project(v) < S.travel / 2, v);
  };

  // drag or tap the grabber / the peek
  onDragY(peek, {
    can: () => U.phone && !U.land,
    begin: () => { if (S.cancel) { S.cancel(); S.cancel = null; } S.busy = true; return S.y; },
    move,
    end,
  });
  // a keyboard press on the grabber (detail 0); pointer taps are handled above
  grab.addEventListener('click', (e) => { if (e.detail === 0) sheetGo(!S.open); });

  // pull the open sheet down from its content when the content is scrolled to the top
  let t = null;
  body.addEventListener('touchstart', (e) => {
    t = S.open && !U.land && e.touches.length === 1 && body.scrollTop <= 0
      ? { y0: e.touches[0].clientY, on: false, s: 0, hist: [] } : null;
  }, { passive: true });
  body.addEventListener('touchmove', (e) => {
    if (!t) return;
    const y = e.touches[0].clientY;
    if (!t.on) {
      if (body.scrollTop > 0 || y - t.y0 < -2) { t = null; return; }
      if (y - t.y0 < 3) return;
      t.on = true; t.y0 = y; t.s = S.y; S.busy = true;
      if (S.cancel) { S.cancel(); S.cancel = null; }
    }
    if (e.cancelable) e.preventDefault();
    t.hist.push({ t: e.timeStamp, y });
    if (t.hist.length > 8) t.hist.shift();
    move(t.s + (y - t.y0));
  }, { passive: false });
  const tend = () => {
    if (!t) return;
    const g = t;
    t = null;
    if (g.on) end(true, velocity(g.hist));
  };
  body.addEventListener('touchend', tend);
  body.addEventListener('touchcancel', tend);

  // the bot card is a sheet of its own: swipe it down to close
  const P = U.panel;
  if (P) {
    const C = P.card;
    onDragY(C.root, {
      can: (e) => U.phone && !U.land && !!e.target.closest('.bhq-hero, .bhq-cgrab') && !e.target.closest('button'),
      begin: () => { if (U.cardStop) { U.cardStop(); U.cardStop = null; } return U.cardY; },
      move: (y) => { U.cardY = y < 0 ? -rubber(-y, 120) : y; cardSet(U.cardY); },
      end: (moved, v) => {
        if (!moved) return;
        const y = U.cardY;
        if (y + project(v) > 130 || v > 900) {
          const gone = C.root.offsetHeight + 24;
          U.cardStop = springTo(y, gone, v, (n) => { U.cardY = n; cardSet(n); }, () => {
            U.cardStop = null;
            if (U.h.onSelectBot) U.h.onSelectBot(null);
            setTimeout(() => { if (U.mode === 'card') { U.cardY = 0; cardSet(0); } }, 120); // nobody closed it: bring it back
          }, 0.3, 1);
        } else {
          U.cardStop = springTo(y, 0, v, (n) => { U.cardY = n; cardSet(n); }, () => { U.cardStop = null; }, 0.4, Math.abs(v) > 600 ? 0.8 : 1);
        }
      },
    });
  }

  if (typeof ResizeObserver === 'function') {
    U.ro3 = new ResizeObserver(() => { sheetMeasure(); updateVis(); });
    U.ro3.observe(root);
    U.ro3.observe(peek);
    if (P) U.ro3.observe(P.card.root);
  }
}

function cardSet(y) { U.panel.card.root.style.transform = y ? 'translate3d(0,' + y.toFixed(1) + 'px,0)' : ''; }
function cardReset() {
  if (U.cardStop) { U.cardStop(); U.cardStop = null; }
  U.cardY = 0;
  if (U.panel) cardSet(0);
}

function sheetSet(y) {
  const S = U.sh;
  S.y = y;
  S.root.style.transform = 'translate3d(0,' + y.toFixed(1) + 'px,0)';
  // the content fades in as the sheet is pulled up, so nothing shows under the home indicator at the peek
  S.body.style.opacity = clamp((S.travel - y) / 36, 0, 1).toFixed(2);
  updateVis();
}

// Open (y = 0) or fold to the peek (y = travel), starting at the finger's velocity.
function sheetGo(open, v0, instant) {
  const S = U.sh;
  if (!S || !U.phone) return;
  S.open = !!open;
  setAttr(S.grab, 'aria-expanded', S.open ? 'true' : 'false');
  setAttr(S.grab, 'aria-label', S.open ? 'Collapse panel' : 'Expand panel');
  setCls(S.root, 'is-open', S.open);
  S.body.inert = !S.open && !U.land;
  if (S.cancel) { S.cancel(); S.cancel = null; }
  if (U.land) return;
  const to = S.open ? 0 : S.travel;
  if (instant || reducedMotion.matches || document.hidden) { sheetSet(to); return; }
  S.cancel = springTo(S.y, to, v0 || 0, sheetSet, () => { S.cancel = null; }, S.open ? 0.4 : 0.34, Math.abs(v0 || 0) > 600 ? 0.82 : 1);
}

function sheetMeasure() {
  const S = U && U.sh;
  if (!S || !U.phone) return;
  S.H = S.root.offsetHeight;
  S.vis0 = S.peek.offsetHeight + S.probe.offsetHeight;
  S.travel = U.land ? 0 : Math.max(0, S.H - S.vis0);
  rootVar('--bhq-peek', S.vis0 + 'px');
  if (U.land) { S.root.style.transform = ''; S.body.style.opacity = ''; S.y = 0; }
  else if (!S.busy && !S.cancel) sheetSet(S.open ? 0 : S.travel);
}

// --bhq-vis: how much of the bottom is covered (the sheet, or the bot card). The zoom buttons ride on it and the
// 3D view shifts by half the difference, so the building stays centred in the free space above.
function updateVis() {
  const S = U && U.sh;
  if (!S || !U.phone) return;
  const c = U.panel && U.panel.card.root;
  let vis;
  if (U.land) vis = 0;
  else if (U.mode === 'card' && c && !c.hidden) vis = c.offsetHeight;
  else vis = Math.max(0, S.H - clamp(S.y, -40, S.travel));
  rootVar('--bhq-vis', Math.round(vis * 10) / 10 + 'px');
}

function watchPhone() {
  if (typeof matchMedia !== 'function') return;
  const a = matchMedia(MQ_PHONE), b = matchMedia(MQ_LAND);
  U.mqs = [a, b];
  U.mqFn = () => setPhone(a.matches, a.matches && b.matches);
  a.addEventListener('change', U.mqFn);
  b.addEventListener('change', U.mqFn);
  U.mqFn();
}

function setPhone(on, land) {
  const was = U.phone, S = U.sh, d = U.dock, p = U.panel;
  U.phone = on;
  U.land = on && land;
  const html = document.documentElement;
  html.classList.toggle('bhq-phone', on);
  html.classList.toggle('bhq-land', U.land);
  if (on && !was) {
    if (p) S.headSlot.append(p.teamHead);
    S.body.append(...[d && d.waiting.root, p && p.tcard, d && d.finished.root, d && d.routines.root, S.feed].filter(Boolean));
    S.open = S.start || U.land;
    S.start = false;
    sheetMeasure();
    sheetGo(S.open, 0, true);
    U.chipFloor = null;
  } else if (!on && was) {
    if (S.cancel) { S.cancel(); S.cancel = null; }
    S.root.style.transform = '';
    S.body.style.opacity = '';
    S.body.inert = false;
    cardReset();
    if (d) d.root.append(d.waiting.root, d.finished.root, d.routines.root);
    if (p) p.team.append(p.teamHead, p.tcard);
    if (S.feed) { if (d && d.root.parentNode) d.root.parentNode.insertBefore(S.feed, d.root); else document.body.append(S.feed); }
    for (const k of Object.keys(rootVars)) document.documentElement.style.removeProperty(k);
    rootVars = {};
  } else if (on) {
    if (U.land) S.open = true;
    sheetMeasure();
    sheetGo(S.open, 0, true);
  }
  layoutDock();
  updateFades();
  if (on && U.last.state) centerChip(U.activeFloorId);
}

// for tests and the harness: ?sheet=open does the same at load
export function setSheetOpen(open) { sheetGo(!!open, 0, true); }

/* ------------------------------------------------------------------ render */

export function renderUI(state, opts) {
  if (!U || !state) return;
  opts = opts || {};
  const o = { currentFloor: opts.currentFloor || null, selectedBot: opts.selectedBot || null };
  U.last = { state, opts: o };
  syncClock(state);
  if (!U.ready) { // let the first paint land without a fade, then mark the page ready (styles.css may use it)
    U.ready = true;
    requestAnimationFrame(() => requestAnimationFrame(() => document.documentElement.classList.add('bhq-ready')));
  }

  safe('topbar', () => U.top && renderTopbar(state));
  safe('floors', () => U.fl && renderFloors(state, o));
  safe('panel', () => U.panel && renderPanel(state, o));
  safe('feed', () => U.feed && renderFeed(state));
  safe('dock', () => U.dock && renderDock(state));
  safe('zoom', () => {
    if (U.zoom && !U.zoom.root.isConnected) document.body.append(U.zoom.root);
  });
  safe('fades', updateFades);
}

const reported = new Set();
function safe(where, fn) {
  try { fn(); } catch (err) {
    if (!reported.has(where)) { reported.add(where); console.error('[ui] render failed in ' + where, err); }
  }
}

/* ---- top bar */

function renderTopbar(state) {
  const t = U.top;
  setText(t.clock, hhmm(Date.now()));
  const connection = state.connection || { status: 'connecting', label: '等待連線' };
  setText(t.connectionLabel, connection.label);
  const brand = document.querySelector('.bhq-word');
  if (brand) setText(brand, state.brand || 'VIRTUAL OFFICE');
  setAttr(t.connection, 'data-connection', connection.status);
  setAttr(t.summary, 'title', connection.status === 'live' ? '目前同步狀態' : '目前保留最後一次狀態，等待同步恢復');

  const c = counts(state);
  for (const p of SUMMARY) {
    const r = t.chips[p.key];
    const n = p.n(c);
    const label = statusWord(p.s, n);
    setNum(r.num, n);
    setText(r.label, label);
    setCls(r.el, 'is-zero', n === 0);
    setCls(r.el, 'is-alert', !!p.alert && n > 0);
    if (p.hideZero) setHidden(r.el, n === 0);
    let title = n + ' ' + label;
    if (p.key === 'attention' && c.stuck > 0) {
      title = c.waiting_user + ' ' + statusWord('waiting_user', c.waiting_user) + ', ' + c.stuck + ' stuck';
    }
    setAttr(r.el, 'title', title);
  }

  // The usage meter hides when there is no usage report (the bots-only mode has none).
  const u = state.usage || {};
  const pct = Number.isFinite(u.total_used_pct) ? clamp(u.total_used_pct, 0, 100) : null;
  setHidden(t.usage, pct == null);
  if (pct != null) {
    const level = pct >= 95 ? 'error' : pct >= 85 ? 'warn' : null;
    setText(t.usageLabel, USAGE_TITLE[u.period] || 'Limit');
    setVar(t.meter, '--p', (pct / 100).toFixed(3));
    setText(t.pct, Math.round(pct) + '%');
    setAttr(t.meter, 'aria-valuenow', Math.round(pct));
    setAttr(t.meter, 'data-level', level);
    setAttr(t.usage, 'data-level', level);

    let resetText = '';
    const resetAt = parseTs(u.resets_at);
    if (resetAt != null) {
      const left = resetAt - nowMs();
      resetText = left > 0 ? 'resets in ' + fmtLeft(left) : 'resetting now';
    }
    setText(t.reset, resetText);
    setHidden(t.reset, !resetText);
    const sep = t.reset.previousElementSibling;
    if (sep) setHidden(sep, !resetText);
    const upd = parseTs(u.updated_at);
    setAttr(t.usage, 'title', upd != null ? 'Updated at ' + hhmm(upd) : null);
  }

  setHidden(t.demo, !state.demo);
}

/* ---- floors */

function countsText(c) {
  const parts = [];
  for (const s of ['working', 'waiting_user', 'error', 'stuck', 'done', 'idle']) {
    const n = Number(c && c[s]) || 0;
    if (n > 0) parts.push(n + ' ' + statusWord(s, n));
  }
  return parts.join(', ');
}

function floorBotIds(state, fl) {
  const bots = state.bots || {};
  if (Array.isArray(fl.bots)) return fl.bots;
  return Object.keys(bots).filter((id) => bots[id].floor === fl.id);
}

function renderFloors(state, o) {
  const f = U.fl;
  const bots = state.bots || {};
  // Building order: highest floor first (top of the card), floor 1 at the bottom.
  const list = (state.floors || []).slice().sort((a, b) => (Number(b.order) || 0) - (Number(a.order) || 0));
  const seen = new Set();
  const activeId = list.some((x) => x.id === o.currentFloor) ? o.currentFloor : null;
  U.activeFloorId = activeId;
  setText(f.count, list.length);

  list.forEach((fl, i) => {
    let r = U.floors.get(fl.id);
    if (!r) { r = makeFloor(fl.id); U.floors.set(fl.id, r); }
    seen.add(fl.id);

    const number = fl.order != null && Number.isFinite(Number(fl.order)) ? Number(fl.order) : list.length - i;
    const ids = floorBotIds(state, fl);
    setText(r.num, fl.kind==='overview'?'G/F':number);
    setText(r.name, fl.name || fl.id);
    const children=ids.filter(id=>bots[id]?.is_subagent).length,memberCount=ids.length-children;
    setText(r.count, children?`${memberCount}+${children}`:memberCount);

    // one tiny lit dot per bot, in desk order
    while (r.dotEls.length < ids.length) { const d = h('i', { class: 'bhq-dot' }); r.dotEls.push(d); r.dots.append(d); }
    while (r.dotEls.length > ids.length) r.dotEls.pop().remove();
    ids.forEach((id, k) => setAttr(r.dotEls[k], 'data-s', effStatus(bots[id])));
    // phones: a corner dot on the chip of a floor where someone needs you (the dots row is hidden there)
    let need = null;
    for (const id of ids) {
      const st = effStatus(bots[id]);
      if (st === 'error') { need = 'error'; break; }
      if (st === 'waiting_user' || st === 'stuck') need = 'waiting_user';
    }
    setAttr(r.btn, 'data-alert', need);

    const summary = countsText(fl.counts);
    setAttr(r.btn, 'aria-label', (fl.kind==='overview'?'G/F':'Floor '+number) + ', ' + (fl.name || fl.id) + ': ' + memberCount + ' 位成員'+(children?' + '+children+' 位子 Agent':'') + (summary ? ' (' + summary + ')' : ''));
    setAttr(r.btn, 'title', (children?`${memberCount} 位成員 · ${children} 位子 Agent · `:'')+(summary||''));
    setAttr(r.btn, 'aria-current', fl.id === activeId ? 'true' : null);
  });

  for (const [id, r] of U.floors) {
    if (!seen.has(id)) { r.btn.remove(); U.floors.delete(id); }
  }

  reconcile(f.shaft, list.map((x) => U.floors.get(x.id).btn), 1); // child 0 is the car
  placeCar();
  centerChip(activeId);
}

// phones: the floors are a scrolling chip row; bring the active chip to the middle when the floor changes
function centerChip(activeId) {
  const f = U.fl;
  if (!U.phone || !activeId || U.chipFloor === activeId) return;
  const r = U.floors.get(activeId);
  if (!r || !r.btn.offsetWidth) return;
  const first = U.chipFloor == null;
  U.chipFloor = activeId;
  const sc = f.fscroll, b = r.btn.getBoundingClientRect(), s = sc.getBoundingClientRect();
  sc.scrollTo({ left: Math.max(0, sc.scrollLeft + (b.left - s.left) - (s.width - b.width) / 2), behavior: first || reducedMotion.matches ? 'auto' : 'smooth' });
}

// The lit pill is one element that slides between floors (the "elevator car").
function placeCar() {
  const f = U && U.fl;
  if (!f) return;
  const r = U.activeFloorId ? U.floors.get(U.activeFloorId) : null;
  if (!r || !r.btn.isConnected || !r.btn.offsetHeight) {
    if (f.carShown) { f.car.style.opacity = '0'; f.carShown = false; }
    return;
  }
  const top = r.btn.offsetTop, hgt = r.btn.offsetHeight;
  if (f.carTop !== top || f.carH !== hgt) {
    f.carTop = top; f.carH = hgt;
    f.car.style.transform = 'translateY(' + top + 'px)';
    f.car.style.height = hgt + 'px';
  }
  if (!f.carShown) { f.car.style.opacity = '1'; f.carShown = true; }
  if (!f.ready) {
    f.ready = true; // enable the sliding transition only after the first placement
    requestAnimationFrame(() => requestAnimationFrame(() => f.shaft.classList.add('is-ready')));
  }
}

/* ---- panel */

function renderPanel(state, o) {
  const p = U.panel;
  const bots = state.bots || {};
  const floor = (state.floors || []).find((x) => x.id === o.currentFloor) || null;
  const bot = o.selectedBot ? bots[o.selectedBot] : null;
  const mode = bot ? 'card' : 'team';

  const prevMode = U.mode;
  const prevBotId = U.cardBot;
  const focusInTeam = p.team.contains(document.activeElement);
  const focusInCard = p.card.root.contains(document.activeElement);

  if (mode !== prevMode) { // the cards inside spring in by themselves when they become visible
    setHidden(p.team, mode === 'card');
    setHidden(p.card.root, mode === 'team');
    U.mode = mode;
    if (mode === 'card') cardReset();
    if (U.sh) { setCls(U.sh.root, 'is-away', mode === 'card'); updateVis(); } // phones: the bot sheet takes the bottom
  }

  if (mode === 'card') {
    if (U.cardBot !== bot.id) {
      U.cardBot = bot.id;
      p.card.histSig = null;
      p.card.fresh = true; // numbers of a new bot appear at once, they do not roll from the previous bot's
      p.card.body.scrollTop = 0;
      if (prevMode === 'card') restartAnim(p.card.root, 'is-enter');
    }
    renderCard(state, bot, floor);
    if (prevMode !== 'card' && focusInTeam) p.card.name.focus({ preventScroll: true });
  } else {
    U.cardBot = null;
    renderTeam(state, floor, o);
    if (prevMode === 'card' && focusInCard) {
      const cell = U.cells.get(prevBotId);
      if (cell) cell.btn.focus({ preventScroll: true });
    }
  }
}

/* ---- floor team */

// The team list includes transient children, while its primary status metrics
// describe selected members. Keep both groups explicit in the same card.
export function floorTeamMetrics(bots) {
  const empty=()=>({total:0,working:0,waiting:0,errors:0,done:0,idle:0});
  const members=empty(),children=empty();
  for(const bot of bots){
    const group=bot.is_subagent?children:members,status=effStatus(bot);group.total++;
    if(status==='working')group.working++;
    else if(status==='waiting_user'||status==='stuck')group.waiting++;
    else if(status==='error')group.errors++;
    else if(status==='done')group.done++;
    else group.idle++;
  }
  const rosterLabel=`${members.total} 位成員`+(children.total?` + ${children.total} 位子 Agent`:'');
  const childLabel=children.total?'子 Agent · '+[
    [children.working,'工作中'],[children.waiting,'待確認'],[children.errors,'錯誤'],[children.done,'完成'],[children.idle,'待命'],
  ].filter(([count])=>count).map(([count,label])=>`${count} ${label}`).join(' · '):'';
  return {members,children,rosterLabel,childLabel};
}

function makeCell(id) {
  const av = makeAva();
  const name = h('b', { class: 'bhq-mname' });
  const step = h('span', { class: 'bhq-mstep' });
  const seen = h('span', { class: 'bhq-mseen bhq-num' });
  const btn = h('button', { class: 'bhq-member', type: 'button', 'data-id': id, role: 'listitem' },
    av.root, h('span', { class: 'bhq-mtext' }, name, step), seen);
  btn.addEventListener('click', () => { if (U.h.onSelectBot) U.h.onSelectBot(id); });
  return { btn, av, name, step, seen };
}

function renderTeam(state, floor, o) {
  const p = U.panel;
  const bots = state.bots || {};
  let ids = floor ? floorBotIds(state, floor) : [];
  ids = ids.filter((id) => bots[id]);

  let animate = true;
  if (U.teamFloor !== (floor ? floor.id : null)) {
    U.teamFloor = floor ? floor.id : null;
    p.list.scrollTop = 0;
    animate = !U.teamReady; // a new floor shows its own numbers at once
    if (U.teamReady) restartAnim(p.list, 'is-enter');
  }
  U.teamReady = true;

  setText(p.teamTitle, floor ? floor.name || floor.id : 'No floor selected');
  const metrics=floorTeamMetrics(ids.map(id=>bots[id]));
  setHidden(p.teamHint, floor?.kind!=='overview'&&!metrics.children.total);
  setText(p.teamHint,metrics.childLabel||'所有已入駐 Agent · 狀態、對話與交付即時同步');
  const n = ids.length;
  const {working,waiting,errors}=metrics.members;
  setText(p.teamSub, floor ? metrics.rosterLabel : (U.phone ? 'Pick a floor above' : 'Pick a floor on the left'));
  // green while anyone works, graphite at rest (the wallpaper's team card)
  setAttr(p.teamHead, 'data-s', working > 0 ? 'working' : 'idle');
  setNum(p.teamBig, working, animate);
  setNum(p.nWait, waiting, animate);
  setNum(p.nErr, errors, animate);
  setText(p.lErr, '成員錯誤');
  setCls(p.rowWait, 'is-zero', !waiting);
  setCls(p.rowErr, 'is-zero', !errors);
  setHidden(p.teamEmpty, n > 0 || !floor);

  const seen = new Set(ids);
  for (const [id, c] of U.cells) {
    if (!seen.has(id)) { c.btn.remove(); U.cells.delete(id); }
  }
  const els = ids.map((id) => {
    let c = U.cells.get(id);
    if (!c) { c = makeCell(id); U.cells.set(id, c); }
    const bot = bots[id];
    const s = effStatus(bot);
    setAvatar(c.av, bot, s);
    setText(c.name, bot.name || id);
    const unrep = s === 'idle' ? unreportedText(bot) : '';
    const quiet = s === 'idle' && bot.status === 'working';   // went silent mid-task: its last step is stale
    setText(c.step, unrep || (quiet ? statusLabel(s) : bot.step || statusLabel(s)));
    setAttr(c.step, 'data-u', unrep ? '' : null);
    const seenAt = parseTs(bot.last_seen);
    setText(c.seen, seenAt == null ? '–' : relShort(seenAt));
    setAttr(c.seen, 'title', seenAt == null ? 'No status line yet' : 'Last status line ' + relAgo(seenAt) + ' (' + hhmm(seenAt) + ')');
    setAttr(c.btn, 'data-s', s);
    setAttr(c.btn, 'aria-label', (bot.name || id) + ', ' + statusLabel(s) + ', ' + seenText(bot));
    setAttr(c.btn, 'title', (bot.name || id) + ': ' + statusLabel(s) + (bot.step ? '. ' + bot.step : '') + ' · ' + seenText(bot));
    return c.btn;
  });
  reconcile(p.list, els, 0);
}

/* ---- bot card */

function histRow(ev) {
  return h('li', { class: 'bhq-step-row' },
    h('time', { class: 'bhq-num', text: hhmm(parseTs(ev.t)) }),
    h('span', { text: ev.step || statusLabel(ev.status), title: ev.step || '' }));
}

function renderCard(state, bot, viewedFloor) {
  const c = U.panel.card;
  const s = effStatus(bot);
  const now = nowMs();
  const animate = !c.fresh;

  setAttr(c.hero, 'data-s', s);
  setAvatar(c.av, bot, s);
  setText(c.name, bot.name || bot.id);

  const fl = (state.floors || []).find((x) => x.id === bot.floor);
  const overview=viewedFloor?.kind==='overview';
  const where = overview?'G/F':(fl && (fl.name || fl.id)) || bot.group || '';
  const desk = overview&&bot.ground_desk_id?' · '+deskLabel(bot.ground_desk_id):Number.isFinite(Number(bot.desk)) && bot.desk != null ? ' · desk ' + Number(bot.desk) : '';
  setText(c.where, where + desk + ' · ' + (bot.is_subagent?`${bot.parent_name||'主 Agent'} 的子 Agent`:seenText(bot)));
  setHidden(c.workspace, !canManageAgent(bot));

  setAttr(c.stDot, 'data-s', s);
  const since = parseTs(bot.since);
  const age = since == null ? '' : ' · ' + (now - since < 60000 ? 'just now' : fmtDur(now - since));
  setText(c.stTxt, (CHIP[s] || statusLabel(s)) + age);

  const unrep = s === 'idle' ? unreportedText(bot) : '';
  setText(c.now, unrep || (s === 'idle' && bot.status === 'working' ? 'Went quiet after: ' + (bot.step || 'a task') : bot.step || 'No data on the current step'));

  // Task: hide when empty, id-like ("tsk-1643") or a plain copy of the current step.
  const task = typeof bot.task === 'string' ? bot.task.trim() : '';
  const showTask = !!task && !/^tsk-\d+$/i.test(task) && task !== (bot.step || '').trim();
  setHidden(c.taskBlock, !showTask);
  setText(c.task, showTask ? task : '');

  // today
  const td = bot.today || {};
  const ev = Number(td.events) || 0, tk = Number(td.tasks_done) || 0;
  setNum(c.sSteps.v, ev, animate);
  setText(c.sSteps.l, plural(ev, 'step', 'steps'));
  setNum(c.sTasks.v, tk, animate);
  setText(c.sTasks.l, plural(tk, 'task', 'tasks'));
  setText(c.sHours.v, fmtHours(td.working_min));

  // usage share (hidden when there is no figure)
  const period = state.usage && state.usage.period;
  setText(c.usageLabel, USAGE_SPENT[period] || 'Usage');
  const share = Number.isFinite(bot.usage_share) ? clamp(bot.usage_share, 0, 1) : null;
  setHidden(c.useBlock, share == null);
  if (share != null) {
    setVar(c.fuel, '--p', (share > 0 ? Math.max(share, 0.02) : 0).toFixed(3));
    setAttr(c.fuel, 'aria-valuenow', Math.round(share * 100));
    setText(c.shareNum, Math.round(share * 100) + '%');
  }

  // last steps (rebuilt only when they change); the newest entry is usually the current step, already shown above
  let hist = (bot.history || []).slice(0, STEPS_SHOWN + 1);
  if (hist.length > 1 && hist[0].step === bot.step) hist = hist.slice(1);
  hist = hist.slice(0, STEPS_SHOWN);
  const sig = bot.id + '\n' + hist.map((x) => x.t + '|' + x.status + '|' + x.step).join('\n');
  if (sig !== c.histSig) {
    c.histSig = sig;
    c.hist.textContent = '';
    if (!hist.length) c.hist.append(h('li', { class: 'bhq-step-empty', text: 'No steps yet.' }));
    for (const x of hist) c.hist.append(histRow(x));
  }

  // chat button
  const has = !!bot.chat_url;
  if (c.cta.disabled === has) c.cta.disabled = !has;
  setAttr(c.cta, 'title', has ? '查看收到的任務、交付內容及來源對話' : '正在載入記錄入口');
  c.fresh = false;
}

/* ---- live feed (last 4 events of the whole building, newest on top) */

function makeRow(ev, state) {
  const bot = state.bots && state.bots[ev.bot];
  const name = ev.name || (bot && bot.name) || ev.bot || '';
  const s = ev.status || 'idle';
  return h('li', { class: 'bhq-frow', 'data-s': s },
    h('time', { class: 'bhq-num', text: hhmm(parseTs(ev.t)), datetime: ev.t || '' }),
    h('i', { class: 'bhq-dot', 'data-s': s, 'aria-hidden': 'true' }),
    h('span', { class: 'bhq-ftxt' }, h('b', { text: name }), ' ' + (ev.step || statusLabel(ev.status))));
}

function retireRow(row) {
  U.feedOut.add(row);
  row.classList.add('is-out');
  const done = () => { row.remove(); U.feedOut.delete(row); };
  if (reducedMotion.matches) done(); else setTimeout(done, FEED_OUT_MS);
}

function renderFeed(state) {
  const fd = U.feed;
  const items = (Array.isArray(state.feed) ? state.feed : []).slice(0, FEED_ROWS);

  // Stable keys; identical events are told apart by counting from the oldest end.
  const seenBase = new Map();
  const keys = new Array(items.length);
  for (let i = items.length - 1; i >= 0; i--) {
    const e = items[i];
    const base = e.t + '|' + e.bot + '|' + e.status + '|' + e.step;
    const n = seenBase.get(base) || 0;
    seenBase.set(base, n + 1);
    keys[i] = n ? base + '#' + n : base;
  }

  const keep = new Set(keys);
  for (const [k, row] of U.feedRows) {
    if (!keep.has(k)) { U.feedRows.delete(k); retireRow(row); }
  }

  let added = 0;
  for (const k of keys) if (!U.feedRows.has(k)) added++;
  // The first paint and a wholesale replacement do not animate; a trickle of new events slides in.
  const animate = !U.feedFirst && added > 0 && added < FEED_ROWS && !reducedMotion.matches;

  const els = items.map((ev, i) => {
    let row = U.feedRows.get(keys[i]);
    if (!row) {
      row = makeRow(ev, state);
      if (animate) {
        row.classList.add('is-new');
        const settle = () => row.classList.remove('is-new');
        row.addEventListener('animationend', settle, { once: true });
        setTimeout(settle, 700); // animationend never fires in a background tab
      }
      U.feedRows.set(keys[i], row);
    }
    return row;
  });
  reconcile(fd.list, els.concat([...U.feedOut]), 0);

  setHidden(fd.empty, items.length > 0);
  U.feedFirst = false;
}

/* ---- dock */

function renderDock(state) {
  renderCheck(state);
  renderWaiting(state);
  renderFinished(state);
  renderRoutines(state);
  layoutDock(); // after the cards know whether they are shown
}

function botFor(state, id, fallbackName) {
  const b = state.bots && state.bots[id];
  return b || { id, name: fallbackName || id, avatar: id };
}

/* Status check: the setup bot posted {"check":{"bots":[…]}} and messaged every bot; a tick for each one whose own status
   line arrived after that. Opens by itself when a new check starts; "no answer" after CHECK_WAIT_MS. */

const CHECK_WAIT_MS = 15 * 60e3;

function makeCheckRow(id) {
  const av = makeAva({ orb: false });
  const name = h('b', { class: 'bhq-ckname' });
  const at = h('span', { class: 'bhq-ckat bhq-num' });
  const li = h('li', { class: 'bhq-ckrow', 'data-id': id }, av.root, name, at);
  li.addEventListener('click', () => { if (U.h.onSelectBot) U.h.onSelectBot(id); });
  return { li, av, name, at };
}

function renderCheck(state) {
  const d = U.dock;
  const ck = state.check;
  const list = ck && Array.isArray(ck.bots) ? ck.bots : [];
  setHidden(d.check.root, !list.length);
  if (!list.length) { d.checkT = null; return; }
  if (d.checkT !== ck.t) { // a new check: open it on top of the others
    d.checkT = ck.t;
    setDockOpen(d.check, true, false);
  }
  const started = parseTs(ck.t) ?? nowMs();
  const late = nowMs() - started > CHECK_WAIT_MS;
  const ok = list.filter((b) => b.at).length;
  setText(d.check.count, ok + '/' + list.length);
  setAttr(d.check.count, 'data-s', ok === list.length ? 'done' : late ? 'error' : 'waiting_user');
  d.check.rows = list.length;
  setCls(d.check.root, 'has-rows', true);

  const seen = new Set();
  const els = list.map((b) => {
    const id = b.id || b.name;
    seen.add(id);
    let r = d.crows.get(id);
    if (!r) { r = makeCheckRow(id); d.crows.set(id, r); }
    setAvatar(r.av, botFor(state, id, b.name), null);
    setText(r.name, b.name || id);
    const at = parseTs(b.at);
    setAttr(r.li, 'data-ok', at != null ? 'yes' : late ? 'no' : 'wait');
    setText(r.at, at != null ? '✓ ' + hhmm(at) : late ? 'no answer' : 'waiting…');
    return r.li;
  });
  for (const [id, r] of d.crows) if (!seen.has(id)) { r.li.remove(); d.crows.delete(id); }
  reconcile(d.cl, els, 0);
  setText(d.cnote, ok === list.length ? 'Every bot answered.'
    : late ? 'Ask the bots that did not answer to re-read their instructions, or run the setup again.'
      : 'Started ' + hhmm(started) + '. Each bot ticks off when its own status line arrives.');
}

/* Waiting on me: every bot that is waiting for the user or in error, newest first. */

function makeWaitRow(id) {
  const av = makeAva();
  const name = h('button', { class: 'bhq-wname', type: 'button' });
  const btn = h('button', { class: 'bhq-wbtn', type: 'button', text: 'Open chat', hidden: true });
  const st = h('b');
  const step = h('span');
  const text = h('p', { class: 'bhq-wtext' }, st, step);
  const row = h('div', { class: 'bhq-wrow', 'data-id': id },
    av.root,
    h('div', { class: 'bhq-wmain' }, h('div', { class: 'bhq-wtop' }, name, btn), text));
  const select = () => { if (U.h.onSelectBot) U.h.onSelectBot(id); };
  name.addEventListener('click', select);
  row.addEventListener('click', (e) => { if (!e.target.closest('button')) select(); });
  btn.addEventListener('click', () => {
    const b = U.last.state && U.last.state.bots && U.last.state.bots[id];
    if (b && b.chat_url) openChat(id, b.chat_url);
  });
  return { row, av, name, btn, st, step };
}

function renderWaiting(state) {
  const d = U.dock;
  const bots = state.bots || {};
  const ids = Object.keys(bots)
    .filter((id) => { const s = effStatus(bots[id]); return s === 'waiting_user' || s === 'error'; })
    .sort((a, b) => (parseTs(bots[b].last_event) || 0) - (parseTs(bots[a].last_event) || 0)
      || String(bots[a].name).localeCompare(String(bots[b].name)));

  const errors = ids.filter((id) => effStatus(bots[id]) === 'error').length;
  setText(d.waiting.count, ids.length);
  d.waiting.rows = ids.length;
  setCls(d.waiting.root, 'has-rows', ids.length > 0);
  setAttr(d.waiting.count, 'data-s', errors ? 'error' : ids.length ? 'waiting_user' : null);

  const seen = new Set(ids);
  for (const [id, r] of d.wrows) {
    if (!seen.has(id)) { r.row.remove(); d.wrows.delete(id); }
  }
  const els = ids.map((id) => {
    let r = d.wrows.get(id);
    if (!r) { r = makeWaitRow(id); d.wrows.set(id, r); }
    const bot = bots[id];
    const s = effStatus(bot);
    setAttr(r.row, 'data-s', s);
    setAvatar(r.av, bot, s);
    setText(r.name, bot.name || id);
    setText(r.st, CHIP[s] || statusLabel(s));
    setText(r.step, bot.step ? ' · ' + bot.step : '');
    setHidden(r.btn, !bot.chat_url);
    return r.row;
  });
  reconcile(d.wlist, els, 0);
  setHidden(d.wempty, ids.length > 0);
}

/* Finished: the `done` lines of the last 24 hours, newest first, on a timeline. Hidden when the server sends none. */

function renderFinished(state) {
  const d = U.dock;
  const has = Array.isArray(state.finished);
  setHidden(d.finished.root, !has);
  if (!has) return;

  const now = nowMs();
  const items = state.finished
    .map((e) => ({ e, t: parseTs(e && e.t) }))
    .filter((x) => x.e && (x.e.persisted || x.t == null || now - x.t <= DAY_MS))
    .sort((a, b) => (b.t || 0) - (a.t || 0))
    .slice(0, FINISHED_MAX);
  setText(d.finished.count, items.length);
  d.finished.rows = items.length;
  setCls(d.finished.root, 'has-rows', items.length > 0);
  setHidden(d.fempty, items.length > 0 && state.history_ok !== false);
  setText(d.fempty, state.history_ok === false ? '交付記錄暫時未能更新；以下是已保存內容。' : '尚未收到交付紀錄。');

  const sig = items.map((x) => x.t + '|' + x.e.bot + '|' + x.e.step + '|' + x.e.url).join('\n');
  if (sig !== d.finSig) {
    d.finSig = sig;
    d.tl.textContent = '';
    d.finRefs = items.map((x) => {
      const bot = botFor(state, x.e.bot, x.e.name);
      const av = makeAva({ orb: false });
      setAvatar(av, bot, null);
      const time = h('span', { class: 'bhq-tl-time bhq-num' });
      const li = h('li', { class: 'bhq-tl-row' },
        av.root,
        h('div', { class: 'bhq-tl-main' },
          h('div', { class: 'bhq-tl-top' }, h('b', { class: 'bhq-tl-name', text: x.e.name || bot.name || x.e.bot || '' }), time),
          h('div', { class: 'bhq-tl-step', text: x.e.step || 'done', title: x.e.step || '' })));
      if (x.e.url) {
        const open = h('button', { class: 'bhq-delivery-open', type: 'button', text: '查看交付' });
        open.addEventListener('click', () => openChat(x.e.bot, x.e.url));
        li.lastElementChild.append(open);
      }
      d.tl.append(li);
      return { time, t: x.t };
    });
  }
  for (const r of d.finRefs) setText(r.time, r.t == null ? '' : relAgo(r.t));
}

/* Routines: sorted by next run; "Turn off" copies `pause routine <name>` and opens the owner's chat. */

async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
  } catch { /* fall through to the old way */ }
  try {
    const ta = h('textarea', { 'aria-hidden': 'true', tabindex: '-1', readonly: true });
    ta.value = text;
    ta.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0;pointer-events:none';
    document.body.append(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return !!ok;
  } catch { return false; }
}

async function turnOffRoutine(r, ref) {
  const d = U.dock;
  const cmd = 'pause routine ' + r.routine;
  const ok = await copyText(cmd);
  setText(ref.noteText, ok ? 'Copied — paste it in the chat' : 'Could not copy — send: ' + cmd);
  const key = ref.key;
  clearTimeout(d.copied.get(key));
  ref.li.classList.add('is-copied');
  d.copied.set(key, setTimeout(() => { d.copied.delete(key); ref.li.classList.remove('is-copied'); }, 4200));
  const bot = U.last.state && U.last.state.bots && U.last.state.bots[r.bot];
  if (bot && bot.chat_url) openChat(bot.id, bot.chat_url);
}

function renderRoutines(state) {
  const d = U.dock;
  const has = Array.isArray(state.routines);
  setHidden(d.routines.root, !has);
  if (!has) return;

  const items = state.routines.filter((r) => r && r.routine).slice()
    .sort((a, b) => (parseTs(a.next_run) ?? Infinity) - (parseTs(b.next_run) ?? Infinity));
  setText(d.routines.count, items.length);
  d.routines.rows = items.length;
  setCls(d.routines.root, 'has-rows', items.length > 0);
  setHidden(d.rempty, items.length > 0);

  const sig = items.map((r) => r.bot + '|' + r.routine + '|' + r.schedule + '|' + r.next_run + '|' + r.name).join('\n');
  if (sig !== d.routSig) {
    d.routSig = sig;
    d.rl.textContent = '';
    d.routRefs = items.map((r) => {
      const bot = botFor(state, r.bot, r.name);
      const owner = r.name || bot.name || r.bot || '';
      const key = r.bot + '|' + r.routine;
      const av = makeAva({ orb: false });
      setAvatar(av, bot, null);
      const next = h('span', { class: 'bhq-rnext bhq-num' });
      const noteText = h('span');
      const btn = h('button', { class: 'bhq-roff', type: 'button', text: 'Turn off', title: 'Copies "pause routine ' + r.routine + '" and opens the chat' });
      const li = h('li', { class: 'bhq-rrow' + (d.copied.has(key) ? ' is-copied' : '') },
        av.root,
        h('div', { class: 'bhq-rmain' },
          h('div', { class: 'bhq-rtop' }, h('b', { class: 'bhq-rname', text: r.routine, title: r.routine }), next),
          h('div', { class: 'bhq-rbot' },
            h('span', { class: 'bhq-rmeta', text: owner + (r.schedule ? ' · ' + r.schedule : ''), title: owner + (r.schedule ? ' · ' + r.schedule : '') }),
            h('span', { class: 'bhq-rnote', role: 'status' }, svg(ICON.check), noteText),
            btn)));
      const ref = { li, next, noteText, key, t: parseTs(r.next_run) };
      setText(noteText, 'Copied — paste it in the chat');
      btn.addEventListener('click', () => turnOffRoutine(r, ref));
      d.rl.append(li);
      return ref;
    });
  }
  for (const ref of d.routRefs) {
    setText(ref.next, ref.t == null ? 'no date' : relIn(ref.t));
    setAttr(ref.next, 'title', ref.t == null ? null : fullLocal(ref.t));
  }
}
