// Isometric 3D building (CONTRACT §6, v2): renderer, camera on springs, floor stack, actors, picking, post.
// Public API (unchanged): initScene, renderScene, zoom, focusBot.
import * as THREE from 'three';
import { CSS2DRenderer, CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { loadOfficeAssets } from './office.js';
import { Building } from './building.js';
import { createDaylight } from './daylight.js';
import { createPost } from './post.js';
import { studioCss } from './studio.js';
import { Actor } from './actor.js';
import { generateBlob } from './blobgen.js';
import { createPlatformModel } from './platform-models.js';
import { attachPlatformModel } from './platform-controller.js';
import { Spring, clamp } from './spring.js';
import { sofiaHour, fontsPromise } from './walls.js';
import { isGround } from '../overview.js';
import {requiredMainSeats,resolveSeats,nearestDesk} from '../seating.js';
import {isObservedChild,memberBots,followerSlots,canManageAgent} from '../subagent-view.js';
import {layoutHeadLabels,headOpenAction} from './dialogue-layout.js';

// Relative to the page (not the site root), so the office also works when it is published in a sub-folder, e.g.
// https://bodila51.github.io/grokbot-wallpaper/office/ -> ../assets. Locally the page is at `/`, where this is /assets.
const ASSETS = '../assets';
const ISO_DIR = new THREE.Vector3(1, 1, 1).normalize();
const HOME = { x: -0.4, y: -0.3, z: 0.3 };             // camera target relative to the active floor's surface
const VIEW_SPAN = { w: 35, h: 22 };                 // meters visible at zoom 1 (the stack + the white air around it)
const PHONE_SPAN_W = 29.5;                          // m across a phone's scene box: the floor + shaft fill ~90 % of the screen
const ZOOM = { min: 0.75, max: 3, focus: 2 };
const UI_LEFT = 230, UI_RIGHT = 360;                   // glass panels the building must not hide behind
const SIN_E = Math.sin(Math.atan(1 / Math.SQRT2));      // sin(35.264°) = ground foreshortening
// Server desk numbers fill 1..20 in arrival order; seat them on the far side of the tables first (odd desks face
// the camera), neighbours alternating so tags never collide.
const DESK_ORDER = [1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20];

let renderer, labels, camera, scene, container, callbacks, daylight, building, post;
let assets = null;
let lastState = null, lastView = { currentFloor: null, selectedBot: null };
const actors = new Map();
const retiring = new Set();
const pickables = new Set();
const models = new Map();            // avatar id -> Promise<Object3D|null>
const effects = [];
const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
const cursor = { active: false, x: 0, z: 0 };
const groundPlane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
const tmp = new THREE.Vector3();
const clock = new THREE.Clock();
const cam = {
  x: new Spring(HOME.x, 0.45), y: new Spring(HOME.y, 0.55), z: new Spring(HOME.z, 0.45), zoom: new Spring(0, 0.4),   // zoom is ln(zoom)
};
let hoveredId = null, pointerDirty = false, drag = null;
const touches = new Map();
let pinch = null;
let introDone = false, switchedAt = 0, nowSec = 0, settledOnce = false;
let loadingEl = null;
let sizeSignature='';
let groundSeats=new Map(),seatTargets=null,seatSaving=false,seatNotice=null,seatNoticeTimer=null;
let followerPositions=new Map();
const childLinks=new Map();
// Debug/test switches: ?floor=<id> forces the active floor, ?from=<id> starts there first, ?intro=0 skips the load
// animation, ?hold=<s> fast-forwards that long after the (last) switch and freezes time, ?cam=x,z,zoom snaps the camera.
// Wallpaper mode (?mode=wallpaper, for Lively): no panel offset, hover only (no pan / zoom / select), <= 30 fps, no
// in-scene tags unless &tags=1. app.js owns ?floor= there (pin / cycle), so the scene's own debug switch is off.
const WALLPAPER = new URLSearchParams(location.search).get('mode') === 'wallpaper';
const WALLPAPER_TAGS = new URLSearchParams(location.search).get('tags') === '1';
const WALLPAPER_FRAME_MS = 1000 / 30;
const WALLPAPER_FRAMING = 0.9;                        // < 1 = closer: the side panels' space is not needed, so the tower fills more
const TEST = (() => {
  const q = new URLSearchParams(location.search);
  return { floor: WALLPAPER ? null : q.get('floor'), from: q.get('from'), intro: q.get('intro'), hold: q.has('hold') ? Number(q.get('hold')) : null, cam: q.get('cam'), cursor: q.get('cursor') };
})();
let frozen = false, pinCursor = false;

// The office GLB is the biggest download: start it the moment this module runs (index.html may have preloaded it
// already; the loader then reuses that response), not after the renderer and the UI are set up.
const officeAssets = loadOfficeAssets(`${ASSETS}/office`);

export function initScene(el, cbs = {}) {
  container = el;
  callbacks = cbs;
  container.classList.add('hq-scene');
  ensureAssets();

  // Loader: index.html may already carry it (visible from the first paint). It stays until the first floor drops in,
  // so nobody sees a half-built office (the elevator shaft alone) while the bots are still on their way.
  loadingEl = container.querySelector('.hq-loading');
  if (!loadingEl) {
    loadingEl = document.createElement('div');
    loadingEl.className = 'hq-loading';
    loadingEl.setAttribute('role', 'status');
    loadingEl.innerHTML = '<span class="hq-loading-label">Building the office</span><span class="hq-loading-track"><i></i></span>';
    container.append(loadingEl);
  }

  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' });
  // the post chain (MSAA, half-float) is capped at 1.5x: soft by design, and it keeps VRAM and fill cost sane
  renderer.setPixelRatio(Math.min(devicePixelRatio, 1.5));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.setClearColor(0x000000, 0);
  renderer.domElement.className = 'hq-canvas';
  container.append(renderer.domElement);

  labels = new CSS2DRenderer();
  labels.domElement.className = 'hq-labels';
  container.append(labels.domElement);

  scene = new THREE.Scene();
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  scene.environmentIntensity = 0.55;

  daylight = createDaylight({ scene, renderer });
  document.documentElement.dataset.sky = 'studio';        // white backdrop at every hour: the UI stays on light glass (web/styles.css)
  container.style.background = studioCss();               // the same backdrop as the post pass draws, for the first moments

  camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 240);
  camera.position.copy(ISO_DIR).multiplyScalar(80);
  camera.lookAt(0, 0, 0);

  officeAssets.then((a) => {
    assets = a;
    building = new Building({ scene, daylight, assets: a, baseUrl: `${ASSETS}/office`, renderer });
    daylight.setBuilding(building);                        // sun stripes on the active floor, ambient tint of the baked surfaces
    a.spots.lounges.forEach((s) => { if (s) s.lounge = true; });
    window.bothq = Object.assign(window.bothq || {}, { source: a.source });
    resize();
    const go = () => { if (lastState) sync(); };
    Promise.race([fontsPromise, new Promise((r) => setTimeout(r, 1500))]).then(go, go);
  });

  post = createPost(renderer, scene, camera);

  addPointerHandlers();
  seatNotice=document.createElement('div');seatNotice.className='office-seat-notice';seatNotice.setAttribute('role','status');seatNotice.hidden=true;container.append(seatNotice);
  addEventListener('office:agent-drag-start',event=>{if(isGround(building?.activeId)&&actors.has(event.detail?.id))onDown(event.detail.event,event.detail.id);});
  addEventListener('keydown',event=>{if(event.key==='Escape'&&drag?.actorId){cancelSeatDrag();drag=null;}});
  new ResizeObserver(resize).observe(container);
  resize();
  renderer.setAnimationLoop(frame);

  window.bothq = Object.assign(window.bothq || {}, {
    scene: { get camera() { return camera; }, get building() { return building; }, cam, actors, get daylight() { return daylight; }, switchFloor: (id) => building && switchFloor(id, false), settle },
  });
}

