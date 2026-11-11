import test from 'node:test';
import assert from 'node:assert/strict';
import {grokAvatarPlan,GROK_DEFAULT_LOOK} from './public/office/grok-avatar.js';
import {visualPlatform} from './public/office/platform-identity.js';
import {paletteForBot,PLATFORM_PALETTES} from './public/office/platform-palette.js';
import {attachPlatformModel} from './public/office/scene/platform-controller.js';

const turn=()=>new Promise(resolve=>setImmediate(resolve));
function fixture(loadModel=async()=>null){
  const models=[],actor={dead:false,retiring:false,update(){},setModel(model){models.push(model);}};
  const factories={createPlatformModel:platform=>({platform}),loadModel,generateBlob:look=>({look}),colorGrokModel:model=>model};
  return {actor,models,factories};
}
test('all four registered platforms keep identity across names, states and conflicting capabilities',()=>{
  for(const provider of ['codex','grok_bot','cursor','hermes'])for(const name of ['任意名字','Codex Hermes Cursor Grok']){
    assert.equal(visualPlatform({provider,name,status:'error',command_capability:{mode:'agent_inbox'}}),provider);
  }
  assert.equal(visualPlatform({provider:'codex',command_capability:{mode:'hermes_acp'}}),'codex');
});
test('explicit native modes can identify a generic runtime; names and unknown values never invent a platform',()=>{
  for(const [mode,platform] of Object.entries({background_queue:'codex',hermes_acp:'hermes',cursor_cloud:'cursor',grok_webhook:'grok_bot'}))assert.equal(visualPlatform({provider:'generic',command_capability:{mode}}),platform);
  for(const bot of [{name:'Codex'},{provider:'generic',command_capability:{mode:'agent_inbox'}},{provider:'crouton',command_capability:{mode:'hermes_acp'}},{provider:'unknown'},{provider:'__proto__'},{command_capability:{mode:'__proto__'}},{command_capability:{mode:{}}}])assert.equal(visualPlatform(bot),null);
});
test('provider changes replace the model while status, names and legacy colors do not rebuild platform models',()=>{
  const f=fixture(),bot={id:'same',provider:'codex'};attachPlatformModel(f.actor,bot,f.factories);
  f.actor.update({...bot,status:'working',name:'Renamed',color:'#ff0000'},null,{});
  f.actor.update({...bot,provider:'cursor'},null,{});
  assert.deepEqual(f.models,[{platform:'codex'},{platform:'cursor'}]);
});
test('a delayed legacy GLB cannot overwrite the newly selected platform model',async()=>{
  let finish,clones=0;const pending=new Promise(resolve=>finish=resolve),f=fixture(()=>pending);
  attachPlatformModel(f.actor,{id:'same',provider:'generic'},f.factories);
  f.actor.update({id:'same',provider:'hermes'},null,{});
  finish({clone(){clones++;return {};}});await turn();
  assert.equal(clones,0);assert.deepEqual(f.models,[{platform:'hermes'}]);
});
test('a retired or dead actor cannot receive a delayed model',async()=>{
  for(const flag of ['retiring','dead']){let finish;const f=fixture(()=>new Promise(resolve=>finish=resolve));
    attachPlatformModel(f.actor,{id:'old',provider:'generic'},f.factories);f.actor[flag]=true;finish(null);await turn();assert.deepEqual(f.models,[]);
  }
});
test('generic agents retain personal looks and a late look cannot undo a newer one',async()=>{
  const f=fixture();attachPlatformModel(f.actor,{id:'generic',provider:'generic',has_glb:false,look:{shape:'sphere',color:'#123456',accessory:'glasses'}},f.factories);
  await turn();assert.equal(f.models[0].look.shape,'sphere');assert.equal(f.models[0].look.color,'#123456');
  f.actor.update({id:'generic',provider:'generic',has_glb:false,look:{shape:'rounded_cube',color:'#abcdef'}},null,{});await turn();assert.equal(f.models[1].look.shape,'rounded_cube');
});

