// The building stack (CONTRACT v2.4/v2.5): floors sorted by order, the active floor open, floors below closed,
// floors above slid up out of the frame (opaque, never faded), roof on top, elevator shaft + cabin. The stack is
// exactly the user's floors (one per bot group) and stands on the studio ground at floor 1: nothing below it. Everything that
// moves is a critically damped spring that can be re-targeted mid-flight (floor switch = elevator ride). The
// open/closed swap of two floors happens in one frame at the peak of a short canvas blur ("whoosh"), so big floors
// never cross-fade (that was hazy and heavy: every material went through the transparent pass).
import * as THREE from 'three';
import { Spring, smoothstep } from './spring.js';
import { FLOOR_STEP, SLAB, SHAFT_POS } from './office.js';
import { WallDisplays } from './walls.js';
import { floorStyles, applyFloorStyle, styleObjects, styleNightMaps } from './floorstyle.js';
import { GroundHall } from './ground-hall.js';

const LIFT = 20;                        // how far upper floors slide up, out of the frame (m)
const SWAP_AT = 0.14;                   // s after a switch starts: blur peak, open/closed swap
const BLUR_TAIL = 0.4;                  // s for the blur to clear after the swap
const BLUR_PX = 7;
const REDUCED = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
const CAB_H = 2.2;
const NIGHT_TINT = [0.2, 0.26, 0.42];   // used when a baked mesh has no night lightmap

// Night lightmaps (assets/office/night/<mesh name>.jpg) are loaded lazily, only once --night > 0, shared by every
// floor clone and disposed again after a stretch of full day (they are big: 3072x2304 for the floor).
function loadNightTexture(url, name) {
  return new THREE.TextureLoader().loadAsync(`${url}/night/${encodeURIComponent(name)}.jpg`).then((t) => {
    t.flipY = false;                  // glTF convention, same UVs as the day lightmap
    t.colorSpace = THREE.SRGBColorSpace;
    t.anisotropy = 8;
    return t;
  }).catch(() => null);
}

// One clone of a template with its own materials (so each floor can fade and glow independently).
class Instance {
  constructor(b, template, kind) {
    this.b = b;
    this.kind = kind;
    this.root = new THREE.Group();
    this.root.name = `${kind}-instance`;
    this.obj = template.clone(true);
    this.root.add(this.obj);
    this.alpha = 1;
    this.mats = [];
    this.textures = [];
    this.screens = [];                  // open floors: [{ mat, glow, target }] by desk index
    this.walls = null;
    this.floorId = null;

    if (kind !== 'shaft' && kind !== 'cabin') this.obj.position.set(0, 0, 0);
    const own = new Map();
    const windowOffset = Math.random();
    this.obj.traverse((o) => {
      if (!o.isMesh) return;
      const list = Array.isArray(o.material) ? o.material : [o.material];
      const out = list.map((m) => {
        const perDesk = /^laptop_screen_\d\d$/.test(o.name);
        let c = perDesk ? null : own.get(m);
        if (!c) {
          c = m.clone();
          if (!perDesk) own.set(m, c);
          this.register(c, o, windowOffset);
        }
        return c;
      });
      o.material = Array.isArray(o.material) ? out : out[0];
      const mm = /^laptop_screen_(\d\d)$/.exec(o.name);
      if (mm) this.screens[Number(mm[1]) - 1] = { mat: out[0], glow: 0, target: 0 };
    });

    if (kind === 'open') {
      const ws = this.obj.getObjectByName('wall_screen'), tb = this.obj.getObjectByName('task_board');
      if (ws || tb) this.walls = new WallDisplays(ws, tb);
    }
  }