// The page may not load Inter / scene.css itself (older index.html): make sure both are there.
function ensureAssets() {
  if (!document.querySelector('link[href*="scene.css"]')) {
    const l = document.createElement('link');
    l.rel = 'stylesheet';
    l.href = new URL('./scene.css', import.meta.url).href;
    document.head.append(l);
  }
  if (!document.querySelector('link[href*="family=Inter"]')) {
    const l = document.createElement('link');
    l.rel = 'stylesheet';
    l.href = 'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;650;700&display=swap';
    document.head.append(l);
  }
}

export function renderScene(state, view) {
  lastState = state;
  lastView = { ...view };
  if (building) sync();
}

function sync() {
  const state = lastState, view = lastView;
  const floors = [...state.floors].map(floor=>isGround(floor.id)?{...floor,seat_count:requiredMainSeats(Object.values(state.bots))}:floor).sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
  building.setFloors(floors);
  let want = view.currentFloor && floors.some((f) => f.id === view.currentFloor) ? view.currentFloor : (floors[0]?.id ?? '_');
  if (TEST.floor && floors.some((f) => f.id === TEST.floor)) want = TEST.from && !introDone && floors.some((f) => f.id === TEST.from) ? TEST.from : TEST.floor;
  if (building.activeId !== want) switchFloor(want, !introDone);
  introDone = true;

  const inst = building.activeOpen();
  if (!inst) return;
  const overview=isGround(building.activeId);
  const floorBots = overview?Object.values(state.bots).sort((a,b)=>a.id.localeCompare(b.id)).map(b=>({...b,overview:true})):Object.values(state.bots).filter((b) => b.floor === building.activeId);
  resize();

  const maxShare = Math.max(0.0001, ...floorBots.map((b) => b.usage_share || 0));
  const members=memberBots(floorBots);
  const lounge = overview?new Map():assignLounge(members);
  groundSeats=overview?resolveSeats(members,inst.spots):new Map();
  const rootSpots=new Map(),spots=new Map();
  for(const bot of members){
    const deskIdx=overview?inst.spots.indexOf(groundSeats.get(bot.id)):(DESK_ORDER[(bot.desk || 1)-1] || 1)-1;
    const desk=overview?inst.spots[deskIdx]:assets.spots.desks[deskIdx] || assets.spots.desks[0];
    const spot=lounge.get(bot.id)||desk;
    if(spot){spots.set(bot.id,spot);rootSpots.set(bot.agent_id,spot);}
  }
  const children=floorBots.filter(isObservedChild);
  followerPositions=followerSlots(children,rootSpots,panBounds(),followerPositions,overview?inst.spots:assets.spots.desks,view.selectedBot);
  for(const bot of children){const at=followerPositions.get(bot.id);if(at)spots.set(bot.id,{pos:new THREE.Vector3(at.x,at.y,at.z),yaw:at.yaw,lounge:true,subagent:true});}
  const ids=new Set(spots.keys());
  for(const [id,actor] of actors)if(!ids.has(id)){actor.retire();retiring.add(actor);actors.delete(id);}
  for(const [id,line] of childLinks)if(!ids.has(id)||!spots.has(id)){line.removeFromParent();line.geometry.dispose();line.material.dispose();childLinks.delete(id);}
  const flags = new Array(overview?inst.spots.length:20).fill(0);
  const delay = Math.max(0, switchedAt + 0.55 - nowSec);
  for (const [index,bot] of floorBots.entries()) {
    const spot=spots.get(bot.id);
    if(!spot)continue;
    let actor = actors.get(bot.id);
    if (!actor) {
      actor = new Actor(bot, { parent: inst.root, inst, pickables, confetti, cursor, onSelect: callbacks.onSelectBot,
        onOpenDialogue: id => {
          callbacks.onSelectBot?.(id);
          const reader=actors.get(id)?.dialogue,action=headOpenAction(lastState?.bots[id],!!reader);
          if(action.mode==='head'){reader.setOpen(true);reader.peek.focus({preventScroll:true});}
          else if(action.mode==='history')location.href=action.url;
        }, dropDelay: delay });
      actors.set(bot.id, actor);
      attachModel(actor, bot);
    }
    actor.root.scale.setScalar(isObservedChild(bot)?0.72:1);
    if(isObservedChild(bot)&&!childLinks.has(bot.id)){
      const geometry=new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(),new THREE.Vector3()]);
      const line=new THREE.Line(geometry,new THREE.LineBasicMaterial({color:0x84a7ba,transparent:true,opacity:0.38,depthWrite:false}));
      inst.root.add(line);childLinks.set(bot.id,line);
    }
    const deskIdx = overview?inst.spots.indexOf(groundSeats.get(bot.id)):(DESK_ORDER[(bot.desk || 1) - 1] || 1) - 1;
    if (!isObservedChild(bot)&&(bot.effective_status || bot.status) === 'working' && !lounge.has(bot.id)&&deskIdx>=0) flags[deskIdx] = 1;
    actor.update(bot, spot, { maxShare, selectedBot: view.selectedBot });
    actor.dialogue.setChildren(children.filter(child=>bot.is_subagent?child.parent_agent_id===bot.agent_id:child.root_agent_id===bot.agent_id));
  }
  positionFollowers();
  building.setScreens(flags);
  const floor = floors.find((f) => f.id === building.activeId);
  building.setDisplayData({ floorName: floor ? floor.name : '', bots: floorBots, hour: sofiaHour(state.now) });
  if (hoveredId && !actors.has(hoveredId)) hoveredId = null;
  if (!settledOnce) {
    settledOnce = true;
    if (TEST.intro === '0') settle(3.5);
    if (TEST.from && TEST.floor) sync();                 // second pass: ride from `from` to `floor`
    if (TEST.hold !== null) { settle(TEST.hold); frozen = true; }
    if (TEST.cursor) {                                   // debug: ?cursor=x,z pins the cursor ground point
      const [x, z] = TEST.cursor.split(',').map(Number);
      cursor.active = true; cursor.x = x; cursor.z = z; pinCursor = true;
      settle(1.2);
    }
    if (TEST.cam) {
      const [x, z, zm] = TEST.cam.split(',').map(Number);
      cam.x.snap(x); cam.z.snap(z); cam.zoom.snap(Math.log(zm || 1));
    }
  }
}

