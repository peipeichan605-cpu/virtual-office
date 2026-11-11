// One bot in the office: jelly blob model + quiet in-scene tag + animations.
// v2.5 behaviour: blobs turn toward the cursor (ground point within ~3.5 m, seated bots with a limited turn),
// their eyes follow it, they hop when the cursor is within ~0.9 m. Everything moves on springs.
import * as THREE from 'three';
import { CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';
import { DialogueBubble } from './dialogue.js';
import { Spring, REDUCED, clamp, smoothstep } from './spring.js';

export const STATUS_COLOR = {
  working: '#34C759', idle: '#A1A1A6', waiting_user: '#FF9F0A', error: '#FF3B30', stuck: '#FF9500', done: '#0A84FF',
};
const STATUS_LABEL = { waiting_user: 'Waiting for you', error: 'Error', stuck: 'Stuck?', done: 'Done', working: 'Working', idle: 'Resting' };
const CAMERA_YAW = Math.PI / 4;     // isometric camera sits on +X +Z
const RESTING_TURN = 0.42;          // blobs lean toward the camera a bit
const MODEL_HEIGHT = 1.0;
const LOOK_RANGE = 3.5;             // m: cursor ground point that makes a bot look
const HOP_RANGE = 0.9;              // m: cursor this close makes it hop
const HOP_COOLDOWN = 1.2;
const EYE_SHIFT = 0.034;            // m, at full sideways glance

const ringTexture = makeRadialTexture([[0, 'rgba(255,255,255,0.95)'], [0.45, 'rgba(255,255,255,0.55)'], [1, 'rgba(255,255,255,0)']]);
const shadowTexture = makeRadialTexture([[0, 'rgba(0,0,0,0.38)'], [0.6, 'rgba(0,0,0,0.12)'], [1, 'rgba(0,0,0,0)']]);
// "done": a short soft flash around the bot and one ripple across the floor, in the done colour (no confetti)
const haloTexture = makeRadialTexture([[0, 'rgba(255,255,255,0.9)'], [0.32, 'rgba(255,255,255,0.42)'], [1, 'rgba(255,255,255,0)']]);
const rippleTexture = makeRadialTexture([[0, 'rgba(255,255,255,0)'], [0.6, 'rgba(255,255,255,0)'], [0.8, 'rgba(255,255,255,0.95)'],
  [0.9, 'rgba(255,255,255,0.3)'], [1, 'rgba(255,255,255,0)']]);
const RIPPLE_GEO = new THREE.PlaneGeometry(2, 2);
const CELEBRATE_S = 0.95;

export class Actor {
  // ctx: { parent: Object3D (the open-floor instance root), inst, pickables: Set, confetti(fn), cursor: {active,x,z}, dropDelay }
  constructor(bot, ctx) {
    this.id = bot.id;
    this.ctx = ctx;
    this.phase = hash(bot.id) % 1000 / 1000 * Math.PI * 2;
    this.status = null;
    this.hovered = false;
    this.selected = false;
    this.anim = null;                // one-shot animation (hop / land / travel / drop)
    this.nextBlink = 1 + Math.random() * 3;
    this.blinkT = -1;
    this.nextAction = 1 + Math.random() * 2;
    this.hopCooldown = 0.6;
    this.retiring = false;
    this.dead = false;
    this.hostAlpha = 1;
    this.tagKey = '';

    this.yawS = new Spring(RESTING_TURN, 0.3);
    this.scaleS = new Spring(1, 0.28);
    this.eyeX = new Spring(0, 0.16);
    this.eyeY = new Spring(0, 0.16);
    this.modelScale = 1;

    this.root = new THREE.Group();
    this.pivot = new THREE.Group();
    this.modelWrap = new THREE.Group();
    this.pivot.add(this.modelWrap);
    this.root.add(this.pivot);

    const shadow = new THREE.Mesh(new THREE.PlaneGeometry(0.85, 0.85), new THREE.MeshBasicMaterial({ map: shadowTexture, transparent: true, depthWrite: false }));
    shadow.rotation.x = -Math.PI / 2;
    shadow.position.y = 0.006;
    shadow.renderOrder = 1;
    this.pivot.add(shadow);

    this.ring = new THREE.Mesh(new THREE.PlaneGeometry(1.5, 1.5), new THREE.MeshBasicMaterial({ map: ringTexture, transparent: true, depthWrite: false, color: STATUS_COLOR.idle, opacity: 0 }));
    this.ring.rotation.x = -Math.PI / 2;
    this.ring.renderOrder = 1;
    this.root.add(this.ring);

    this.tagEl = document.createElement('div');
    this.tagEl.className = 'hqt';
    this.tagEl.dataset.mode = 'off';
    this.tagEl.innerHTML = '<div class="hqt-pill"><i class="hqt-dot"></i><span class="hqt-text"></span></div><div class="hqt-stem"></div>';
    this.tagText = this.tagEl.querySelector('.hqt-text');
    this.tagStatus=document.createElement('span');this.tagStatus.className='hqt-state';this.tagEl.querySelector('.hqt-pill').append(this.tagStatus);
    this.tagRelation=document.createElement('span');this.tagRelation.className='hqt-relation';this.tagEl.querySelector('.hqt-pill').after(this.tagRelation);
    this.tag = new CSS2DObject(this.tagEl);
    this.tag.center.set(0.5, 0);
    this.tag.position.y = MODEL_HEIGHT + 0.24;
    this.tag.visible = false;
    this.pivot.add(this.tag);

    this.dialogue = new DialogueBubble(() => this.ctx.onSelect?.(this.id), id => this.ctx.onOpenDialogue?.(id));
    this.speech = new CSS2DObject(this.dialogue.element);
    this.speech.center.set(0.5, 1);
    this.speech.position.y = MODEL_HEIGHT + 0.65;
    this.speech.visible = false;
    this.pivot.add(this.speech);
    this.setPlaceholderModel(bot.color);
    ctx.parent.add(this.root);
  }

  // ---------- model ----------

  setPlaceholderModel(color) {
    const body = new THREE.Mesh(new THREE.SphereGeometry(0.46, 40, 28), jelly(color));
    body.scale.set(1, 0.95, 0.82);
    body.position.y = 0.44;
    const eyeGeo = new THREE.CapsuleGeometry(0.042, 0.085, 6, 12);
    const eyeMat = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.25, emissive: 0xffffff, emissiveIntensity: 0.15 });
    const eyeL = new THREE.Mesh(eyeGeo, eyeMat); eyeL.position.set(-0.12, 0.52, 0.36);
    const eyeR = new THREE.Mesh(eyeGeo, eyeMat); eyeR.position.set(0.12, 0.52, 0.36);
    const g = new THREE.Group();
    g.add(body, eyeL, eyeR);
    this.modelScale = 1;
    this.useModel(g, [eyeL, eyeR], true);
  }

  // model: Object3D from a blob GLB (already cloned); tint: hex color for the default blob
  setModel(model, tint) {
    let eyes = [];
    model.traverse((o) => {
      if (/^eye_[LR]/.test(o.name)) eyes.push(o);
      if (o.isMesh && tint && /body/.test(o.name + (o.parent?.name || ''))) {
        o.material = o.material.clone();
        o.material.color.set(tint);
      }
    });
    eyes = eyes.filter((e) => !eyes.some((p) => p !== e && isAncestor(p, e)));
    const box = new THREE.Box3().setFromObject(model);
    const size = box.getSize(new THREE.Vector3());
    const s = MODEL_HEIGHT / Math.max(size.x, size.y, size.z, 0.01);
    model.scale.multiplyScalar(s);
    box.setFromObject(model);
    const c = box.getCenter(new THREE.Vector3());
    model.position.x -= c.x; model.position.z -= c.z; model.position.y -= box.min.y;
    this.modelScale = model.scale.x || 1;
    box.setFromObject(model);
    this.tag.position.y = box.max.y + 0.24;                       // pill floats just above this blob's head
    this.speech.position.y = box.max.y + 0.65;
    this.useModel(model, eyes, false);
  }

  useModel(model, eyes, placeholder) {
    for (const o of this.pickMeshes || []) this.ctx.pickables.delete(o);
    for(const previous of this.modelWrap.children)previous.userData?.disposePlatformModel?.();
    this.modelWrap.clear();
    this.modelWrap.add(model);
    this.eyes = eyes;
    this.eyeBase = eyes.map((e) => e.scale.y);
    this.eyePos = eyes.map((e) => e.position.clone());
    this.pickMeshes = [];
    model.traverse((o) => { if (o.isMesh) { o.userData.actorId = this.id; this.pickMeshes.push(o); if (!this.retiring) this.ctx.pickables.add(o); } });
    this.placeholder = placeholder;
  }

  // ---------- state ----------

  update(bot, spot, view) {
    const status = bot.effective_status || bot.status;
    const statusChanged = status !== this.status;
    const prev = this.status;
    this.status = status;
    this.bot = bot;
    this.tag.center.y=bot.overview?1:0;

    const color = STATUS_COLOR[status] || STATUS_COLOR.idle;
    this.ring.material.color.set(color);
    this.tagEl.dataset.status = status;
    this.tagEl.style.setProperty('--c', color);
    this.selected = view.selectedBot === bot.id;
    this.dialogue.update(bot, this.selected);
    this.renderTag();

    if (spot && (!this.spot || this.spot.pos.distanceToSquared(spot.pos) > 1e-4)) {
      if (!this.spot) this.dropIn(spot); else this.travelTo(spot);
      this.spot = spot;
    }
    this.ring.position.y = 0.012 - (this.spot ? this.spot.pos.y : 0);

    if (statusChanged && prev) {
      if (status === 'done') { this.celebrate(); this.hop(0.45); }
      else if (status === 'waiting_user' || status === 'error') this.hop(0.3);
    }
  }

  // Quiet by default: selected (dark pill, name), needs-the-user (compact pill, short reason), hovered (pill, name).
  renderTag() {
    const s = this.status, bot = this.bot;
    if (!bot) return;
    const attn = s === 'waiting_user' || s === 'error' || s === 'stuck';
    const mode = this.selected ? 'sel' : bot.overview || bot.is_subagent ? 'overview' : attn ? 'attn' : this.hovered ? 'hover' : 'off';
    let text = bot.name || '';
    if (mode === 'attn') {
      const reason = reasonText(s, bot.step);
      text = this.hovered ? `${bot.name} · ${reason}` : reason;
    }
    const statusText={working:'工作中',idle:'休息中',waiting_user:'等待',error:'錯誤',stuck:'待確認',done:'完成'}[s]||s;
    const relation=bot.is_subagent?`${bot.parent_name||bot.root_name||'主 Agent'} 的子 Agent`:bot.child_count?`子 Agent · ${bot.child_count}`:'';
    const key = `${mode}|${s}|${text}|${relation}`;
    if (key === this.tagKey) return;
    this.tagKey = key;
    this.tagEl.dataset.mode = mode;
    this.tagText.textContent = text;
    this.tagStatus.textContent=bot.overview||bot.is_subagent?statusText:'';
    this.tagRelation.textContent=relation;
    this.tagEl.title = [bot.name,statusText,relation].filter(Boolean).join(' · ');
  }

  spotIsLounge() { return !!this.spot && this.spot.lounge; }

  setHovered(on) {
    if (this.hovered === on) return;
    this.hovered = on;
    if (on && !this.anim) this.hop(0.2);
    this.renderTag();
  }

  setDragging(position){
    this.dragPosition=position?.clone()||null;
    if(!position&&this.spot&&!this.retiring)this.travelTo(this.spot);
  }

  retire() {
    if (this.retiring) return;
    this.retiring = true;
    this.dialogue.disable();this.speech.visible=false;
    this.hovered = false;
    this.scaleS.to(0);
    for (const o of this.pickMeshes || []) this.ctx.pickables.delete(o);
    this.tagEl.dataset.mode = 'off';
    this.tag.visible = false;
  }

  // ---------- motion ----------

  dropIn(spot) {
    this.root.position.copy(spot.pos);
    this.baseYaw = spot.yaw;
    this.yawS.snap(spot.yaw + RESTING_TURN);
    this.anim = { kind: 'drop', t: -(this.ctx.dropDelay || 0), dur: 0.75, from: 2.4 };
    this.scaleS.snap(this.ctx.dropDelay > 0 ? 0.001 : 1);
    if (this.ctx.dropDelay > 0) this.scaleS.to(1, this.ctx.dropDelay);
  }

  travelTo(spot) {
    const from = this.root.position.clone();
    const dist = from.distanceTo(spot.pos);
    if (REDUCED || dist < 0.05) { this.root.position.copy(spot.pos); this.baseYaw = spot.yaw; return; }
    const dir = new THREE.Vector3().subVectors(spot.pos, from);
    this.anim = { kind: 'travel', t: 0, dur: Math.max(0.8, dist / 2.6), from, to: spot.pos.clone(), toYaw: spot.yaw, travelYaw: Math.atan2(dir.x, dir.z) };
  }

  hop(height) {
    if (REDUCED || this.anim || this.retiring) return;
    this.anim = { kind: 'hop', t: 0, dur: 0.62, h: height };
  }

  /** a task is done: a soft flash around the bot, one ripple on the floor, happy squinting eyes */
  celebrate() {
    if (REDUCED || this.retiring) return;
    this.endCelebration();
    const color = new THREE.Color(STATUS_COLOR.done);
    const ripple = new THREE.Mesh(RIPPLE_GEO, new THREE.MeshBasicMaterial({ map: rippleTexture, color, transparent: true, depthWrite: false, opacity: 0 }));
    ripple.rotation.x = -Math.PI / 2;
    ripple.position.y = this.ring.position.y + 0.003;
    ripple.renderOrder = 2;
    const halo = new THREE.Sprite(new THREE.SpriteMaterial({ map: haloTexture, color, transparent: true, depthWrite: false, opacity: 0 }));
    halo.position.y = Math.max(0.3, this.tag.position.y - 0.24) * 0.55;          // the middle of the body
    halo.renderOrder = 2;
    this.root.add(ripple, halo);
    this.fx = { t: 0, ripple, halo };
    this.joy = 0.75;
  }

  endCelebration() {
    if (!this.fx) return;
    for (const o of [this.fx.ripple, this.fx.halo]) { o.removeFromParent(); o.material.dispose(); }
    this.fx = null;
  }

  tickCelebration(dt) {
    const f = this.fx;
    f.t += dt;
    const k = f.t / CELEBRATE_S;
    if (k >= 1) { this.endCelebration(); return; }
    const out = 1 - (1 - k) ** 3;                                   // the ripple runs out fast and slows down
    f.ripple.scale.setScalar(0.4 + 1.6 * out);
    f.ripple.material.opacity = 0.8 * (1 - k) ** 1.3;
    const fk = Math.min(1, f.t / 0.55);                             // the flash: up and gone in about half a second
    f.halo.scale.setScalar(0.8 + 1.3 * (1 - (1 - fk) ** 2));
    f.halo.material.opacity = 0.6 * Math.sin(Math.PI * fk);
  }

  tick(dt, t) {
    const p = this.pivot;
    const s = this.status;
    let y = 0, sx = 1, sy = 1, rx = 0, rz = 0, ox = 0;

    if (!REDUCED) {
      const breathe = Math.sin(t * 2.1 + this.phase);
      sy = 1 + 0.018 * breathe; sx = 1 - 0.012 * breathe;
      if (s === 'working') { y = 0.012 * Math.abs(Math.sin(t * 9 + this.phase)); rx = 0.06; }
      if (s === 'stuck') rz = 0.09 * Math.sin(t * 1.3 + this.phase);
      if (s === 'error') {
        const k = (t + this.phase) % 1.6;
        if (k < 0.45) ox = 0.03 * Math.sin(k * 70);
      }
      if (s === 'waiting_user' && !this.anim) {
        this.nextAction -= dt;
        if (this.nextAction <= 0) { this.hop(0.32); this.nextAction = 1.7 + Math.random() * 0.6; }
      }
    }

    // one-shot animation
    if (this.anim) {
      const a = this.anim;
      a.t += dt;
      const k = clamp(a.t / a.dur, 0, 1);
      if (a.kind === 'hop') ({ y, sx, sy } = hopCurve(k, a.h));
      else if (a.kind === 'drop') {
        if (a.t < 0) y = a.from;
        const fall = Math.min(1, k / 0.55);
        if (a.t >= 0) y = a.from * (1 - fall * fall);
        if (k > 0.55) ({ sx, sy } = squash((k - 0.55) / 0.45));
      } else if (a.kind === 'travel') {
        const e = easeInOut(k);
        this.root.position.lerpVectors(a.from, a.to, e);
        const hops = Math.max(1, Math.round(a.dur / 0.42));
        const local = (k * hops) % 1;
        y = 0.55 * Math.sin(local * Math.PI);
        sy = 1 + 0.12 * Math.sin(local * Math.PI); sx = 1 - 0.07 * Math.sin(local * Math.PI);
        this.baseYaw = k < 0.92 ? a.travelYaw : a.toYaw;
      }
      if (a.t >= a.dur) {
        if (a.kind === 'travel') { this.root.position.copy(a.to); this.baseYaw = a.toYaw; }
        this.anim = null;
      }
    }

    if(this.dragPosition)this.root.position.copy(this.dragPosition);

    // ---- cursor: look toward it, glance with the eyes, hop when it is close ----
    const cur = this.ctx.cursor;
    let w = 0, lookYaw = 0, dist = 99;
    if (cur?.active && !this.retiring && this.hostAlpha > 0.6) {
      const dx = cur.x - this.root.position.x, dz = cur.z - this.root.position.z;
      dist = Math.hypot(dx, dz);
      if (dist < LOOK_RANGE) { w = smoothstep(LOOK_RANGE, LOOK_RANGE - 1.3, dist); lookYaw = Math.atan2(dx, dz); }
    }
    this.hopCooldown -= dt;
    if (dist < HOP_RANGE && this.hopCooldown <= 0 && !this.anim && !REDUCED) { this.hop(0.26); this.hopCooldown = HOP_COOLDOWN; }

    const base = (this.baseYaw || 0) + (this.anim?.kind === 'travel' ? 0 : RESTING_TURN);
    let target = base;
    if (this.hovered || this.selected) target = CAMERA_YAW;
    else if (w > 0) {
      const seated = !this.spotIsLounge() && s !== 'idle';
      const lim = seated ? 0.8 : 1.9;                               // seated bots never leave the desk
      target = base + clamp(angleDiff(lookYaw, base), -lim, lim) * w;
    }
    this.yawS.target = this.yawS.x + angleDiff(target, this.yawS.x);
    this.yawS.step(dt);

    const sel = this.selected ? 1.06 : 1;
    const sc = this.scaleS.step(dt);
    p.position.set(ox, y, 0);
    p.scale.set(sx * sel * sc, sy * sel * sc, sx * sel * sc);
    p.rotation.set(rx, this.yawS.x, rz, 'YXZ');

    // eyes
    if (w > 0 && !this.hovered && !this.selected) {
      const ang = angleDiff(lookYaw, this.yawS.x);
      this.eyeX.to(Math.sin(clamp(ang, -1.2, 1.2)) * EYE_SHIFT * w);
      this.eyeY.to(-0.012 * w);
    } else { this.eyeX.to(0); this.eyeY.to(0); }
    this.eyeX.step(dt); this.eyeY.step(dt);

    // status ring: quiet unless it matters
    const pulse = s === 'working' ? 0.14 + 0.05 * Math.sin(t * 3 + this.phase)
      : s === 'waiting_user' || s === 'error' ? 0.4 + 0.25 * (0.5 + 0.5 * Math.sin(t * 5))
      : s === 'stuck' ? 0.3 + 0.15 * Math.sin(t * 2) : 0;
    const wantRing = this.selected ? 0.8 : this.hovered ? Math.max(0.4, pulse) : pulse;
    this.ring.material.opacity = wantRing * clamp(sc, 0, 1);
    this.ring.scale.setScalar(this.selected ? 1.12 : 1);
    if (this.fx) this.tickCelebration(dt);

    // tag follows the host floor: hidden while that floor is faded / lifted
    this.speech.visible = this.dialogue.visible && this.hostAlpha > 0.5 && !this.retiring && this.scaleS.x > 0.6;
    this.tag.visible = !this.speech.visible && this.tagEl.dataset.mode !== 'off' && this.hostAlpha > 0.5 && !this.retiring && this.scaleS.x > 0.6;
    // Floor/model animation can hide the anchor for one frame; keep the reader open.
    if (this.retiring && this.dialogue.open) this.dialogue.setOpen(false);
    this.tickEyes(dt);
    if (this.retiring && this.scaleS.x < 0.03) { this.dead = true; this.dispose(); }
  }

  tickEyes(dt) {
    if (!this.eyes.length) return;
    const s = this.status;
    let open = 1;
    if (s === 'idle' && !this.spotIsLounge()) open = 0.12;        // sleeping at the desk
    else if (s === 'error') open = 0.55;
    else if (s === 'stuck') open = 0.7;
    if (open > 0.5 && !REDUCED) {
      this.nextBlink -= dt;
      if (this.nextBlink <= 0) { this.blinkT = 0; this.nextBlink = 2.2 + Math.random() * 3.5; }
      if (this.blinkT >= 0) {
        this.blinkT += dt;
        const b = this.blinkT / 0.16;
        if (b >= 1) this.blinkT = -1; else open *= Math.max(0.1, Math.abs(1 - 2 * b));
      }
    }
    if (this.joy > 0) { this.joy -= dt; open = Math.min(open, 0.32); }   // just finished a task: happy, squinting
    const k = 1 / this.modelScale;
    this.eyes.forEach((e, i) => {
      e.scale.y = this.eyeBase[i] * open;
      e.position.x = this.eyePos[i].x + this.eyeX.x * k;
      e.position.y = this.eyePos[i].y + this.eyeY.x * k;
    });
  }

  dispose() {
    this.endCelebration();
    for (const o of this.pickMeshes || []) this.ctx.pickables.delete(o);
    for(const model of this.modelWrap.children)model.userData?.disposePlatformModel?.();
    this.modelWrap.clear();
    this.dialogue.dispose();
    this.tagEl.remove();
    this.root.removeFromParent();
  }
}

