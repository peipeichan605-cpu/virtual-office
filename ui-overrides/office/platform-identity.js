// Visual identity follows explicit registration metadata, never a display name.
export const PLATFORM_NAMES=Object.freeze({codex:'Codex',grok_bot:'Grok Bot',cursor:'Cursor',hermes:'Hermes'});
const MODES=Object.freeze({background_queue:'codex',hermes_acp:'hermes',cursor_cloud:'cursor',grok_webhook:'grok_bot'});
export function visualPlatform(bot={}){
  const provider=typeof bot.provider==='string'?bot.provider.trim().toLowerCase():'';
  if(Object.hasOwn(PLATFORM_NAMES,provider))return provider;
  const mode=bot.command_capability?.mode;
  if((!provider||provider==='generic')&&typeof mode==='string'&&Object.hasOwn(MODES,mode))return MODES[mode];
  return null;
}