// Follow the root's live position during travel and dragging, without assigning
// a seat to the child or moving its actual native workspace.
function positionFollowers(){
  const byAgent=new Map([...actors.values()].map(actor=>[actor.bot?.agent_id,actor]));
  for(const actor of actors.values())if(isObservedChild(actor.bot)){
    const root=byAgent.get(actor.bot.root_agent_id),offset=followerPositions.get(actor.id);
    if(!root||!offset)continue;
    actor.root.position.x=root.root.position.x+offset.dx;
    actor.root.position.z=root.root.position.z+offset.dz;
    const line=childLinks.get(actor.id);
    if(line){
      const parent=byAgent.get(actor.bot.parent_agent_id)||root,coordinates=line.geometry.attributes.position;
      coordinates.setXYZ(0,parent.root.position.x,0.08,parent.root.position.z);
      coordinates.setXYZ(1,actor.root.position.x,0.08,actor.root.position.z);
      coordinates.needsUpdate=true;line.geometry.computeBoundingSphere();
      line.visible=(actor.hostAlpha??1)>0.5&&!actor.retiring;
    }
  }
}

// Fast-forward all springs/animations (used by ?intro=0 and window.bothq.settle() for tests and screenshots).
function settle(seconds = 3) {
  const step = 1 / 30;
  for (let t = 0; t < seconds; t += step) {
    nowSec += step;
    stepCamera(step);
    building.update(step, performance.now());
    for (const a of actors.values()) { a.hostAlpha = building.activeOpen()?.alpha ?? 1; a.tick(step, nowSec); }
    positionFollowers();
    for (const a of retiring) { a.tick(step, nowSec); if (a.dead) retiring.delete(a); }
  }
}

// Idle bots go to the lounge while seats last; the rest nap at their desks.
function assignLounge(bots) {
  const map = new Map();
  const seats = assets.spots.lounges.filter(Boolean);
  const idle = bots.filter((b) => (b.effective_status || b.status) === 'idle').sort((a, b) => (a.desk || 0) - (b.desk || 0));
  idle.slice(0, seats.length).forEach((b, i) => map.set(b.id, seats[i]));
  return map;
}

// Registered platforms get a dedicated Office model; other providers retain
// their own GLB / generated look. Model selection is independent of work status.
function attachModel(actor,bot){
  attachPlatformModel(actor,bot,{createPlatformModel,loadModel,generateBlob});
}

function loadModel(id) {
  if (!models.has(id)) {
    models.set(id, new GLTFLoader().loadAsync(`${ASSETS}/blobs/${encodeURIComponent(id)}.glb`)
      .then((g) => g.scene)
      .catch(() => null));
  }
  return models.get(id);
}

// ---------- floor switch = elevator ride ----------

