// Shared, deterministic fallbacks for cached/older snapshots. Python owns saved seats.
export const agentKey=bot=>String(bot.agent_id||bot.id);
export function deskLabel(id){
  if(id==='gf-ceo-001')return 'CEO Room · 行政工作位';
  const main=/^gf-main-(\d{3})$/.exec(id||'');if(main)return '休息區 · '+Number(main[1])+' 號休息位';
  const office=/^gf-office-([1-5])-(\d{3})$/.exec(id||'');return office?'辦公室 '+office[1]+' · '+Number(office[2])+' 號工作台':'';
}
export function requiredMainSeats(bots){
  bots=bots.filter(bot=>!bot.is_subagent);
  return Math.max(bots.length,...bots.map(bot=>/^gf-main-(\d{3})$/.exec(bot.ground_desk_id||'')).filter(Boolean).map(match=>Math.min(400,Number(match[1]))));
}
export function resolveSeats(bots,seats){
  bots=bots.filter(bot=>!bot.is_subagent);
  const known=new Map(seats.map(seat=>[seat.id,seat])),assigned=new Map(),occupied=new Set();
  const ordered=[...bots].sort((a,b)=>agentKey(a).localeCompare(agentKey(b)));
  for(const bot of ordered){
    const seat=known.get(bot.ground_desk_id);
    if(seat&&!occupied.has(seat.id)){assigned.set(bot.id,seat);occupied.add(seat.id);}
  }
  const available=seats.filter(seat=>seat.id.startsWith('gf-main-')&&!occupied.has(seat.id));
  for(const bot of ordered)if(!assigned.has(bot.id)&&available.length){const seat=available.shift();assigned.set(bot.id,seat);}
  return assigned;
}
export function nearestDesk(point,seats,radius=1.75){
  let best=null,distance=radius*radius;
  for(const seat of seats){const next=(point.x-seat.x)**2+(point.z-seat.z)**2;if(next<distance){best=seat;distance=next;}}
  return best;
}