test('Grok Bot uses the original Office look chain rather than a custom platform model',async()=>{
  const f=fixture();const bot={id:'native-grok',provider:'grok_bot',has_glb:false,look:{shape:'flower',color:'#c27d55',accessory:'glasses'}};
  attachPlatformModel(f.actor,bot,f.factories);await turn();
  assert.equal(f.actor.visualPlatform,'grok_bot');assert.deepEqual(f.models,[{look:{shape:'flower',color:'#c27d55',accessory:'glasses',seed:'native-grok'}}]);
  f.actor.update({...bot,look:{shape:'star',color:'#4aab91'}},null,{});await turn();assert.equal(f.models[1].look.shape,'star');
});
test('Grok native GLB requests are fenced when the provider changes',async()=>{
  let finish,clones=0;const f=fixture(()=>new Promise(resolve=>finish=resolve));
  attachPlatformModel(f.actor,{id:'native-grok',provider:'grok_bot'},f.factories);f.actor.update({id:'native-grok',provider:'cursor'},null,{});
  finish({clone(){clones++;return {};}});await turn();assert.equal(clones,0);assert.deepEqual(f.models,[{platform:'cursor'}]);
});
test('agent identity keeps its palette across names, work status and snapshot order',()=>{
  const first={id:'old-name',agent_id:'codex:stable-identity',provider:'codex',status:'idle'};
  const palette=paletteForBot(first);assert.equal(paletteForBot({...first,id:'new-name',name:'renamed',status:'error',color:'#ff0000'}),palette);
  const f=fixture(),received=[];f.factories.createPlatformModel=(platform,options)=>{received.push(options.palette.key);return {platform};};
  attachPlatformModel(f.actor,first,f.factories);f.actor.update({...first,status:'working',name:'new-name'},null,{});assert.deepEqual(received,[palette.key]);
  assert.ok(paletteForBot({provider:'grok_bot',id:'grok'}).key.startsWith('grok_bot-'));assert.equal(paletteForBot({provider:'generic',id:'other'}),null);
});
test('all four platforms offer six coordinated colors across different stable agent IDs',()=>{
  for(const provider of ['codex','grok_bot','cursor','hermes']){
    const found=new Set(Array.from({length:150},(_,i)=>paletteForBot({provider,agent_id:provider+':'+i}).key));assert.equal(found.size,6);
    assert.equal(PLATFORM_PALETTES[provider].length,6);
    for(const palette of PLATFORM_PALETTES[provider])for(const key of ['shell','accent','secondary','dark','metal','wing'])assert.match(palette[key],/^#[0-9a-f]{6}$/i);
  }
});

test('a Grok GLB becoming available replaces its generated native fallback',async()=>{
  let loads=0;const glb={clone(){return {nativeGLB:true};}},f=fixture(async()=>{loads++;return glb;});
  const bot={id:'grok-asset',provider:'grok_bot',has_glb:false,look:{shape:'sphere'}};
  attachPlatformModel(f.actor,bot,f.factories);await turn();assert.equal(loads,0);assert.equal(f.models[0].look.shape,'sphere');
  f.actor.update({...bot,has_glb:true},null,{});await turn();assert.equal(loads,1);assert.deepEqual(f.models[1],{nativeGLB:true});
});

test('Grok defaults to the actual authored grok-bot GLB even when a per-agent file is absent',async()=>{
  const requested=[],f=fixture(async id=>{requested.push(id);return {clone(){return {originalGrokGLB:true};}};});
  attachPlatformModel(f.actor,{id:'arbitrary-grok-name',provider:'grok_bot',has_glb:false},f.factories);await turn();
  assert.deepEqual(requested,['grok-bot']);assert.deepEqual(f.models,[{originalGrokGLB:true}]);
});
test('an unavailable original Grok GLB keeps its native shape and stem with the assigned color',async()=>{
  const f=fixture();attachPlatformModel(f.actor,{id:'grok',provider:'grok_bot',has_glb:false,look:{shape:'sphere',color:'#6CAC72',accessory:'glasses'}},f.factories);await turn();
  assert.deepEqual(f.models,[{look:{...GROK_DEFAULT_LOOK,color:paletteForBot({id:'grok',provider:'grok_bot'}).shell,seed:'grok-bot'}}]);
});
test('only the synthetic legacy Grok default is migrated; fresh explicit looks and personal assets survive',()=>{
  const look={shape:'sphere',color:'#6CAC72',accessory:'glasses'};
  assert.equal(grokAvatarPlan({look}).asset,'grok-bot');
  assert.deepEqual(grokAvatarPlan({look,look_origin:'reported'}),{asset:null,look});
  assert.equal(grokAvatarPlan({id:'custom',avatar:'own-grok',has_glb:true,look}).asset,'own-grok');
  const flower={shape:'flower',color:'#FF309B',accessory:'none'};
  assert.deepEqual(grokAvatarPlan({look:flower}),{asset:null,look:flower});
});

test('the original Grok GLB receives stable body-only paint after cloning and is not rebuilt for status or names',async()=>{
  const paints=[],f=fixture(async()=>({clone(){return {clonedOriginal:true};}}));f.factories.colorGrokModel=(model,color)=>{paints.push({model,color});return model;};
  const bot={id:'display-one',agent_id:'grok_bot:stable-one',provider:'grok_bot',has_glb:false};
  attachPlatformModel(f.actor,bot,f.factories);await turn();assert.equal(paints[0].color,paletteForBot(bot).shell);assert.deepEqual(paints[0].model,{clonedOriginal:true});
  f.actor.update({...bot,id:'renamed',name:'changed',status:'working'},null,{});await turn();assert.equal(paints.length,1);
});
test('explicit original-shaped Grok colors retain the original GLB while personal GLBs remain untouched',async()=>{
  const explicit={provider:'grok_bot',id:'explicit',look:{shape:'sphere',color:'#39A5C7',accessory:'stem'},look_origin:'reported'};
  assert.equal(grokAvatarPlan(explicit).asset,'grok-bot');assert.equal(grokAvatarPlan(explicit).bodyColor,'#39A5C7');
  const paints=[],f=fixture(async()=>({clone(){return {native:true};}}));f.factories.colorGrokModel=(model,color)=>{paints.push(color);return model;};attachPlatformModel(f.actor,explicit,f.factories);await turn();assert.deepEqual(paints,['#39A5C7']);
  f.actor.update({...explicit,has_glb:true,avatar:'personal-mesh'},null,{});await turn();assert.equal(paints.length,1);
});