function switchFloor(id, first) {
  cancelSeatDrag();drag=null;
  for (const a of actors.values()) { a.retire(); retiring.add(a); }
  actors.clear();
  for(const line of childLinks.values()){line.removeFromParent();line.geometry.dispose();line.material.dispose();}childLinks.clear();followerPositions.clear();
  hoveredId = null;
  building.setActive(id, { intro: first });
  const y = building.activeY + HOME.y;
  const home=homePosition();cam.x.to(home.x); cam.z.to(home.z); cam.y.to(y); cam.zoom.to(0);
  if (first) {
    cam.x.snap(home.x); cam.z.snap(home.z);
    cam.y.snap(y - 2.6); cam.zoom.snap(Math.log(0.86));
    cam.y.to(y, 0.1); cam.zoom.to(0, 0.1);
  }
  switchedAt = nowSec;
  if (first) { building.prewarm(camera); hideLoader(); }
}

function hideLoader() {
  const el = loadingEl;
  if (!el) return;
  loadingEl = null;
  el.classList.add('is-done');
  setTimeout(() => el.remove(), 450);
}

// ---------- camera (all on springs) ----------

export function zoom(dir) {
  if (!building) return;
  if (dir === 'reset') {
    const home=homePosition();cam.x.to(home.x); cam.z.to(home.z); cam.y.to(building.activeY + HOME.y); cam.zoom.to(0);
    return;
  }
  const z = clamp(Math.exp(cam.zoom.target) * (dir === 'in' ? 1.3 : 1 / 1.3), ZOOM.min, ZOOM.max);
  cam.zoom.to(Math.log(z));
}

export function focusBot(id) {
  const a = actors.get(id);
  if (!a || !building) return;
  const target=a.spot?.pos||a.root.position;
  cam.x.to(target.x);
  cam.z.to(target.z);
  cam.y.to(building.activeY + 4.0); // Leave room above the actor for its dialogue.
  cam.zoom.to(Math.log(Math.max(Math.exp(cam.zoom.target), ZOOM.focus)));
}

function stepCamera(dt) {
  clampTarget();
  cam.x.step(dt); cam.y.step(dt); cam.z.step(dt); cam.zoom.step(dt);
  const z = clamp(Math.exp(cam.zoom.x), ZOOM.min * 0.85, ZOOM.max * 1.1);
  if (Math.abs(z - camera.zoom) > 1e-5) { camera.zoom = z; camera.updateProjectionMatrix(); }
  tmp.set(cam.x.x, cam.y.x, cam.z.x);
  camera.position.copy(tmp).addScaledVector(ISO_DIR, 80);
}

function clampTarget() {
  const bounds=panBounds();
  cam.x.target = clamp(cam.x.target, bounds.minX, bounds.maxX);
  cam.z.target = clamp(cam.z.target, bounds.minZ, bounds.maxZ);
  cam.zoom.target = clamp(cam.zoom.target, Math.log(ZOOM.min), Math.log(ZOOM.max));
}

function resize() {
  if (!renderer) return;
  const w = container.clientWidth || 1, h = container.clientHeight || 1;
  const layout=isGround(building?.activeId)?building.overview.layout:null;
  const signature=`${w}:${h}:${layout?.width||0}:${layout?.depth||0}`;
  if(signature===sizeSignature)return;
  sizeSignature=signature;
  renderer.setSize(w, h, false);
  labels.setSize(w, h);
  post?.setSize(w, h);
  const aspect = w / h;
  // phones (the UI's <= 900 px layout) have no side panels to keep clear: the floor fills the width, bots read bigger
  const spanW = WALLPAPER ? VIEW_SPAN.w : VIEW_SPAN.w + (PHONE_SPAN_W - VIEW_SPAN.w) * (1 - clamp((w - 560) / 340, 0, 1));
  const wide=layout?Math.max(spanW,(layout.width+layout.depth)*.99):spanW;
  const tall=layout?Math.max(VIEW_SPAN.h,(layout.width+layout.depth)*.59):VIEW_SPAN.h;
  const halfH = Math.max(tall, wide / aspect) / 2 * (WALLPAPER ? WALLPAPER_FRAMING : 1);
  camera.left = -halfH * aspect; camera.right = halfH * aspect;
  camera.top = halfH; camera.bottom = -halfH;
  // keep the building clear of the glass panels: shift the view so its centre is the free space between them
  const shift = WALLPAPER ? 0 : clamp((w - 900) / 600, 0, 1) * (UI_RIGHT - UI_LEFT) / 2;   // wallpaper: no panels, centred
  if (shift > 0.5) camera.setViewOffset(w, h, shift, 0, w, h); else camera.clearViewOffset();
  camera.updateProjectionMatrix();
}

const pxPerMeter = () => renderer.domElement.clientHeight / ((camera.top - camera.bottom) / camera.zoom);
const panBounds = ()=>isGround(building?.activeId)?building.overview.layout:{minX:-9.5,maxX:9.5,minZ:-7.5,maxZ:7.5};
const homePosition=()=>isGround(building?.activeId)?{x:building.overview.layout.center.x,z:building.overview.layout.center.z}:HOME;

// ---------- pointer: pan, zoom, hover, click ----------

function addPointerHandlers() {
  const el = renderer.domElement;
  el.addEventListener('pointermove', onMove);
  el.addEventListener('pointerleave', () => { cursor.active = false; setHover(null); });
  if (WALLPAPER) return;                              // a desktop background: bots follow the cursor, nothing else
  el.addEventListener('pointerdown', onDown);
  el.addEventListener('pointerup', onUp);
  el.addEventListener('pointercancel', (e) => { touches.delete(e.pointerId); cancelSeatDrag();drag = null; pinch = null; });
  el.addEventListener('wheel', onWheel, { passive: false });
  el.addEventListener('contextmenu', (e) => e.preventDefault());
}

function setPointer(e) {
  const r = renderer.domElement.getBoundingClientRect();
  pointer.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
}

