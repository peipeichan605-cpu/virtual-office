// Time of day (CONTRACT v2.2): the light inside and on the building follows the real sun where the viewer is (the time
// zone's city, or ?lat=..&lon=..). Everything is a continuous function of the sun's elevation and side of the sky,
// recomputed every 20 s and followed with a soft lag, so nothing ever switches: light colour and strength, the sun's
// direction (and with it the stripes and the length of every shadow), the room's ambient, the night lightmaps, the
// lamps and the glass. Five looks are blended by elevation: night (below -9°), twilight (dawn or dusk, around the
// horizon), low sun (morning or golden, under ~20°) and day; morning and evening differ by the side of the sky.
//
// One shadow map from the sun shades everything: the lit furniture and the bots (three's own shadows) and the unlit
// baked floor and wall, which sunpatch.js relights from the same map. The room is open on top, like the picture: the
// sun reaches the whole floor, the window frame casts the stripes. The studio backdrop (studio.js) stays white.
// Publishes <html data-daylight="dawn|day|golden|dusk|night"> and --night (0..1), at most once a second.
//
// Tests: ?time=18:50 or window.bothq.setTime('18:50') hold that time on a fixed reference day (latitude 42.7°, sun
// declination +10°, solar noon 12:40: 07:15 morning, 13:00 day, 18:50 sunset, 23:00 night), the same on every date;
// ?time=18:50&real=1 = today's real sun at that time; ?time=auto or setTime(null) = the live clock.
import * as THREE from 'three';
import { smoothstep, lerp, clamp } from './spring.js';
import { createSunPatch } from './sunpatch.js';
import {shadowVolume,shadowMapSize,resizeShadowMap,NATIVE_LAMP} from './lighting-layout.js';

const rgb = (hex) => { const c = new THREE.Color(hex); c.convertLinearToSRGB(); return [c.r, c.g, c.b]; };

// bake: share of the night lightmaps; tint: linear multiplier of the baked surfaces (the room's ambient light);
// floorSun / floorSunC: the sun on the baked floor (1 = as bright as the bake's own sunlit floor); glass: the curtain wall.
const P = (o) => o;
const DAY = P({
  night: 0, bake: 0, lamps: 0, windows: 0,
  sunI: 2.4, sunC: rgb('#FFF1DE'),
  hemiI: 1.05, hemiSky: rgb('#F3F2F0'), hemiGnd: rgb('#CFC8BE'),
  fillI: 0.35, fillC: rgb('#E6EEFF'), envI: 0.5, exposure: 1.05,
  shadow: 0.34, tint: [1.42, 1.43, 1.46], floorSun: 0.72, floorSunC: rgb('#FFF6EC'),
  glassC: rgb('#D2E4F2'), glassA: 0.22, glassE: 0.12,
});
const MORNING = P({                                       // fresh: near-white floor, cool shade, pale sky in the glass
  night: 0, bake: 0, lamps: 0, windows: 0.05,
  sunI: 2.6, sunC: rgb('#FFEFD9'),
  hemiI: 1.0, hemiSky: rgb('#E1EAFF'), hemiGnd: rgb('#C6CAD2'),
  fillI: 0.38, fillC: rgb('#DCE6FF'), envI: 0.5, exposure: 1.07,
  shadow: 0.36, tint: [2.05, 2.15, 2.35], floorSun: 0.72, floorSunC: rgb('#FFEEDA'),
  glassC: rgb('#A6CCF2'), glassA: 0.82, glassE: 0.5,
});
const GOLDEN = P({                                        // sunset: warm amber room, glowing glass, long golden light
  night: 0, bake: 0, lamps: 0.15, windows: 0.15,
  sunI: 2.8, sunC: rgb('#FFA458'),
  hemiI: 0.85, hemiSky: rgb('#FFD0A4'), hemiGnd: rgb('#B48466'),
  fillI: 0.3, fillC: rgb('#FFC9A4'), envI: 0.4, exposure: 1.05,
  shadow: 0.4, tint: [2.3, 1.32, 0.62], floorSun: 1.0, floorSunC: rgb('#FFD27E'),
  glassC: rgb('#FFC064'), glassA: 0.86, glassE: 1.0,
});
const DAWN = P({                                          // blue hour before sunrise
  night: 0.5, bake: 0.3, lamps: 0.6, windows: 0.4,
  sunI: 0.3, sunC: rgb('#FFC9B0'),
  hemiI: 0.8, hemiSky: rgb('#B9C2E6'), hemiGnd: rgb('#8A8798'),
  fillI: 0.25, fillC: rgb('#BFC8F0'), envI: 0.3, exposure: 1.04,
  shadow: 0.3, tint: [1.3, 1.32, 1.48], floorSun: 0, floorSunC: rgb('#FFEBD2'),
  glassC: rgb('#7F98CF'), glassA: 0.6, glassE: 0.45,
});
const DUSK = P({                                          // after sunset: lamps come on in a violet afterglow
  night: 0.6, bake: 0.35, lamps: 0.85, windows: 0.7,
  sunI: 0.3, sunC: rgb('#FF9070'),
  hemiI: 0.7, hemiSky: rgb('#8C7896'), hemiGnd: rgb('#3A3040'),
  fillI: 0.2, fillC: rgb('#8A80B8'), envI: 0.28, exposure: 1.03,
  shadow: 0.32, tint: [1.3, 1.02, 0.84], floorSun: 0, floorSunC: rgb('#FFD27E'),
  glassC: rgb('#4A5487'), glassA: 0.8, glassE: 0.5,
});
const NIGHT = P({                                         // dim warm room, lamp pools on the desks, deep navy glass
  night: 1, bake: 0.6, lamps: 1, windows: 1,
  sunI: 0.2, sunC: rgb('#8CA6FF'),
  hemiI: 0.4, hemiSky: rgb('#6A6158'), hemiGnd: rgb('#2A2420'),
  fillI: 0.15, fillC: rgb('#8C8098'), envI: 0.2, exposure: 1.0,
  shadow: 0.4, tint: [0.95, 0.8, 0.62], floorSun: 0, floorSunC: rgb('#FFEBD2'),
  glassC: rgb('#1A2A66'), glassA: 0.92, glassE: 0.5,
});