  register(c, mesh, windowOffset) {
    this.mats.push(c);
    const bn = mesh.userData.bakedName;
    if (bn && c.map && !c.userData.nightPatched) this.b.patchBaked(c, bn);
    if (c.name === 'night_lamp') {
      if (c.emissive.getHex() === 0) c.emissive.set(0xFFC070);
      c.emissiveIntensity = 0;
      this.b.daylight.track(c, 'lamp');
    } else if (c.name === 'night_windows') {
      if (c.emissive.getHex() === 0) c.emissive.set(0xFFFFFF);
      c.emissiveIntensity = 0;
      if (c.emissiveMap) {                 // every floor gets its own random pattern of lit panes
        const e = c.emissiveMap.clone(), d = c.map ? c.map.clone() : null;
        for (const t of [e, d]) if (t) { t.wrapS = t.wrapT = THREE.RepeatWrapping; t.offset.x = windowOffset; t.needsUpdate = true; this.textures.push(t); }
        c.emissiveMap = e; if (d) c.map = d;
      }
      this.b.daylight.track(c, 'windows');
    } else if (c.name === 'night_screen') {
      if (c.emissive.getHex() === 0) c.emissive.set(0x9FD0FF);
      c.emissiveIntensity = 0;
    }
  }

  setShown(v) {
    this.alpha = v ? 1 : 0;
    this.root.visible = v;
  }

  dispose() {
    for (const m of this.mats) { this.b.daylight.untrack(m); m.dispose(); }
    for (const t of this.textures) t.dispose();
    this.root.removeFromParent();
  }
}

export class Building {
  constructor({ scene, daylight, assets, baseUrl, renderer }) {
    this.scene = scene;
    this.daylight = daylight;
    this.assets = assets;
    this.baseUrl = baseUrl;
    this.renderer = renderer;
    this.group = new THREE.Group();
    this.group.name = 'building';
    this.group.visible = false;         // nothing (not even the shaft) until the first floor drops in: scene.js shows a loader
    scene.add(this.group);
    this.introDrop = false;             // load animation: shaft + cabin ride down with the active floor

    this.slots = [];                    // sorted by floor order
    this.byId = new Map();
    this.styles = new Map();            // floor id -> { accent, layout } (floorstyle.js)
    this.activeIndex = -1;
    this.pool = [];                     // open-floor instances (max 2)
    this.sig = '';
    this.floorData = null;
    this.overview = new GroundHall(assets);
    this.group.add(this.overview.root);
    this.nightRecs = new Map();

    this.roof = new Instance(this, assets.templates.roof, 'roof');
    this.roofLift = new Spring(1, 0.5);
    this.group.add(this.roof.root);

    this.shaft = new Instance(this, assets.templates.elevator_shaft, 'shaft');
    this.cabin = new Instance(this, assets.templates.elevator_cabin, 'cabin');
    this.group.add(this.shaft.root, this.cabin.root);
    this.setupElevatorGeometry();
    this.cabinY = new Spring(0, 0.55);
    this.shaftTop = new Spring(1, 0.55);

    this.lampPositions = Array.from({ length: 6 }, () => new THREE.Vector3());
    daylight.setLampSource(() => {
      const inst = this.activeOpen();
      if (!inst) return null;
      const interior=this.activeSlot?.kind==='overview';
      const lamps = interior?(inst.lampPositions||[]):this.assets.spots.lamps;
      for (let i = 0; i < 6; i++) {
        const p = lamps[i];
        if(interior&&p){this.lampPositions[i].copy(p);inst.root.localToWorld(this.lampPositions[i]);}
        else this.lampPositions[i].set(p ? p.x : 0, p ? p.y + inst.root.position.y : -99, p ? p.z : 0);
      }
      return { positions: lamps.length ? this.lampPositions : [], k: inst.alpha,interior };
    });
    this.screenTargets = new Array(20).fill(0);

    for (let i = 0; i < 2; i++) {        // both open-floor instances up front: nothing is cloned during a switch
      const inst = new Instance(this, assets.templates.floor_open, 'open');
      inst.setShown(false);
      this.group.add(inst.root);
      this.pool.push(inst);
    }
    this.pulseT = -1;                   // time since the current switch started (s), -1 = idle
    this.swapped = true;
    this.blurPx = 0;
  }