function seatMessage(text,error=false,hold=0){
  if(!seatNotice)return;clearTimeout(seatNoticeTimer);seatNotice.textContent=text;seatNotice.hidden=!text;seatNotice.dataset.error=String(error);
  if(hold)seatNoticeTimer=setTimeout(()=>{seatNotice.hidden=true;},hold);
}

function seatPoint(event){
  if(!building||!isGround(building.activeId))return null;
  setPointer(event);raycaster.setFromCamera(pointer,camera);
  const plane=new THREE.Plane(new THREE.Vector3(0,1,0),-building.activeY-.8),point=new THREE.Vector3();
  if(!raycaster.ray.intersectPlane(plane,point))return null;
  point.y-=building.activeY;return point;
}

function occupant(deskId,movingId){
  for(const [id,seat] of groundSeats)if(id!==movingId&&seat.id===deskId)return id;
  return null;
}

function pickDesk(event){
  if(!seatTargets)return null;
  setPointer(event);raycaster.setFromCamera(pointer,camera);
  const hit=raycaster.intersectObjects(seatTargets.pickables,false)[0];
  if(hit){
    let object=hit.object;while(object&&!object.userData.officeDeskId&&!object.userData.officeSeatIds)object=object.parent;
    if(object?.userData.officeSeatIds){const point=building.activeOpen().root.worldToLocal(hit.point.clone()),ids=new Set(object.userData.officeSeatIds);return nearestDesk(point,seatTargets.spots.filter(spot=>ids.has(spot.id)).map(spot=>({...spot,x:spot.pos.x,z:spot.pos.z})),Infinity);}
    const id=object?.userData.officeDeskId;if(id)return seatTargets.spots.find(spot=>spot.id===id)||null;
  }
  const point=seatPoint(event);if(!point)return null;
  return nearestDesk(point,seatTargets.spots.map(spot=>({...spot,x:spot.pos.x,z:spot.pos.z})),1.85);
}

function showSeatTargets(movingId){
  clearSeatTargets();const inst=building?.activeOpen();if(!inst?.spots)return;
  const root=new THREE.Group();root.name='office-drop-targets';
  const geometry=new THREE.RingGeometry(.64,.78,28);geometry.rotateX(-Math.PI/2);
  const free=new THREE.MeshBasicMaterial({color:'#38ad78',transparent:true,opacity:.7,depthWrite:false,side:THREE.DoubleSide});
  const busy=new THREE.MeshBasicMaterial({color:'#d7857d',transparent:true,opacity:.55,depthWrite:false,side:THREE.DoubleSide});
  const rings=new Map(),pickables=[];
  for(const spot of inst.spots){const ring=new THREE.Mesh(geometry,occupant(spot.id,movingId)?busy:free);ring.position.set(spot.pos.x,spot.lounge?spot.pos.y+.025:.09,spot.pos.z);root.add(ring);rings.set(spot.id,ring);}
  inst.obj.traverse(object=>{if(!object.isMesh)return;let parent=object;while(parent&&!parent.userData.officeDeskId&&!parent.userData.officeSeatIds)parent=parent.parent;if(parent)pickables.push(object);});
  const label=document.createElement('span');label.className='office-drop-label';const tag=new CSS2DObject(label);tag.visible=false;root.add(tag);inst.root.add(root);
  seatTargets={root,geometry,free,busy,rings,pickables,spots:inst.spots,tag,label};
  seatMessage('拖到綠色休息位或工作台 · 紅色位置已有人');
}

function highlightSeat(spot,movingId){
  if(!seatTargets)return;
  for(const [id,ring] of seatTargets.rings)ring.scale.setScalar(spot?.id===id?1.3:1);
  seatTargets.tag.visible=!!spot;
  if(spot){const busy=!!occupant(spot.id,movingId);seatTargets.tag.position.copy(spot.pos).add(new THREE.Vector3(0,1.1,0));seatTargets.label.textContent=(spot.label||spot.id)+(busy?' · 已有人':' · 放開移到這裏');seatTargets.label.dataset.busy=String(busy);}
}

function clearSeatTargets(){
  if(!seatTargets)return;seatTargets.root.removeFromParent();seatTargets.label.remove();seatTargets.geometry.dispose();seatTargets.free.dispose();seatTargets.busy.dispose();seatTargets=null;container?.classList.remove('is-moving-agent');
}

function cancelSeatDrag(){
  if(drag?.actorId)actors.get(drag.actorId)?.setDragging(null);
  clearSeatTargets();if(!seatSaving)seatMessage('');
}

async function finishSeatMove(current,event){
  const actor=actors.get(current.actorId),seat=pickDesk(event);
  const busy=seat&&occupant(seat.id,current.actorId);clearSeatTargets();
  if(!actor||!seat||busy){actor?.setDragging(null);seatMessage(busy?'呢個位置已有人使用。':'未放到座位，已返回原位。',!!busy,2600);return;}
  seatSaving=true;actor.setDragging(seat.pos.clone().add(new THREE.Vector3(0,.25,0)));seatMessage('正在保存 '+(seat.label||seat.id)+'…');
  try{await callbacks.onMoveBot?.(current.actorId,seat.id);seatMessage('已搬到 '+(seat.label||seat.id),false,2400);}
  catch(error){seatMessage(error.message||'未能保存位置，已返回原位。',true,3200);}
  finally{actor.setDragging(null);seatSaving=false;}
}

function onDown(e,forcedId=null) {
  if(seatSaving&&isGround(building?.activeId))return;
  renderer.domElement.setPointerCapture?.(e.pointerId);
  setPointer(e);
  touches.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (touches.size === 2) {
    const [a, b] = [...touches.values()];
    pinch = { d: Math.hypot(a.x - b.x, a.y - b.y), z: cam.zoom.target };
    cancelSeatDrag();drag = null;
    return;
  }
  const id=isGround(building?.activeId)&&e.button===0?(forcedId||pick()):null;
  const actor=id&&actors.get(id),point=actor?seatPoint(e):null;
  drag = { id: e.pointerId, x0: e.clientX, y0: e.clientY, lx: e.clientX, ly: e.clientY, lt: performance.now(), panning: false, vx: 0, vz: 0,
    actorId:canManageAgent(actor?.bot)?id:null,fromHandle:!!forcedId,moving:false,offset:actor&&point?actor.root.position.clone().sub(point):new THREE.Vector3() };
}