function mixParams(a, b, t) {
  const out = {};
  for (const k in a) {
    out[k] = Array.isArray(a[k]) ? a[k].map((v, i) => lerp(v, b[k][i], t)) : lerp(a[k], b[k], t);
  }
  return out;
}

// ---------- the sun ----------
const RAD = Math.PI / 180;
const REF = { lat: 42.7, dec: 10, noon: 760 };            // the reference day of ?time=
const PLACES = {                                          // a city per common time zone
  'Europe/Sofia': [42.7, 23.3], 'Europe/Kiev': [50.45, 30.5], 'Europe/Kyiv': [50.45, 30.5], 'Europe/Moscow': [55.75, 37.6],
  'Europe/Warsaw': [52.2, 21], 'Europe/Berlin': [52.5, 13.4], 'Europe/Paris': [48.85, 2.35], 'Europe/London': [51.5, -0.1],
  'Europe/Madrid': [40.4, -3.7], 'Europe/Rome': [41.9, 12.5], 'Europe/Istanbul': [41, 29], 'Asia/Dubai': [25.2, 55.3],
  'Asia/Kolkata': [28.6, 77.2], 'Asia/Singapore': [1.35, 103.8], 'Asia/Tokyo': [35.7, 139.7], 'Australia/Sydney': [-33.9, 151.2],
  'America/New_York': [40.7, -74], 'America/Chicago': [41.9, -87.6], 'America/Denver': [39.7, -105], 'America/Los_Angeles': [34.05, -118.25],
  'America/Sao_Paulo': [-23.55, -46.6],
};
function wherePlace(params) {
  if (params.has('lat') && params.has('lon')) return [Number(params.get('lat')), Number(params.get('lon'))];
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return PLACES[zone] || [45, -new Date().getTimezoneOffset() / 4];   // unknown zone: mid latitudes, longitude from the offset
}

// [elevation, azimuth from the south, + toward the west] in degrees, from the hour angle and declination (radians)
function horizon(H, dec, lat) {
  const phi = lat * RAD;
  const el = Math.asin(Math.sin(phi) * Math.sin(dec) + Math.cos(phi) * Math.cos(dec) * Math.cos(H));
  const az = Math.atan2(Math.sin(H), Math.cos(H) * Math.sin(phi) - Math.tan(dec) * Math.cos(phi));
  return [el / RAD, az / RAD];
}
// The sun's position at a moment and place (the SunCalc formulas: days since J2000, mean anomaly, ecliptic longitude).
export function sunPosition(ms, lat, lon) {
  const d = ms / 864e5 - 10957.5;
  const M = RAD * (357.5291 + 0.98560028 * d);
  const L = M + RAD * (1.9148 * Math.sin(M) + 0.02 * Math.sin(2 * M) + 0.0003 * Math.sin(3 * M) + 102.9372) + Math.PI;
  const e = RAD * 23.4397;
  const dec = Math.asin(Math.sin(e) * Math.sin(L)), ra = Math.atan2(Math.sin(L) * Math.cos(e), Math.cos(L));
  return horizon(RAD * (280.16 + 360.9856235 * d + lon) - ra, dec, lat);
}
function refPosition(minutes) {
  return horizon(RAD * (minutes - REF.noon) / 4, REF.dec * RAD, REF.lat);
}