  // The shaft is authored 1 m tall (origin at its bottom or centre) and the cabin has its origin at its floor:
  // normalise both so the bottom sits at y = 0 and the plan centre at the shaft position.
  setupElevatorGeometry() {
    this.shaftH = 1;
    for (const inst of [this.shaft, this.cabin]) {
      inst.root.updateMatrixWorld(true);
      const box = new THREE.Box3().setFromObject(inst.obj);
      if (!Number.isFinite(box.min.y)) continue;
      const c = box.getCenter(new THREE.Vector3());
      inst.obj.position.y -= box.min.y;
      if (Math.abs(c.x - SHAFT_POS.x) > 1.5 || Math.abs(c.z - SHAFT_POS.y) > 1.5) { inst.obj.position.x += SHAFT_POS.x - c.x; inst.obj.position.z += SHAFT_POS.y - c.z; }
      if (inst === this.shaft) this.shaftH = Math.max(0.01, box.max.y - box.min.y);
    }
  }

  // Night crossfade of a baked (unlit) material: mix(dayMap, nightMap, uNight). Missing/not yet loaded night map ->
  // tinted day map. All materials baked from the same mesh share one record (uniforms + the lazily loaded texture).
  patchBaked(mat, name) {
    mat.userData.nightPatched = true;
    let rec = this.nightRecs.get(name);
    if (!rec) {
      const override = this.assets.nightMaps.get(name);
      const drawn = name.startsWith('style_');        // floorstyle.js patch / rugs: no night lightmap on disk, tinted day map
      rec = { name, uMap: { value: override || mat.map }, uTint: { value: new THREE.Vector3(...(override ? [1, 1, 1] : NIGHT_TINT)) }, dayMap: mat.map, tex: null, state: override || drawn ? 'static' : 'idle', since: 0 };
      this.nightRecs.set(name, rec);
    }
    const { uMap, uTint } = rec;
    // A floor's accent rug (floorstyle.js) carries its colour in its own uniform: daylight.js owns .color (ambient tint).
    const accent = mat.userData.accent ? (mat.accentUniform = { value: new THREE.Color(1, 1, 1) }) : null;
    mat.onBeforeCompile = (shader) => {
      shader.uniforms.uNight = this.daylight.uNight;
      shader.uniforms.uNightMap = uMap;
      shader.uniforms.uNightTint = uTint;
      if (accent) shader.uniforms.uAccent = accent;
      shader.fragmentShader = shader.fragmentShader
        .replace('#include <common>', `#include <common>\nuniform float uNight;\nuniform sampler2D uNightMap;\nuniform vec3 uNightTint;${accent ? '\nuniform vec3 uAccent;' : ''}`)
        .replace('#include <map_fragment>', `#ifdef USE_MAP
  vec4 dayC = texture2D( map, vMapUv );
  vec4 nightC = texture2D( uNightMap, vMapUv );
  nightC.rgb *= uNightTint;
  diffuseColor *= mix( dayC, nightC, uNight );
#endif`);
      // after color_fragment, so light that later patches add in front of it (sunpatch.js) is tinted too
      if (accent) shader.fragmentShader = shader.fragmentShader.replace('#include <color_fragment>', '#include <color_fragment>\n  diffuseColor.rgb *= uAccent;');
    };
    mat.customProgramCacheKey = () => (accent ? 'bothq-baked-night-accent' : 'bothq-baked-night');
  }

