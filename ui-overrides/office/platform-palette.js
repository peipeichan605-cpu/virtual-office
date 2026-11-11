import {visualPlatform} from './platform-identity.js';

// Fixed, coordinated paint sets. Brand artwork and live status colors are separate.
const sets={
  grok_bot:[
    ['orange','#FD690F','#FD690F','#ffffff','#262626','#333333','#ffffff'],
    ['mint','#4BBF83','#4BBF83','#ffffff','#262626','#333333','#ffffff'],
    ['sky','#4D9CF6','#4D9CF6','#ffffff','#262626','#333333','#ffffff'],
    ['coral','#EF6B78','#EF6B78','#ffffff','#262626','#333333','#ffffff'],
    ['lilac','#A579DB','#A579DB','#ffffff','#262626','#333333','#ffffff'],
    ['gold','#E3AE36','#E3AE36','#ffffff','#262626','#333333','#ffffff'],
  ],
  codex:[
    ['sky','#c1def3','#679fd0','#edf6fc','#24364a','#97b2c6','#f7f4e9'],
    ['mint','#bde2ce','#509c83','#edf7ed','#23433d','#89b7a3','#f7f4e9'],
    ['lilac','#d7c4ec','#9974c3','#f6edfc','#3c3054','#b3a1c7','#f7f4e9'],
    ['apricot','#f0c9a1','#c98c59','#fff3e7','#533c30','#c6a17f','#f7f4e9'],
    ['rose','#efc3d0','#c47e97','#fceef2','#50343f','#c5a0ae','#f7f4e9'],
    ['aqua','#b5e0e4','#4c9fa9','#eefbfc','#24444c','#8db7c0','#f7f4e9'],
  ],
  cursor:[
    ['coral','#eeb7a3','#cb775b','#f8e6dc','#303239','#b88e80','#fff7ec'],
    ['sage','#bfd0ae','#769563','#eff4e9','#303a30','#8fa083','#fff7ec'],
    ['lavender','#c9bce4','#8975b3','#f1ebf9','#373144','#9990ad','#fff7ec'],
    ['saffron','#ead29b','#ba974c','#fcf3df','#413b2d','#b6a17b','#fff7ec'],
    ['denim','#b5cbe2','#708eaf','#eaf2fb','#2c3542','#8298af','#fff7ec'],
    ['seafoam','#acd6ce','#579d90','#eaf7f2','#2c3c3a','#7eaaa0','#fff7ec'],
  ],
  hermes:[
    ['royal','#3159cf','#6380dd','#eee7d5','#22336c','#bc9a58','#faf5e7'],
    ['teal','#298d91','#53aca8','#dcefe5','#1e4a51','#c4a063','#faf5e7'],
    ['plum','#8255ae','#a483c4','#efe1f3','#44315e','#c39f66','#fbf5ed'],
    ['sunset','#cd704f','#e29266','#f4e2c7','#6b3d34','#bf9b56','#fff5df'],
    ['forest','#3f8a6b','#71aa7c','#e2edcf','#284b3c','#bea366','#faf6e7'],
    ['berry','#b05a87','#ce83a4','#f2e1eb','#593147','#c8a06b','#fcf5ed'],
  ],
};
export const PLATFORM_PALETTES=Object.freeze(Object.fromEntries(Object.entries(sets).map(([platform,rows])=>[platform,Object.freeze(rows.map(([name,shell,accent,secondary,dark,metal,wing])=>Object.freeze({key:platform+'-'+name,shell,accent,secondary,dark,metal,wing})))])));
export function paletteForBot(bot={}){
  const palettes=PLATFORM_PALETTES[visualPlatform(bot)];
  if(!palettes)return null;
  // agent_id survives display-name changes; older snapshots fall back to their ID.
  const identity=String(bot.agent_id||bot.id||'unassigned');let hash=2166136261;
  for(let i=0;i<identity.length;i++){hash^=identity.charCodeAt(i);hash=Math.imul(hash,16777619);}
  return palettes[(hash>>>0)%palettes.length];
}