function onMove(e) {
  setPointer(e);
  cursor.active = true;
  pointerDirty = true;
  if (touches.has(e.pointerId)) touches.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (pinch && touches.size === 2) {
    const [a, b] = [...touches.values()];
    cam.zoom.to(pinch.z + Math.log(Math.hypot(a.x - b.x, a.y - b.y) / pinch.d));
    return;
  }
  if (!drag || drag.id !== e.pointerId) return;
  if(drag.actorId){
    if(!drag.moving&&Math.hypot(e.clientX-drag.x0,e.clientY-drag.y0)>6){drag.moving=true;actors.get(drag.actorId)?.dialogue.setOpen(false);showSeatTargets(drag.actorId);container.classList.add('is-moving-agent');}
    if(drag.moving){
      const point=seatPoint(e);if(point){const at=point.clone().add(drag.offset);at.y+=.28;actors.get(drag.actorId)?.setDragging(at);}
      const seat=pickDesk(e);highlightSeat(seat,drag.actorId);
    }
    return;
  }
  if (!drag.panning && Math.hypot(e.clientX - drag.x0, e.clientY - drag.y0) > 5) drag.panning = true;
  if (!drag.panning) return;
  const now = performance.now();
  const dx = e.clientX - drag.lx, dy = e.clientY - drag.ly;
  const dtS = Math.max(0.001, (now - drag.lt) / 1000);
  drag.lx = e.clientX; drag.ly = e.clientY; drag.lt = now;
  // screen-space drag -> ground-plane move of the camera target (content follows the pointer 1:1)
  const ppm = pxPerMeter();
  const mx = -dx / ppm, mz = dy / ppm / SIN_E;
  const wx = (mx - mz) / Math.SQRT2, wz = (-mx - mz) / Math.SQRT2;
  const bounds=panBounds();
  cam.x.snap(clamp(cam.x.x + wx, bounds.minX, bounds.maxX));
  cam.z.snap(clamp(cam.z.x + wz, bounds.minZ, bounds.maxZ));
  drag.vx = drag.vx * 0.6 + (wx / dtS) * 0.4;
  drag.vz = drag.vz * 0.6 + (wz / dtS) * 0.4;
}

function onUp(e) {
  touches.delete(e.pointerId);
  if (pinch) { if (touches.size < 2) pinch = null; drag = null; return; }
  if (!drag || drag.id !== e.pointerId) return;
  const d = drag;
  drag = null;
  if(d.actorId){
    if(d.moving){finishSeatMove(d,e);return;}
    if(d.fromHandle){callbacks.onChooseDesk?.(d.actorId);return;}
  }
  if (d.panning) {                                   // let it glide a little, then settle
    const vx = clamp(d.vx, -30, 30), vz = clamp(d.vz, -30, 30);
    cam.x.v = vx; cam.z.v = vz;
    cam.x.to(cam.x.x + vx * 0.12); cam.z.to(cam.z.x + vz * 0.12);
    return;
  }
  setPointer(e);
  callbacks.onSelectBot?.(pick());
}

function onWheel(e) {
  e.preventDefault();
  setPointer(e);
  const z0 = Math.exp(cam.zoom.target);
  const z1 = clamp(z0 * Math.exp(-e.deltaY * (e.ctrlKey ? 0.012 : 0.0016)), ZOOM.min, ZOOM.max);
  if (z1 === z0) return;
  // keep the ground point under the cursor in place while zooming
  const shift = camera.view?.enabled ? (camera.view.offsetX / camera.view.fullWidth) * 2 : 0;
  const nx = pointer.x + shift, ny = pointer.y;
  const k = 1 / z0 - 1 / z1;
  const mx = nx * camera.right * k, my = ny * camera.top * k / SIN_E;
  cam.x.to(cam.x.target + (mx - my) / Math.SQRT2);
  cam.z.to(cam.z.target + (-mx - my) / Math.SQRT2);
  cam.zoom.to(Math.log(z1));
}

function pick() {
  raycaster.setFromCamera(pointer, camera);
  const hit = raycaster.intersectObjects([...pickables], false)[0];
  return hit ? hit.object.userData.actorId : null;
}

function setHover(id) {
  if (id === hoveredId) return;
  if (hoveredId) actors.get(hoveredId)?.setHovered(false);
  hoveredId = id;
  if (id) actors.get(id)?.setHovered(true);
  container.classList.toggle('is-hovering', !!id);
  callbacks.onHoverBot?.(id);
}

// ---------- effects ----------

const CONFETTI_COLORS = [0x34C759, 0x0A84FF, 0xFF9F0A, 0xFF4FA0, 0xFF6A13, 0x14B8A6];
const confettiGeo = new THREE.PlaneGeometry(0.07, 0.035);

function confetti(at) {
  const group = new THREE.Group();
  group.position.copy(at);
  const parts = [];
  for (let i = 0; i < 36; i++) {
    const m = new THREE.Mesh(confettiGeo, new THREE.MeshBasicMaterial({ color: CONFETTI_COLORS[i % CONFETTI_COLORS.length], side: THREE.DoubleSide, transparent: true }));
    const a = Math.random() * Math.PI * 2, up = 2.2 + Math.random() * 1.6, out = 0.8 + Math.random() * 1.2;
    parts.push({ m, v: new THREE.Vector3(Math.cos(a) * out, up, Math.sin(a) * out), spin: new THREE.Vector3(Math.random() * 9, Math.random() * 9, 0) });
    group.add(m);
  }
  scene.add(group);
  effects.push({ group, parts, t: 0, life: 1.6 });
}