  updateNightMaps(nowMs) {
    const night = this.daylight.night;
    for (const rec of this.nightRecs.values()) {
      if (rec.state === 'static') continue;
      if (night > 0.01) {
        rec.since = nowMs;
        if (rec.state === 'idle') {
          rec.state = 'loading';
          loadNightTexture(this.baseUrl, rec.name).then((t) => {
            if (!t) { rec.state = 'missing'; return; }
            if (rec.dayMap) { t.wrapS = rec.dayMap.wrapS; t.wrapT = rec.dayMap.wrapT; t.channel = rec.dayMap.channel; }
            rec.tex = t; rec.uMap.value = t; rec.uTint.value.set(1, 1, 1); rec.state = 'ready';
            if (rec.name === 'baked_floor') this.styleNight(t.image);
          });
        }
      } else if (rec.state === 'ready' && nowMs - rec.since > 30000) {       // 30 s of full day: free the GPU memory
        rec.uMap.value = rec.dayMap; rec.uTint.value.set(...NIGHT_TINT);
        rec.tex.dispose(); rec.tex = null; rec.state = 'idle';
        if (rec.name === 'baked_floor') this.styleNight(null);
      }
    }
  }

  // The lounge corners' floor patch and rugs (floorstyle.js) follow the night floor's light: built from its lightmap
  // when that loads, dropped again with it.
  styleNight(img) {
    const maps = img && this.assets.style ? styleNightMaps(img) : new Map();
    for (const rec of this.nightRecs.values()) {
      if (!rec.name.startsWith('style_')) continue;
      if (rec.styleNight) { rec.styleNight.dispose(); rec.styleNight = null; }
      const t = maps.get(rec.name);
      if (t) { rec.styleNight = t; rec.uMap.value = t; rec.uTint.value.set(1, 1, 1); } else { rec.uMap.value = rec.dayMap; rec.uTint.value.set(...NIGHT_TINT); }
    }
  }

  // ---------- floors ----------

  setFloors(floors) {
    const list = floors.length ? floors : [{ id: '_', name: '', order: 1 }];
    const overviewFloor=list.find(f=>f.kind==='overview');
    this.overview.configure(overviewFloor?.seat_count??overviewFloor?.bots.length??0);
    const sig = list.map((f) => f.id).join('|');
    list.forEach((f, i) => { const s = this.byId.get(f.id); if (s) { s.name = f.name; s.index = i; } });
    if (sig === this.sig) return;
    this.sig = sig;
    this.styles = this.assets.style ? floorStyles(list) : new Map();
    const keep = new Map(list.map((f, i) => [f.id, i]));
    for (const s of this.slots) if (!keep.has(s.id)) this.disposeSlot(s);
    this.slots = list.map((f, i) => {
      let s = this.byId.get(f.id);
      if (!s) {
        const closed=f.kind==='overview'?{root:this.overview.closed,setShown(v){this.root.visible=v;},dispose(){this.root.removeFromParent();}}:new Instance(this,this.assets.templates.floor_closed,'closed');
        s = { id: f.id, name: f.name, kind:f.kind, index: i, closed, open: null, lift: new Spring(1, 0.5), showOpen: false };
        this.group.add(s.closed.root);
        s.closed.setShown(false);
        this.byId.set(f.id, s);
      }
      s.index = i;
      return s;
    });
    for (const [id] of [...this.byId]) if (!keep.has(id)) this.byId.delete(id);
    this.activeIndex = Math.min(this.activeIndex, this.slots.length - 1);
    if (this.activeSlot) this.applyStyle(this.activeSlot);   // a floor added before it may move its corner / colour
    this.retarget(false);
  }

  // The floor's accent + lounge corner on its open instance. For the active floor the lounge seats follow the corner:
  // spots that move are new objects, so a seated bot walks over to its new seat (scene.js reads assets.spots.lounges).
  applyStyle(slot) {
    if(slot.kind==='overview')return;
    const st = this.styles.get(slot.id);
    if (!st || !slot.open) return;
    applyFloorStyle(slot.open.obj, st);
    slot.open.walls?.setAccent(st.accent);
    if (slot !== this.activeSlot) return;
    const lounges = this.assets.spots.lounges;
    this.assets.style.layouts[st.layout].forEach((s, i) => {
      const cur = lounges[i];
      if (!cur || cur.pos.distanceToSquared(s.pos) > 1e-6 || cur.yaw !== s.yaw) lounges[i] = { pos: s.pos.clone(), yaw: s.yaw, lounge: true };
    });
  }