// Everything the light needs, from where the sun is. The sun's side of the sky (azSouth: morning < 0 < evening) picks
// morning or evening; its elevation blends night, twilight, low sun and day. In the room the sun stays behind the glass
// wall (it faces -z): scene azimuth 270° - 0.3 * azSouth, so the light falls in down-left all day, shallower in the
// morning and steeper in the evening, and never lower than 5° (shadows ~11x the height). As it sets it eases into a
// high, cool moon.
export function lightAt(el, azSouth) {
  const e = smoothstep(-40, 40, azSouth);
  let p = mixParams(mixParams(MORNING, GOLDEN, e), DAY, smoothstep(4, 30, el));
  p = mixParams(mixParams(DAWN, DUSK, e), p, smoothstep(-1.5, 3, el));
  p = mixParams(NIGHT, p, smoothstep(-9, -1, el));
  const moon = 1 - smoothstep(-6, 2, el);
  p.sunEl = lerp(Math.max(el, 5), 42, moon);
  p.sunAz = lerp(clamp(270 - 0.3 * azSouth, 235, 305), 250, moon);
  return p;
}

// The name <html data-daylight> carries (CONTRACT: dawn|day|golden|dusk|night).
export function phaseAt(el, azSouth) {
  const evening = azSouth > 0;
  return el < -5 ? 'night' : el < 2 ? (evening ? 'dusk' : 'dawn') : el < 15 ? (evening ? 'golden' : 'dawn') : 'day';
}

function parseClock(s) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(s || '').trim());
  if (!m) return null;
  return clamp(Number(m[1]), 0, 24) * 60 + clamp(Number(m[2]), 0, 59);
}

const PLAN_MS = 20000;                                    // the target follows the clock every 20 s
const SCAN_MS = 2000;                                     // materials and shadow flags are refreshed every 2 s
const LAMP_COUNT = 6;
const LAMP_COLOR = NATIVE_LAMP.color;
const LAMP_INTENSITY = NATIVE_LAMP.intensity;              // the spot lights on the lit furniture (cd)
const LAMP_POOL = 4.0;                                    // the same lamps on the baked floor (sunpatch.js)
const LAMP_ANGLE = NATIVE_LAMP.angle;                      // rad, half the cone of a pendant's shade
const LAMP_PENUMBRA = NATIVE_LAMP.penumbra;
const ROOM = { x: 8.3, z: 6.3, y0: -0.05, y1: 3.0 };      // what the sun's shadow camera must cover, around the floor
const COARSE = typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches;
const isLit = (m) => m.isMeshStandardMaterial || m.isMeshLambertMaterial || m.isMeshPhongMaterial || m.isMeshToonMaterial;
const NO_SHADOW = /^(slab|glass)/;                        // the floor slab sits under the floor; glass lets the sun through
const TABLE = /^table_\d+$/;                              // original baked workspace table pools

