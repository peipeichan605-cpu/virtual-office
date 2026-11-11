// The author's actual authored Grok Bot model (pinned upstream), not a guessed
// green sphere. Its fallback descriptor comes from the same demo-looks.json.
export const GROK_AVATAR_ID='grok-bot';
export const GROK_DEFAULT_LOOK=Object.freeze({shape:'sphere',color:'#FC6A14',accessory:'stem'});
export function isLegacyGrokPlaceholder(look){
  return look?.shape==='sphere'&&String(look.color||'').toUpperCase()==='#6CAC72'&&look.accessory==='glasses';
}
function isDefaultLook(look){
  return !look||(look.shape==='sphere'&&['#FC6A14','#FD690F'].includes(String(look.color||'').toUpperCase())&&look.accessory==='stem');
}
export function grokAvatarPlan(bot={}){
  const descriptor=bot.look;
  // Only a known synthetic legacy default is migrated. Fresh, explicitly reported
  // looks may still deliberately use that color/accessory combination.
  const custom=descriptor&&!isDefaultLook(descriptor)&&!(isLegacyGrokPlaceholder(descriptor)&&bot.look_origin!=='reported');
  const look=custom?{shape:descriptor.shape,color:descriptor.color||bot.color,accessory:descriptor.accessory}:GROK_DEFAULT_LOOK;
  const personal=bot.has_glb===true&&(bot.avatar||bot.id);
  if(personal&&personal!==GROK_AVATAR_ID)return {asset:personal,look};
  if(custom&&look.shape==='sphere'&&look.accessory==='stem'&&/^#[0-9a-f]{6}$/i.test(look.color||''))return {asset:GROK_AVATAR_ID,look,bodyColor:look.color};
  return custom?{asset:null,look}:{asset:GROK_AVATAR_ID,look:GROK_DEFAULT_LOOK};
}