  disposeSlot(s) {
    s.closed.dispose();
    if(s.open===this.overview){this.overview.setShown(false);return;}
    if (s.open) { this.pool = this.pool.filter((p) => p !== s.open); s.open.dispose(); }
  }

  get floorCount() { return this.slots.length; }
  get activeSlot() { return this.slots[this.activeIndex] || null; }
  get activeId() { return this.activeSlot?.id ?? null; }
  get activeY() { return Math.max(0, this.activeIndex) * FLOOR_STEP; }
  activeOpen() { return this.activeSlot?.open || null; }

  // Make a floor the active (open) one. intro: start fully assembled-from-nothing (load animation).
  setActive(id, { intro = false } = {}) {
    const idx = this.slots.findIndex((s) => s.id === id);
    if (idx < 0) return false;
    const moved = this.activeIndex >= 0 && idx !== this.activeIndex;
    this.activeIndex = idx;
    const slot = this.slots[idx];
    this.acquireOpen(slot);
    this.applyStyle(slot);
    if (intro) {
      for (const s of this.slots) s.lift.snap(1);
      this.roofLift.snap(1);
      this.group.visible = true;
      this.introDrop = true;
    }
    if (moved && !intro && !REDUCED) { this.pulseT = 0; this.swapped = false; }   // swap under the blur peak
    else this.applyShown();
    this.retarget(intro);
    if (intro) { this.cabinY.snap(this.cabinY.target); this.shaftTop.snap(this.shaftTop.target); }   // a whole elevator, not a stub growing alone
    return true;
  }

  // The active floor shows its open interior, every other floor its closed glass module.
  applyShown() {
    this.slots.forEach((s, i) => { s.showOpen = i === this.activeIndex && !!s.open; });
  }

  retarget(intro) {
    const a = this.activeIndex;
    if (a < 0) return;
    this.slots.forEach((s, i) => {
      if (i <= a) s.lift.to(0, intro ? 0.05 + (a - i) * 0.08 : 0);
      else s.lift.to(1);
    });
    this.roofLift.to(1);
    this.cabinY.to(a * FLOOR_STEP);
    this.shaftTop.to(Math.min(this.slots.length * FLOOR_STEP, a * FLOOR_STEP + CAB_H + 0.35));
  }

  acquireOpen(slot) {
    if(slot.kind==='overview'){slot.open=this.overview;this.overview.floorId=slot.id;return this.overview;}
    if (slot.open) return slot.open;
    // a free instance, else steal the hidden one (the visible one belongs to the floor we are leaving)
    const inst = this.pool.find((p) => !this.slots.some((s) => s.open === p))
      || this.pool.reduce((m, p) => (p.alpha < m.alpha ? p : m), this.pool[0]);
    const owner = this.slots.find((s) => s.open === inst);
    if (owner) { owner.open = null; owner.showOpen = false; }
    inst.onSteal?.();
    slot.open = inst;
    inst.floorId = slot.id;
    inst.screens.forEach((s) => { s.glow = 0; s.target = 0; });
    return inst;
  }

  // flags[deskIndex] = 1 when that desk's bot is working, 0 otherwise (empty desks too)
  setScreens(flags) {
    this.screenTargets = flags;
  }

  setDisplayData(data) {
    this.floorData = data;
    this.activeOpen()?.walls?.setData(data);
  }

