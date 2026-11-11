import {visualPlatform} from '../platform-identity.js';
import {paletteForBot} from '../platform-palette.js';
import {grokAvatarPlan,GROK_AVATAR_ID} from '../grok-avatar.js';
import {colorOriginalGrok} from './grok-colors.js';

// Grok Bot retains the native Office avatar/GLB chain. Other known platforms
// keep their silhouette with stable per-agent paint. Fence every async native load.
export function attachPlatformModel(actor,bot,{createPlatformModel,loadModel,generateBlob,colorGrokModel=colorOriginalGrok}){
  let current=null;
  const apply=(b)=>{
    if(actor.dead||actor.retiring)return;
    const platform=visualPlatform(b);
    const palette=paletteForBot(b);
    const custom=platform&&platform!=='grok_bot';
    const grok=platform==='grok_bot'?grokAvatarPlan(b):null;
    const grokBodyColor=grok?.asset===GROK_AVATAR_ID?(grok.bodyColor||palette.shell):null;
    const key=custom?'platform:'+platform+'|'+palette.key:grok?'grok:'+grok.asset+'|'+JSON.stringify(grok.look)+'|'+(grokBodyColor||'explicit'):`native:${platform||'generic'}:${b.avatar||b.id}|${b.has_glb??''}|${b.color||''}|${JSON.stringify(b.look||null)}`;
    if(key===current)return;
    current=key;actor.visualPlatform=platform;
    if(custom){actor.setModel(createPlatformModel(platform,{palette}));return;}
    const asset=grok?grok.asset:(b.has_glb===false?null:(b.avatar||b.id));
    Promise.resolve(asset?loadModel(asset):null).then(model=>{
      if(actor.dead||actor.retiring||current!==key)return;
      if(model){const clone=model.clone(true);if(grokBodyColor)colorGrokModel(clone,grokBodyColor);actor.setModel(clone);return;}
      const look=grok?grok.look:(b.look||{});
      actor.setModel(generateBlob({shape:look.shape,color:grokBodyColor||look.color||b.color,accessory:look.accessory,seed:grok?.asset||b.id}));
    });
  };
  apply(bot);
  const update=actor.update.bind(actor);
  actor.update=(b,spot,view)=>{apply(b);return update(b,spot,view);};
}