// ---------- helpers ----------

export function jelly(color) {
  return new THREE.MeshPhysicalMaterial({ color, roughness: 0.32, clearcoat: 1, clearcoatRoughness: 0.18, sheen: 0.3 });
}

function reasonText(status, step) {
  let t = (step || '').trim();
  if (status === 'stuck' || !t) return STATUS_LABEL[status] || '';
  t = t.charAt(0).toUpperCase() + t.slice(1);
  return t.length > 34 ? `${t.slice(0, 33).trimEnd()}…` : t;
}

function hopCurve(k, h) {
  if (k < 0.15) { const q = k / 0.15; return { y: 0, sx: 1 + 0.12 * q, sy: 1 - 0.18 * q }; }
  if (k < 0.7) { const q = (k - 0.15) / 0.55; return { y: h * Math.sin(q * Math.PI), sx: 0.93, sy: 1.12 }; }
  return { y: 0, ...squash((k - 0.7) / 0.3) };
}

function squash(q) {
  const a = Math.sin(q * Math.PI) * (1 - q);
  return { sx: 1 + 0.16 * a, sy: 1 - 0.2 * a };
}

const easeInOut = (k) => (k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2);

function angleDiff(a, b) {
  return ((a - b + Math.PI) % (Math.PI * 2) + Math.PI * 2) % (Math.PI * 2) - Math.PI;
}

function isAncestor(p, c) {
  for (let n = c.parent; n; n = n.parent) if (n === p) return true;
  return false;
}

export function hash(str) {
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); }
  return h >>> 0;
}

function makeRadialTexture(stops) {
  const c = document.createElement('canvas');
  c.width = c.height = 128;
  const g = c.getContext('2d');
  const grad = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  for (const [o, col] of stops) grad.addColorStop(o, col);
  g.fillStyle = grad;
  g.fillRect(0, 0, 128, 128);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}