function stepEffects(dt) {
  for (let i = effects.length - 1; i >= 0; i--) {
    const fx = effects[i];
    fx.t += dt;
    for (const p of fx.parts) {
      p.v.y -= 6.5 * dt;
      p.v.multiplyScalar(1 - 1.4 * dt);
      p.m.position.addScaledVector(p.v, dt);
      p.m.rotation.x += p.spin.x * dt; p.m.rotation.y += p.spin.y * dt;
      p.m.material.opacity = Math.max(0, 1 - fx.t / fx.life);
    }
    if (fx.t >= fx.life) {
      fx.group.removeFromParent();
      fx.parts.forEach((p) => p.m.material.dispose());
      effects.splice(i, 1);
    }
  }
}

// ---------- share image (web/share/share.js) ----------

// One still of the current floor for the share card, framed like a product shot: the floor, the stack under it and the
// elevator fitted into `box` (fractions of the frame, y down: the caption gets the rest), on the same white studio
// through the same post chain, at width x height, supersampled by `scale` when the GPU allows it.
// Nothing private gets in: the in-scene tags are DOM and are never drawn into the canvas; the wall screen and the task
// board are redrawn for this one frame without the floor's name, bot names or tasks; selection / hover rings, bots on
// their way out and confetti are hidden. The live canvas is resized, drawn, copied out and restored inside this one
// task, then the live view is drawn again before the browser paints, so nothing flickers.
// Returns a <canvas> of width x height, or null while the office is still loading.
export function captureScene({ width = 1600, height = 900, scale = 1, box = { x: 0.06, y: 0.06, w: 0.88, h: 0.76 } } = {}) {
  const inst = building?.activeOpen();
  if (!inst || !post) return null;
  const pr = renderer.getPixelRatio();
  const maxPx = Math.min(renderer.capabilities.maxTextureSize, renderer.capabilities.maxRenderbufferSize || Infinity);
  const ss = Math.max(1, Math.min(scale, maxPx / width, maxPx / height));
  const zoom0 = camera.zoom, pos0 = camera.position.clone();
  const hidden = [];
  const hide = (o) => { if (o?.visible) { o.visible = false; hidden.push(o); } };
  for (const a of actors.values()) hide(a.ring);
  for (const a of retiring) hide(a.root);
  for (const fx of effects) hide(fx.group);
  const walls = inst.walls, live = walls?.data;
  try {
    if (live) {
      const anon = { ...live, floorName: `Floor ${building.activeIndex + 1}`, bots: live.bots.map((b) => ({ ...b, name: '', task: '', step: '' })) };
      if (walls.screen) walls.drawScreen(anon);
      if (walls.board) walls.drawBoard(anon);
    }
    renderer.setPixelRatio(ss);
    renderer.setSize(width, height, false);
    post.setSize(width, height);
    frameShot(width / height, box);
    focusV.set(0, building.cabinY.x + 0.7, 0).project(camera);
    post.render(clamp(focusV.y * 0.5 + 0.5, 0, 1), 0, 1);
    const out = document.createElement('canvas');
    out.width = width; out.height = height;
    const g = out.getContext('2d');
    g.imageSmoothingQuality = 'high';
    g.drawImage(renderer.domElement, 0, 0, width, height);
    return out;
  } finally {
    hidden.forEach((o) => { o.visible = true; });
    if (live) {
      if (walls.screen) walls.drawScreen(live);
      if (walls.board) walls.drawBoard(live);
    }
    renderer.setPixelRatio(pr);
    resize();
    camera.zoom = zoom0;
    camera.position.copy(pos0);
    camera.updateProjectionMatrix();
    focusV.set(0, building.cabinY.x + 0.7, 0).project(camera);
    post.render(clamp(focusV.y * 0.5 + 0.5, 0, 1), 0, clamp(1.5 / camera.zoom, 0.25, 1));
  }
}

// The camera at its home spot over the active floor, the frustum fitted to every visible building mesh (in view space,
// so the iso silhouette is measured, not a world box) and placed so that silhouette is centred in `box`.
const shotBox = new THREE.Box3();
const shotCorner = new THREE.Vector3();
function frameShot(aspect, box) {
  camera.clearViewOffset();
  camera.zoom = 1;
  camera.position.set(HOME.x, building.activeY + HOME.y, HOME.z).addScaledVector(ISO_DIR, 80);
  camera.updateMatrixWorld(true);
  building.group.updateMatrixWorld(true);
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  const walk = (o) => {
    if (!o.visible) return;
    if (o.isMesh && o.geometry) {
      if (!o.geometry.boundingBox) o.geometry.computeBoundingBox();
      shotBox.copy(o.geometry.boundingBox);
      for (let i = 0; i < 8; i++) {
        shotCorner.set(i & 1 ? shotBox.max.x : shotBox.min.x, i & 2 ? shotBox.max.y : shotBox.min.y, i & 4 ? shotBox.max.z : shotBox.min.z)
          .applyMatrix4(o.matrixWorld).applyMatrix4(camera.matrixWorldInverse);
        x0 = Math.min(x0, shotCorner.x); x1 = Math.max(x1, shotCorner.x);
        y0 = Math.min(y0, shotCorner.y); y1 = Math.max(y1, shotCorner.y);
      }
    }
    for (const c of o.children) walk(c);
  };
  walk(building.group);
  if (!Number.isFinite(x0)) { x0 = -VIEW_SPAN.w / 2; x1 = VIEW_SPAN.w / 2; y0 = -VIEW_SPAN.h / 2; y1 = VIEW_SPAN.h / 2; }
  const fh = Math.max((y1 - y0) / box.h, (x1 - x0) / (box.w * aspect)), fw = fh * aspect;
  camera.left = (x0 + x1) / 2 - (box.x + box.w / 2) * fw;
  camera.right = camera.left + fw;
  camera.top = (y0 + y1) / 2 + (box.y + box.h / 2) * fh;
  camera.bottom = camera.top - fh;
  camera.updateProjectionMatrix();
}