export function createDaylight({ scene, renderer }) {
  const params = new URLSearchParams(location.search);
  // the shadow map is on from the start, so no lit material ever compiles twice
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFShadowMap;
  const sun = new THREE.DirectionalLight(0xffffff, 1);
  sun.castShadow = true;
  sun.shadow.mapSize.setScalar(COARSE ? 1024 : 2048);
  sun.shadow.bias = -0.0005;
  sun.shadow.normalBias = 0.02;
  sun.shadow.radius = 2;
  const hemi = new THREE.HemisphereLight(0xffffff, 0xffffff, 1);
  const fill = new THREE.DirectionalLight(0xffffff, 0.4);
  fill.position.set(6, 5, 8);
  scene.add(sun, sun.target, hemi, fill);

  // the desk pendants: soft downward cones, so they leave pools on the desks (the floor's pools: sunpatch.js)
  const lamps = [];
  for (let i = 0; i < LAMP_COUNT; i++) {
    const l = new THREE.SpotLight(LAMP_COLOR, 0, 0, LAMP_ANGLE, LAMP_PENUMBRA, 2);
    l.castShadow = false;
    l.position.set(0, -99, 0);
    scene.add(l, l.target);
    lamps.push(l);
  }
  const lampLight = new THREE.Color();
  let tables = [];                                // desk tops of the active floor: rects (world x/z) + top height (floor-local)

  const patch = createSunPatch(sun);
  const tracked = new Map();                      // material -> kind ('lamp' | 'windows')
  const baked = new Set();                        // the building's unlit baked materials (their colour = the ambient tint)
  const glass = new Set();                        // the curtain wall's panes
  const uNight = { value: 0 };
  const state = { night: 0, lampsK: 0, windowsK: 0, phase: 'day', minutes: 720, el: 45, az: 0, params: null };
  let override = null;                            // minutes (?time= / setTime), or null = the live clock
  const real = params.get('real') === '1';        // ?time=..&real=1: today's real sun at that time
  let building = null;
  let cur = null;
  let target = null;
  let lampSource = null;                          // () => { positions: Vector3[], k: number }
  let lastHtml = -1e9;
  let lastPlan = -1e9;
  let lastScan = -1e9;

  const dir = new THREE.Vector3();
  const centre = new THREE.Vector3();
  const corner = new THREE.Vector3();
  const tmp = new THREE.Color();

  const urlTime = params.get('time');
  if (urlTime && urlTime !== 'auto') override = parseClock(urlTime);

  function setTime(value) {
    override = value && value !== 'auto' ? parseClock(value) : null;
    lastPlan = lastHtml = -1e9;
  }

  function plan() {
    const d = new Date();
    if (override !== null && !real) [state.el, state.az] = refPosition(override);
    else {
      if (override !== null) d.setHours(Math.floor(override / 60), override % 60, 0, 0);
      const [lat, lon] = wherePlace(params);
      [state.el, state.az] = sunPosition(d.getTime(), lat, lon);
    }
    state.minutes = override !== null ? override : d.getHours() * 60 + d.getMinutes() + d.getSeconds() / 60;
    target = lightAt(state.el, state.az);
    state.phase = phaseAt(state.el, state.az);
  }

  // The baked and glass materials of every floor; shadow flags on the lit, opaque meshes of the active floor (its
  // furniture, the window frame, the walls, and the bots, who live under the floor's root too).
  function scan() {
    baked.clear();
    glass.clear();
    building.group.traverse((o) => {
      if (!o.isMesh) return;
      for (const m of Array.isArray(o.material) ? o.material : [o.material]) {
        if (m.userData.nightPatched) { baked.add(m); patch.relight(m); }
        if (m.name === 'glass_pane') glass.add(m);
      }
    });
    const inst = building.activeOpen();
    inst?.root.traverse((o) => {
      if (!o.isMesh) return;
      const mats = Array.isArray(o.material) ? o.material : [o.material];
      const opaque=mats.every((m) => isLit(m) && !m.transparent);
      const on = opaque && !NO_SHADOW.test(o.name) && !NO_SHADOW.test(o.parent?.name || '');
      const receiver=opaque&&o.userData.shadowReceiver===true;
      o.castShadow = on&&!receiver;
      o.receiveShadow = on||receiver;
    });
    tables = [];
    if (inst) {
      inst.root.updateMatrixWorld(true);
      for (const o of inst.obj.children) {
        if (!TABLE.test(o.name) || !o.visible) continue;
        const b = new THREE.Box3().setFromObject(o);
        tables.push({ x0: b.min.x, z0: b.min.z, x1: b.max.x, z1: b.max.z, y: b.max.y - inst.root.position.y });
      }
    }
  }

  // The sun's shadow camera: looking down the sun's direction at the active floor, fitted tightly around the room.
  function fitShadow() {
    const root = building?.activeOpen()?.root;
    const layout=building?.activeSlot?.kind==='overview'?building.overview.layout:null;
    const room=shadowVolume(layout);
    resizeShadowMap(sun.shadow,shadowMapSize(!!layout,COARSE,renderer.capabilities?.maxTextureSize));
    if (root) root.getWorldPosition(centre); else centre.set(0, 0, 0);
    const y = centre.y;
    centre.x+=room.cx;centre.z+=room.cz;
    centre.y += (room.y0 + room.y1) / 2;
    sun.target.position.copy(centre);
    sun.position.copy(centre).addScaledVector(dir, room.distance);
    const cam = sun.shadow.camera;
    cam.position.copy(sun.position);
    cam.lookAt(centre);
    cam.updateMatrixWorld();
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity, z0 = Infinity, z1 = -Infinity;
    for (let i = 0; i < 8; i++) {
      corner.set(centre.x + (i & 1 ? room.x : -room.x), y + (i & 2 ? room.y1 : room.y0), centre.z + (i & 4 ? room.z : -room.z));
      corner.applyMatrix4(cam.matrixWorldInverse);
      x0 = Math.min(x0, corner.x); x1 = Math.max(x1, corner.x);
      y0 = Math.min(y0, corner.y); y1 = Math.max(y1, corner.y);
      z0 = Math.min(z0, corner.z); z1 = Math.max(z1, corner.z);
    }
    cam.left = x0 - 0.1; cam.right = x1 + 0.1; cam.bottom = y0 - 0.1; cam.top = y1 + 0.1;
    cam.near = Math.max(0.1, -z1 - 0.5); cam.far = -z0 + 0.5;
    cam.updateProjectionMatrix();
  }

  function apply(p) {
    sun.color.setRGB(...p.sunC, THREE.SRGBColorSpace);
    sun.intensity = p.sunI;
    const el = THREE.MathUtils.degToRad(p.sunEl), az = THREE.MathUtils.degToRad(p.sunAz);
    dir.set(Math.cos(el) * Math.cos(az), Math.sin(el), Math.cos(el) * Math.sin(az));
    fitShadow();
    hemi.color.setRGB(...p.hemiSky, THREE.SRGBColorSpace);
    hemi.groundColor.setRGB(...p.hemiGnd, THREE.SRGBColorSpace);
    hemi.intensity = p.hemiI;
    fill.color.setRGB(...p.fillC, THREE.SRGBColorSpace);
    fill.intensity = p.fillI;
    scene.environmentIntensity = p.envI;
    renderer.toneMappingExposure = p.exposure;
    // G/F shares the native daylight profile too. Its warm floors and local
    // pendants supply the interior colour; global light must still follow night.
    uNight.value = p.bake;
    state.night = p.night;
    state.lampsK = p.lamps;
    state.windowsK = p.windows;
    for (const [mat, kind] of tracked) {
      if (kind === 'lamp') mat.emissiveIntensity = p.lamps * 2.4;
      else if (kind === 'windows') mat.emissiveIntensity = p.windows * 1.7;
    }
    for (const mat of baked) mat.color.setRGB(...p.tint);
    tmp.setRGB(...p.glassC, THREE.SRGBColorSpace);
    for (const mat of glass) { mat.color.copy(tmp); mat.emissive.copy(tmp).multiplyScalar(p.glassE); mat.opacity = p.glassA; }
    patch.update(dir, p.floorSunC, p.floorSun, 1 - p.bake);
  }

  function updateLamps() {
    const src = lampSource ? lampSource() : null;
    const k = src ? src.k * state.lampsK : 0;
    // The expanded lobby is physically lit and has no baked pendant pools.
    // Its three owned area pendants follow the exact native night transition.
    building?.overview?.setLobbyLightLevel?.(building?.activeSlot?.kind==='overview'?k:0);
    for (let i = 0; i < LAMP_COUNT; i++) {
      const l = lamps[i];
      const pos = src && src.positions[i];
      // lights stay in the scene (intensity 0 by day) so the lit materials never recompile
      if (pos) { l.position.copy(pos); l.target.position.set(pos.x, pos.y - 3, pos.z); l.intensity = LAMP_INTENSITY * k; } else l.intensity = 0;
    }
    const y = building?.activeOpen()?.root.position.y ?? 0;
    lampLight.set(LAMP_COLOR).multiplyScalar(LAMP_POOL * k);
    patch.lamps(k > 0.001 ? src.positions : null, lampLight, LAMP_ANGLE, LAMP_PENUMBRA, tables.map((t) => ({ ...t, y: t.y + y })));
  }

  function update(dt, nowMs) {
    if (nowMs - lastPlan > PLAN_MS) { lastPlan = nowMs; plan(); }
    if (building && nowMs - lastScan > SCAN_MS) { lastScan = nowMs; scan(); }
    if (!cur) cur = target;
    else {
      const k = 1 - Math.exp(-dt / 0.45);        // never jump: follow the clock with a soft lag
      cur = mixParams(cur, target, k);
    }
    state.params = cur;
    apply(cur);
    updateLamps();

    if (nowMs - lastHtml > 1000) {
      lastHtml = nowMs;
      const root = document.documentElement;
      if (root.dataset.daylight !== state.phase) root.dataset.daylight = state.phase;
      root.style.setProperty('--night', cur.night.toFixed(3));
    }
  }

  const api = {
    uNight, state, update, setTime,
    track(mat, kind) { tracked.set(mat, kind); },
    untrack(mat) { tracked.delete(mat); },
    setLampSource(fn) { lampSource = fn; },
    setBuilding(b) { building = b; lastScan = -1e9; },
    get shadowOpacity() { return cur ? cur.shadow : 0.3; },
    get night() { return state.night; },
  };
  window.bothq = Object.assign(window.bothq || {}, { setTime, daylight: state });
  return api;
}
