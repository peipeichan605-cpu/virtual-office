import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

// Execute the actual scene integration functions with observable renderer/post
// boundaries. Loading the whole scene module would start GLTF/network loading.
const scene = await readFile(new URL('./public/office/scene/scene.js', import.meta.url),'utf8');
const start=scene.indexOf('export function graphicsRenderLimits('),end=scene.indexOf('\nfunction resize(',start);
assert.ok(start>=0&&end>start);
const compile=new Function('deps', `
  let graphicsSettings;
  const {shouldDraw,idleRender,wakeScene,renderer,post,container,devicePixelRatio,resize,publishGraphicsStatus,location} = deps;
  ${scene.slice(start,end).replaceAll('export function','function')}
  return {graphicsRenderLimits,graphicsPixelRatio,applyGraphicsSettings,getSettings:()=>graphicsSettings};
`);
const fixture=(query='')=>{
  const events=[],statuses=[];
  const renderer={ratio:1.5,capabilities:{maxTextureSize:8192,maxRenderbufferSize:4096},getPixelRatio(){return this.ratio;},setPixelRatio(value){this.ratio=value;events.push(['ratio',value]);}};
  const idleRender={enabled:true,idle:true};
  const deps={renderer,idleRender,devicePixelRatio:2,container:{clientWidth:1920,clientHeight:1080},location:{search:query},
    shouldDraw:{setFps:fps=>events.push(['fps',fps])},wakeScene:reason=>{idleRender.idle=false;events.push(['wake',reason]);},
    resize:()=>events.push(['resize',renderer.ratio]),publishGraphicsStatus:status=>statuses.push(status),
    post:{setAntialias:requested=>{events.push(['antialias',requested]);return {requestedAntialias:requested,effectiveAntialias:requested,maxSamples:4,availableAntialias:[1,2,4]};}}};
  return {...compile(deps),deps,events,statuses};
};

test('resolution choices preserve the native default and scale only the drawing buffer',()=>{
  const {graphicsPixelRatio:ratio}=fixture();
  assert.equal(ratio(2,1,1920,1080),1.5);
  assert.equal(ratio(2,.75,1920,1080),1.125);
  assert.equal(ratio(2,1.25,1920,1080),1.875);
  assert.equal(ratio(1,1.25,1920,1080),1.25);
  assert.equal(ratio(NaN,1,1920,1080),1);
  for(const invalid of [0,-1,NaN,Infinity])assert.throws(()=>ratio(2,invalid,100,100),RangeError);
});

test('physical framebuffer dimensions never exceed texture or renderbuffer limits',()=>{
  const {graphicsPixelRatio:ratio}=fixture();
  for(const [width,height,limits] of [[6000,3000,{maxTextureSize:8192,maxRenderbufferSize:1024}],[600,9000,{maxTextureSize:2048,maxRenderbufferSize:4096}]]){
    const value=ratio(2,1.25,width,height,limits),limit=Math.min(limits.maxTextureSize,limits.maxRenderbufferSize);
    assert.ok(width*value<=limit+1e-9);assert.ok(height*value<=limit+1e-9);
  }
});

test('live graphics changes update FPS, scene-only AA, scale and idle and wake immediately',()=>{
  const f=fixture(),settings={fps:90,antialias:2,scale:1.25,idle:false};f.applyGraphicsSettings(settings);
  assert.equal(f.getSettings(),settings);assert.equal(f.deps.renderer.ratio,1.875);assert.equal(f.deps.idleRender.enabled,false);assert.equal(f.deps.idleRender.idle,false);
  assert.deepEqual(f.events,[['fps',90],['wake','graphics'],['ratio',1.875],['antialias',2],['resize',1.875]]);
  assert.equal(f.statuses.at(-1).requestedFps,90);assert.equal(f.statuses.at(-1).effectiveScale,1.25);assert.equal(f.statuses.at(-1).message,'');
  f.applyGraphicsSettings({fps:30,antialias:4,scale:1,idle:true});assert.equal(f.deps.renderer.ratio,1.5);assert.equal(f.deps.idleRender.enabled,true);
});

test('reported fallback reflects device AA support and capped resolution, with normalized scale',()=>{
  const f=fixture();f.deps.container.clientWidth=10000;
  f.deps.post.setAntialias=requested=>({requestedAntialias:requested,effectiveAntialias:1,availableAntialias:[1],maxSamples:0});
  f.applyGraphicsSettings({fps:60,antialias:4,scale:1.25,idle:true});const result=f.statuses.at(-1);
  assert.equal(result.effectiveAntialias,1);assert.equal(result.effectiveScale,Number((4096/10000/1.5).toFixed(3)));
  assert.match(result.message,/×4.*×1/);assert.match(result.message,/裝置上限/);
});

test('the explicit idle debug override survives settings updates and is honestly described',()=>{
  const f=fixture('?idle_render=0');f.applyGraphicsSettings({fps:60,antialias:2,scale:.75,idle:true});
  assert.equal(f.deps.idleRender.enabled,false);assert.match(f.statuses.at(-1).message,/測試連結/);
});

test('actual GL renderbuffer limit is wired into scale when Three capabilities omit it',()=>{
  const f=fixture();delete f.deps.renderer.capabilities.maxRenderbufferSize;f.deps.renderer.capabilities.maxTextureSize=16384;f.deps.container.clientWidth=4000;
  f.deps.renderer.getContext=()=>({MAX_RENDERBUFFER_SIZE:123,getParameter:key=>{assert.equal(key,123);return 4096;}});
  f.applyGraphicsSettings({fps:30,antialias:4,scale:1.25,idle:true});
  assert.equal(f.deps.renderer.ratio,4096/4000);assert.match(f.statuses.at(-1).message,/裝置上限/);
  assert.equal(f.graphicsRenderLimits(f.deps.renderer).maxRenderbufferSize,4096);
});
