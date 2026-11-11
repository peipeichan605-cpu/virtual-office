// Observed children are a visual projection of a selected member's native work.
// Keep them outside StateStore: they never occupy a desk or create another floor.
export const isObservedChild = bot => bot?.is_subagent === true;
export const memberBots = bots => bots.filter(bot => !isObservedChild(bot));
export const childVisualId = agent => 'subagent:' + String(typeof agent === 'string' ? agent : agent.id);
export const canManageAgent = bot => !!bot?.agent_id && !isObservedChild(bot);
export const childEvidenceLabel = bot => ({native_hook:'原生事件同步',native_metadata:'原生紀錄同步',parent_reported:'Agent 合作回報'}[bot?.evidence] || '');

export function withObservedSubagents(state, agents, historyUrl) {
  const bots = Object.fromEntries(Object.entries(state.bots).filter(([,bot]) => !isObservedChild(bot)));
  const members = new Map(Object.values(bots).map(bot => [bot.agent_id, bot]));
  const definitions = new Map(agents.map(agent => [agent.id, agent]));
  const children = agents.filter(isObservedChild);
  const counts = new Map();
  for (const agent of children) {
    const root = members.get(agent.root_agent_id);
    if (!root) continue; // A child cannot outlive its selected root membership.
    const parent = definitions.get(agent.parent_agent_id);
    const status = ['working','done','idle','waiting_user','error'].includes(agent.state) ? agent.state : 'waiting_user';
    const id = childVisualId(agent);
    bots[id] = {
      id, agent_id: agent.id, name: String(agent.name || '子 Agent'), provider: agent.provider,
      is_subagent: true, parent_agent_id: agent.parent_agent_id, root_agent_id: agent.root_agent_id,
      parent_name: parent?.name || root.name, root_name: root.name, depth: agent.depth || 1,
      parent_run_id: agent.parent_run_id, floor: root.floor, desk: null, ground_desk_id: null,
      evidence: agent.evidence || null, source_kind: agent.source_kind || null,
      workspace: root.workspace, status, effective_status: status, step: agent.step || '狀態待確認',
      task: null, since: agent.state_changed_at || null, last_event: agent.state_changed_at || null,
      last_seen: agent.state_changed_at || null, latest_message: agent.latest_message || null,
      command_capability: {can_send:false, enabled:false, mode:'observation', reason:'observed_subagent'},
      chat_url: historyUrl(agent.id), look_origin: 'platform-default', look: null,
      color: root.color, avatar: null, has_glb: false, usage_pct: null, usage_share: null,
      today: {working_min:0,events:0,tasks_done:0,hourly:new Array(24).fill(0)}, history: [], schedule: [],
    };
    counts.set(root.agent_id, (counts.get(root.agent_id) || 0) + 1);
  }
  for (const [id,bot] of Object.entries(bots)) if (!isObservedChild(bot)) {
    bots[id] = {...bot, child_count: counts.get(bot.agent_id) || 0};
  }
  const floors = state.floors.map(floor => ({...floor, bots:[
    ...floor.bots.filter(id => !isObservedChild(state.bots[id])),
    ...Object.values(bots).filter(bot => isObservedChild(bot) && bot.floor === floor.id).map(bot => bot.id),
  ]}));
  return {...state, bots, floors, subagent_count:Object.values(bots).filter(isObservedChild).length};
}

// Pure position planning, independent of Three.js. Reuse each child's offset
// while the root moves; avoid other actors and keep positions inside the floor.
// Children stand beside their root, never on an allocated furniture seat.
export function followerSlots(children, roots, bounds, previous = new Map(), furniture = [], selectedId = null) {
  const slots = new Map(), occupied = [];
  const margin = 0.65;
  const inside = point => point.x >= bounds.minX + margin && point.x <= bounds.maxX - margin &&
    point.z >= bounds.minZ + margin && point.z <= bounds.maxZ - margin;
  const free = point => inside(point) && [...roots.values()].every(spot =>
    Math.hypot(point.x-spot.pos.x,point.z-spot.pos.z) >= 1.2) &&
    furniture.filter(spot=>spot?.pos).every(spot => Math.hypot(point.x-spot.pos.x,point.z-spot.pos.z) >= 0.8) &&
    occupied.every(spot => Math.hypot(point.x-spot.x,point.z-spot.z) >= 1.05);
  const candidates = [];
  // Prefer the open space in front of the parent; later rings fit a larger team.
  for (let ring=0;ring<8;ring++) {
    const radius=1.65+ring*1.25, count=8+ring*6;
    for (let index=0;index<count;index++) {
      const angle=index/count*Math.PI*2;
      candidates.push({dx:Math.sin(angle)*radius,dz:Math.cos(angle)*radius});
    }
  }
  const ordered=[...children].sort((a,b)=>String(a.agent_id).localeCompare(String(b.agent_id)));
  // Keep existing reservations before assigning new arrivals, so siblings stay put.
  const reserve = (bot, offset) => {
    const root=roots.get(bot.root_agent_id);
    if (!root || !offset) return false;
    const point={x:root.pos.x+offset.dx,y:0,z:root.pos.z+offset.dz};
    if (!free(point)) return false;
    const slot={...point,dx:offset.dx,dz:offset.dz,yaw:root.yaw || 0,root_agent_id:bot.root_agent_id};
    occupied.push(slot);slots.set(bot.id,slot);return true;
  };
  // A child chosen from the family menu must remain reachable in a crowded room.
  const selected=ordered.find(bot=>bot.id===selectedId);
  if(selected){
    const old=previous.get(selected.id);
    if(old?.root_agent_id===selected.root_agent_id)reserve(selected,old);
    if(!slots.has(selected.id))for(const offset of candidates)if(reserve(selected,offset))break;
  }
  for (const bot of ordered) {
    if(slots.has(bot.id))continue;
    const old=previous.get(bot.id);
    if (old?.root_agent_id===bot.root_agent_id) reserve(bot,old);
  }
  for (const bot of ordered) if (!slots.has(bot.id)) {
    for (const offset of candidates) if (reserve(bot,offset)) break;
  }
  return slots;
}
