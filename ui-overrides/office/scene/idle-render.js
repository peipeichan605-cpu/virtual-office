// Rendering policy only: feed polling, receivers and command delivery never
// depend on this controller. A sleeping office still refreshes lighting/UI.
const fields = (object, names) => names.map(name => object?.[name] ?? null);
const canonical = value => Array.isArray(value) ? value.map(canonical) : value && typeof value === 'object'
  ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value;

const BOT_FIELDS = ['id','agent_id','name','provider','status','effective_status','state','step','floor','desk','ground_desk_id',
  'avatar','has_glb','look','look_origin','color','is_subagent','parent_agent_id','root_agent_id','parent_run_id',
  'parent_name','root_name','depth','evidence','source_kind','child_count','usage_pct','usage_share'];
const MESSAGE_FIELDS = ['event_id','seq','run_id','role','phase','text','command_id','at'];

export function meaningfulOfficeSignature(state, view = {}) {
  const bots = Object.entries(state?.bots || {}).sort(([a],[b]) => a.localeCompare(b)).map(([id,bot]) => {
    const capability = bot.command_capability;
    const task = bot.task && typeof bot.task === 'object' ? fields(bot.task,['id','run_id','name','title','text','status','step']) : bot.task ?? null;
    return [id, fields(bot,BOT_FIELDS), task, fields(bot.latest_message,MESSAGE_FIELDS),
      fields(bot.workspace,['id','key','path','root','name']),
      fields(capability,['mode','can_send','enabled','persistent','native_ready','receiver_online','same_session','reason']),
      fields(capability?.public_sync,['configured','mode','state','scope','reason']),
      fields(capability?.connection_check,['state','command_id','verified','reply_verified'])];
  });
  const floors = [...(state?.floors || [])].sort((a,b) => String(a.id).localeCompare(String(b.id))).map(floor =>
    [fields(floor,['id','name','kind','order','seat_count','accent','style']), [...(floor.bots || [])].sort()]);
  const finished = (state?.finished || []).map(row => fields(row,['id','run_id','bot','agent_id','name','status','step','delivery_preview','summary','text','t','finished_at']));
  // Deliberately omit now/version, seen-age, heartbeat times and accumulating
  // daily counters: identical one-second feed polls must allow real sleep.
  return JSON.stringify(canonical([bots,floors,finished,fields(view,['currentFloor','selectedBot','simpleMode'])]));
}

export function hasWorkingAgents(state) {
  return Object.values(state?.bots || {}).some(bot => [bot.effective_status,bot.status,bot.state].some(status => ['working','running','busy'].includes(status)));
}

const springMoving = spring => !!spring && spring.settled === false;
export function hasSceneTransition({camera,building,actors = [],retiring,effects = [],drag,pinch,seatSaving} = {}) {
  if (!building || drag || pinch || seatSaving || (retiring?.size || retiring?.length || 0) || effects.length) return true;
  if (Object.values(camera || {}).some(springMoving)) return true;
  if (building.pulseT >= 0 || building.introDrop) return true;
  if (['roofLift','cabinY','shaftTop'].some(name => springMoving(building[name]))) return true;
  for (const slot of building.slots || []) {
    if (springMoving(slot.lift)) return true;
    if (slot.open?.alpha && slot.open.screens?.some(screen => screen && Math.abs(screen.glow-screen.target) > 1e-4)) return true;
  }
  for (const actor of actors) {
    // Let an already-running hop/blink finish, but do not reset inactivity for
    // cosmetics. The next quiet gap sleeps before another hop can be spawned.
    if (actor.anim || actor.fx || actor.retiring || actor.dragPosition || actor.blinkT >= 0) return true;
    if (['scaleS','yawS','eyeX','eyeY'].some(name => springMoving(actor[name]))) return true;
  }
  return false;
}

export class IdleRenderController {
  constructor({enabled = true,idleAfterMs = 5000,heartbeatMs = 1000,onTransition} = {}) {
    if (!(idleAfterMs > 0) || !(heartbeatMs > 0)) throw new RangeError('idle intervals must be positive');
    this.enabled = enabled; this.idleAfterMs = idleAfterMs; this.heartbeatMs = heartbeatMs; this.onTransition = onTransition;
    this.lastActivity = 0; this.lastRender = null; this.signature = null; this.working = false; this.idle = false; this.dirty = true; this.hidden = false;
  }
  wake(now, reason = 'interaction') {
    this.lastActivity = now; this.dirty = true;
    if (this.idle) { this.idle = false; this.onTransition?.({idle:false,now,reason}); }
  }
  observe(state, view, now) {
    this.working = hasWorkingAgents(state);
    const signature = meaningfulOfficeSignature(state,view);
    if (signature === this.signature) return false;
    this.signature = signature; this.wake(now,'state'); return true;
  }
  quietDue(now, working = this.working) { return this.enabled && !working && now-this.lastActivity >= this.idleAfterMs; }
  frame(now, {hidden = false,busy = false,working = this.working} = {}) {
    if (hidden) { this.hidden = true; return {draw:false,idle:this.idle}; }
    if (this.hidden) { this.hidden = false; this.wake(now,'visibility'); }
    if (!this.enabled) return {draw:true,idle:false};
    if (working || busy) {
      if (this.idle) this.wake(now,working?'working':'transition');
      return {draw:true,idle:false};
    }
    if (this.quietDue(now,working)) {
      if (!this.idle) { this.idle = true; this.onTransition?.({idle:true,now,reason:'quiet'}); }
      return {draw:this.dirty || this.lastRender === null || now-this.lastRender >= this.heartbeatMs,idle:true};
    }
    return {draw:true,idle:false};
  }
  didRender(now) { this.lastRender = now; this.dirty = false; }
}

// CSS2D styles and message-age labels change while rendering. Observe only
// semantic public dialogue text, and compare it before treating a mutation as
// activity. Neither payloads nor these in-memory signatures are ever logged.
const PUBLIC_DIALOGUE = '.speech-preview,.speech-meta,.speech-message p,.speech-family-list,.command-status,.command-pending,.command-approvals';
export function publicDialogueSignature(root) {
  return JSON.stringify([...root.querySelectorAll(PUBLIC_DIALOGUE)].map(element => element.textContent || ''));
}
export function observePublicDialogue(root, wake, Observer = globalThis.MutationObserver) {
  if (!Observer) return null;
  let signature = publicDialogueSignature(root);
  const relevant = node => {
    const element = node?.nodeType === 1 ? node : node?.parentElement;
    return !!element && (element.matches?.(PUBLIC_DIALOGUE) || element.closest?.(PUBLIC_DIALOGUE) || element.querySelector?.(PUBLIC_DIALOGUE));
  };
  const observer = new Observer(records => {
    if (!records.some(record => relevant(record.target) || [...record.addedNodes || [],...record.removedNodes || []].some(relevant))) return;
    const next = publicDialogueSignature(root);
    if (next !== signature) { signature = next; wake(); }
  });
  observer.observe(root,{subtree:true,childList:true,characterData:true});
  return observer;
}