// ---------- loop ----------

function updateCursorGround() {
  if (pinCursor || !cursor.active || !building) return;
  raycaster.setFromCamera(pointer, camera);
  groundPlane.constant = -building.activeY;
  if (raycaster.ray.intersectPlane(groundPlane, tmp)) { cursor.x = tmp.x; cursor.z = tmp.z; } else cursor.active = false;
}

const focusV = new THREE.Vector3();

let lastDraw = 0;
let overlayViewport=null,overlayViewportAt=-1e9;

function layoutDialogues(nowMs) {
  const width=container.clientWidth,height=container.clientHeight;
  if(!overlayViewport||overlayViewport.width!==width||overlayViewport.height!==height||nowMs-overlayViewportAt>200){
    overlayViewportAt=nowMs;
    const origin=container.getBoundingClientRect(),obstacles=[];
    let left=12,right=width-12;
    for(const selector of ['#topbar','#floors','#panel','.bhq-dock','#office-mode-toggle']){
      const el=document.querySelector(selector);if(!el||!el.getClientRects().length)continue;
      const b=el.getBoundingClientRect();if(!b.width||!b.height)continue;
      const rect={left:b.left-origin.left,right:b.right-origin.left,top:b.top-origin.top,bottom:b.bottom-origin.top};
      obstacles.push(rect);
      if(width>=900&&selector!=='#topbar'&&selector!=='#office-mode-toggle'){
        if(rect.left<width*.35)left=Math.max(left,rect.right+10);
        else if(rect.right>width*.65)right=Math.min(right,rect.left-10);
      }
    }
    if(right-left<260){left=12;right=width-12;}
    overlayViewport={width,height,left,right,obstacles};
  }
  const candidates=[];
  for(const actor of actors.values()){
    const dialogue=actor.dialogue,el=dialogue.element;
    if(!actor.speech.visible)continue;
    actor.speech.getWorldPosition(tmp).project(camera);
    const x=(tmp.x+1)*width/2,y=(1-tmp.y)*height/2;
    el.style.display='';
    el.style.setProperty('--speech-panel-width',Math.min(350,overlayViewport.right-overlayViewport.left)+'px');
    dialogue.fitViewport(y,height);
    const measureKey=`${width}|${height}|${document.documentElement.classList.contains('office-simple-mode')}|${dialogue.expanded}|${dialogue.children.length>0}`;
    if(dialogue.expanded||dialogue.measureKey!==measureKey){dialogue.measureKey=measureKey;dialogue.measure={width:el.offsetWidth||140,height:el.offsetHeight||34};}
    candidates.push({id:actor.id,x,y,width:dialogue.measure.width,height:dialogue.measure.height,
      expanded:dialogue.expanded,selected:actor.selected,hovered:actor.hovered,
      focused:el.contains(document.activeElement),child:!!actor.bot?.is_subagent});
  }
  const placement=layoutHeadLabels(candidates,overlayViewport);
  for(const actor of actors.values()){
    const at=placement.get(actor.id);if(!at)continue;
    const el=actor.dialogue.element;
    actor.speech.center.y=at.below?0:1;
    el.style.translate=`${at.dx}px ${at.dy}px`;
    el.style.setProperty('--speech-offset-x',at.dx+'px');
    el.dataset.occluded=String(!at.visible);
    el.classList.toggle('below',at.below);
  }
}

function frame(t) {
  if (document.hidden) { clock.getDelta(); return; }       // no work in a background tab
  if (WALLPAPER) {                                          // <= 30 fps: skip the in-between vsyncs; dt keeps real time
    const since = t - lastDraw;
    if (since < WALLPAPER_FRAME_MS - 2) return;
    lastDraw += Math.max(1, Math.round(since / WALLPAPER_FRAME_MS)) * WALLPAPER_FRAME_MS;  // stay on the 33 ms grid (100+ Hz displays)
    if (t - lastDraw > WALLPAPER_FRAME_MS / 2) lastDraw = t;                                // fell behind (hidden, hitch): resync, no bursts
  }
  const dt = frozen ? 0 : Math.min(clock.getDelta(), 0.05);
  if (frozen) clock.getDelta();
  nowSec = clock.elapsedTime;
  const nowMs = performance.now();

  daylight.update(dt, nowMs);
  stepCamera(dt);
  if (building) building.update(dt, nowMs);
  if (pointerDirty && !drag?.panning && !drag?.moving) { pointerDirty = false; setHover(pick()); }
  updateCursorGround();

  const hostAlpha = building?.activeOpen()?.alpha ?? 1;
  for (const a of actors.values()) { a.hostAlpha = hostAlpha; a.tick(dt, nowSec); }
  positionFollowers();
  for (const a of retiring) {
    a.hostAlpha = a.ctx.inst.alpha;
    a.tick(dt, nowSec);
    if (a.dead) retiring.delete(a);
  }
  stepEffects(dt);

  if (post) {
    focusV.set(0, (building ? building.cabinY.x : 0) + 0.7, 0).project(camera);
    post.render(clamp(focusV.y * 0.5 + 0.5, 0, 1), dt, clamp(1.5 / camera.zoom, 0.25, 1));
  } else renderer.render(scene, camera);
  if (!WALLPAPER || WALLPAPER_TAGS) {
    layoutDialogues(nowMs);
    labels.render(scene, camera);
  }
}