  prewarm(camera) {
    const all = [this.roof, this.shaft, this.cabin, ...this.slots.map((s) => s.closed), ...this.pool, this.overview];
    const saved = all.map((i) => i.root.visible);
    all.forEach((i) => { i.root.visible = true; });
    const hidden = this.pool.flatMap((p) => styleObjects(p.obj)).filter((o) => !o.visible);   // every lounge corner, so a switch never compiles
    hidden.forEach((o) => { o.visible = true; });
    // The scene is drawn into the post chain's targets (linear, tone mapped later): compile for a target too, or every
    // program built here has the wrong output variant and the real one compiles mid-ride.
    const rt = new THREE.WebGLRenderTarget(1, 1, { type: THREE.HalfFloatType }), prevRT = this.renderer.getRenderTarget();
    this.renderer.setRenderTarget(rt);
    this.renderer.compile(this.scene, camera);
    this.renderer.setRenderTarget(prevRT);
    rt.dispose();
    hidden.forEach((o) => { o.visible = false; });
    all.forEach((i, k) => { i.root.visible = saved[k]; });
  }

  update(dt, nowMs) {
    const a = this.activeIndex;
    const night = this.daylight.night;
    const N = this.slots.length;

    if (this.pulseT >= 0) {
      this.pulseT += dt;
      if (!this.swapped && this.pulseT >= SWAP_AT) { this.swapped = true; this.applyShown(); }
      if (this.pulseT >= SWAP_AT + BLUR_TAIL) this.pulseT = -1;
    }
    const p = this.pulseT < 0 ? 0 : this.pulseT < SWAP_AT ? smoothstep(0, SWAP_AT, this.pulseT) : 1 - smoothstep(SWAP_AT, SWAP_AT + BLUR_TAIL, this.pulseT);
    const px = Math.round(p * BLUR_PX * 4) / 4;
    if (px !== this.blurPx) { this.blurPx = px; this.renderer.domElement.style.filter = px ? `blur(${px}px)` : ''; }

    for (const s of this.slots) {
      s.lift.step(dt);
      const off = s.lift.x * (LIFT + Math.max(0, s.index - a) * FLOOR_STEP);
      const inView = off < LIFT * 0.8;
      const y = s.index * FLOOR_STEP + off;
      s.closed.root.position.y = y;
      s.closed.setShown(inView && !s.showOpen);
      if (s.open) {
        s.open.root.position.y = y;
        s.open.setShown(inView && s.showOpen);
        if (s.open.alpha) this.updateOpen(s, dt, nowMs, night);
      }
    }

    this.roofLift.step(dt);
    const roff = this.roofLift.x * (LIFT + Math.max(0, N - a) * FLOOR_STEP);
    this.roof.root.position.y = N * FLOOR_STEP - SLAB + roff;
    this.roof.setShown(roff < LIFT * 0.8);

    this.cabinY.step(dt);
    this.shaftTop.step(dt);
    const act = this.slots[a];
    if (this.introDrop && (!act || (act.lift.x < 1e-3 && act.lift.pending === null))) this.introDrop = false;
    const drop = this.introDrop ? act.lift.x * LIFT : 0;
    this.cabin.root.position.y = this.cabinY.x + drop;
    const bottom = -SLAB, top = Math.max(bottom + 0.1, this.shaftTop.x);
    this.shaft.root.position.y = bottom + drop;
    this.shaft.root.scale.y = (top - bottom) / this.shaftH;

    this.updateNightMaps(nowMs);
  }

  updateOpen(slot, dt, nowMs, night) {
    const inst = slot.open;
    const k = 1 - Math.exp(-dt * 3.2);
    const isActive = this.slots[this.activeIndex] === slot;
    inst.screens.forEach((s, i) => {
      if (!s) return;
      s.target = isActive ? (this.screenTargets[i] || 0) : 0;
      s.glow += (s.target - s.glow) * k;
      s.mat.emissiveIntensity = s.glow * (0.32 + 1.35 * night) + night * 0.03;
    });
    inst.walls?.tick(nowMs, night);
  }
}
