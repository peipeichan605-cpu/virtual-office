import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFile} from 'node:fs/promises';
import {dirname,resolve} from 'node:path';
import {pathToFileURL,fileURLToPath} from 'node:url';

// Use an already installed Three.js fixture, without installing dependencies.
let threeRoot=null;
const testRoot=dirname(fileURLToPath(import.meta.url));
for(const base of [process.env.OFFICE_THREE_TEST_ROOT,testRoot,process.cwd()].filter(Boolean)){
  try{const require=createRequire(resolve(base,'package.json'));threeRoot=resolve(dirname(require.resolve('three')),'..');break;}catch{}
}

test('MSAA scene and single-sample post pipeline with real Three.js passes',{skip:!threeRoot},async t=>{
  const threeUrl=pathToFileURL(resolve(threeRoot,'build/three.module.js')).href;
  const THREE=await import(threeUrl);
  const dataUrl=source=>'data:text/javascript;base64,'+Buffer.from(source).toString('base64');
  const mapThree=source=>source.replaceAll("from 'three';",`from '${threeUrl}';`)
    .replace(/from 'three\/addons\/([^']+)';/g,(_,name)=>`from '${pathToFileURL(resolve(threeRoot,'examples/jsm',name)).href}';`);
  const studio=mapThree(await readFile(resolve(testRoot,'public/office/scene/studio.js'),'utf8'))
    .replace("from './aura-cache.js';",`from '${pathToFileURL(resolve(testRoot,'public/office/scene/aura-cache.js')).href}';`)
    .replace("from './gaussian-kernel.js';",`from '${pathToFileURL(resolve(testRoot,'public/office/scene/gaussian-kernel.js')).href}';`);
  const source=mapThree(await readFile(resolve(testRoot,'public/office/scene/post.js'),'utf8'))
    .replace("from './studio.js';",`from '${dataUrl(studio)}';`);
  const {createPost}=await import(dataUrl(source));

  class Renderer {
    constructor(scene){
      this.scene=scene;this.target={name:'external'};this.autoClear=true;this.autoClearColor=true;this.autoClearDepth=true;this.autoClearStencil=true;
      this.color=new THREE.Color('#123456');this.alpha=.6;this.shadowMap={enabled:true};this.domElement=new EventTarget();
      this.toneMapping=THREE.ACESFilmicToneMapping;this.toneMappingExposure=1.05;this.outputColorSpace=THREE.SRGBColorSpace;
      this.ratio=1.5;this.calls=[];this.prior=this.target;
    }
    getSize(out){return out.set(640,360);}getPixelRatio(){return this.ratio;}
    getRenderTarget(){return this.target;}setRenderTarget(target){this.target=target;}
    getClearColor(out){return out.copy(this.color);}getClearAlpha(){return this.alpha;}
    setClearColor(color,alpha){this.color.set(color);this.alpha=alpha;}
    clear(){if(this.target?.texture)this.target.texture.trace=[];}clearDepth(){}
    render(object){
      let kind,input;
      if(object===this.scene){kind=object.overrideMaterial?'mask':'scene';if(object.matrixWorldAutoUpdate)object.updateMatrixWorld();}
      else if(object.material?.name==='BotHQTiltShift'){
        kind=object.material.uniforms.dir.value.x?'horizontal':'vertical';input=object.material.uniforms.tDiffuse.value;
      }else if(object.material?.name==='OutputShader'){kind='output';input=object.material.uniforms.tDiffuse.value;}
      else if(object.material?.uniforms.tHold){kind='studio';input=object.material.uniforms.tDiffuse.value;}
      else {kind='aura';input=object.material?.uniforms.tMap?.value;}
      if(input&&this.target?.texture)assert.notEqual(input,this.target.texture,'a fullscreen pass must not sample its output attachment');
      this.calls.push({kind,target:this.target,input,autoClear:this.autoClear});
      if(this.fail===kind)throw Error('injected '+kind+' failure');
      const trace=[...(input?.trace||[]),kind];
      if(this.target?.texture)this.target.texture.trace=trace;
      if(kind==='studio')this.finalTrace=trace;
    }
  }
  const fixture=(query='')=>{
    globalThis.location={search:query};
    const scene=new THREE.Scene(),camera=new THREE.OrthographicCamera(-5,5,5,-5,.1,100);camera.position.z=10;
    scene.add(new THREE.Mesh(new THREE.BoxGeometry(),new THREE.MeshBasicMaterial()));
    const renderer=new Renderer(scene),post=createPost(renderer,scene,camera);
    return {scene,camera,renderer,post};
  };
  const draw=f=>f.post.render(.37,.033,.8);
  const dispose=f=>{for(const pass of f.post.composer.passes)pass.dispose?.();f.post.composer.dispose();};

  await t.test('only native scene uses four samples and effects retain the original input order',()=>{
    const f=fixture();draw(f);
    const main=f.renderer.calls.filter(x=>x.kind==='scene');assert.equal(main.length,1);assert.equal(main[0].target.samples,4);
    assert.equal(main[0].target.texture.type,THREE.HalfFloatType);assert.equal(main[0].target.depthBuffer,true);
    for(const call of f.renderer.calls)if(!['scene','mask'].includes(call.kind)&&call.target?.texture)assert.equal(call.target.samples,0,call.kind+' target must be single sample');
    assert.deepEqual(f.renderer.finalTrace,['scene','horizontal','vertical','output','studio']);
    assert.equal(f.renderer.calls.filter(x=>['horizontal','vertical','output','studio'].includes(x.kind)).length,4,'no additional copy quad');
    assert.equal(f.post.composer.renderTarget1.depthBuffer,false);assert.equal(f.post.composer.renderTarget2.depthBuffer,false);
    assert.equal(f.renderer.target,f.renderer.prior);assert.equal(f.renderer.autoClear,true);
    assert.equal(f.renderer.color.getHex(),new THREE.Color('#123456').getHex());assert.equal(f.renderer.alpha,.6);
    dispose(f);
  });

  await t.test('repeated frames always read the newly drawn scene instead of a stale ping-pong input',()=>{
    const f=fixture();
    for(let i=0;i<4;i++){
      f.renderer.calls=[];draw(f);const scene=f.renderer.calls.find(x=>x.kind==='scene');const horizontal=f.renderer.calls.find(x=>x.kind==='horizontal');
      assert.equal(horizontal.input,scene.target.texture);assert.deepEqual(f.renderer.finalTrace,['scene','horizontal','vertical','output','studio']);
    }
    const passes=f.post.composer.passes;assert.equal(passes[1].uniforms.focus.value,.37);assert.equal(passes[2].uniforms.focus.value,.37);
    assert.equal(passes[1].uniforms.strength.value,1.05*1.5*.8);dispose(f);
  });

  await t.test('tilt-shift disabled reads the resolved native scene directly with no copy pass',()=>{
    const f=fixture('?tiltshift=0');draw(f);
    const main=f.renderer.calls.find(x=>x.kind==='scene');const output=f.renderer.calls.find(x=>x.kind==='output');
    assert.equal(output.input,main.target.texture);assert.deepEqual(f.renderer.finalTrace,['scene','output','studio']);
    assert.equal(f.renderer.calls.filter(x=>['horizontal','vertical','output','studio'].includes(x.kind)).length,2);
    dispose(f);
  });

  await t.test('first enabled consumer routing survives temporary pass toggles',()=>{
    const f=fixture(),passes=f.post.composer.passes;
    passes[1].enabled=false;draw(f);assert.deepEqual(f.renderer.finalTrace,['scene','vertical','output','studio']);
    passes[2].enabled=false;draw(f);assert.deepEqual(f.renderer.finalTrace,['scene','output','studio']);
    passes[1].enabled=passes[2].enabled=true;draw(f);assert.deepEqual(f.renderer.finalTrace,['scene','horizontal','vertical','output','studio']);dispose(f);
  });

  await t.test('capture resize and return to live dimensions resize the dedicated scene and all post buffers',()=>{
    const f=fixture();
    for(const [ratio,width,height] of [[1.5,640,360],[2,1600,900],[1.5,640,360]]){
      f.renderer.ratio=ratio;f.post.setSize(width,height);f.renderer.calls=[];draw(f);
      const main=f.renderer.calls.find(x=>x.kind==='scene').target;
      assert.equal(main.width,width*ratio);assert.equal(main.height,height*ratio);assert.equal(main.samples,4);
      for(const target of [f.post.composer.renderTarget1,f.post.composer.renderTarget2]){
        assert.equal(target.width,width*ratio);assert.equal(target.height,height*ratio);assert.equal(target.samples,0);
      }
      assert.equal(f.post.composer.passes[1].uniforms.dir.value.x,1/(width*ratio));assert.equal(f.post.composer.passes[2].uniforms.dir.value.y,1/(height*ratio));
      assert.deepEqual(f.renderer.finalTrace,['scene','horizontal','vertical','output','studio']);
    }
    f.post.composer.setPixelRatio(2);f.post.composer.setSize(100,50);f.renderer.calls=[];draw(f);
    const main=f.renderer.calls.find(x=>x.kind==='scene').target;assert.equal(main.width,200);assert.equal(main.height,100);dispose(f);
  });

  await t.test('scene and post failures restore the caller target and autoClear before the next frame',()=>{
    for(const failure of ['scene','horizontal','vertical','output','studio']){
      const f=fixture();f.renderer.fail=failure;assert.throws(()=>draw(f),/injected/);
      assert.equal(f.renderer.target,f.renderer.prior,failure);assert.equal(f.renderer.autoClear,true,failure);assert.equal(f.scene.overrideMaterial,null,failure);
      f.renderer.fail=null;draw(f);assert.deepEqual(f.renderer.finalTrace,['scene','horizontal','vertical','output','studio']);dispose(f);
    }
  });

  await t.test('composer disposal also releases the dedicated multisample target',()=>{
    const f=fixture();draw(f);const main=f.renderer.calls.find(x=>x.kind==='scene').target;let disposed=0;main.addEventListener('dispose',()=>disposed++);
    dispose(f);assert.equal(disposed,1);
  });

  await t.test('legacy opt-in retains original multisampled vertical post pass for controlled A/B',()=>{
    const f=fixture('?post_msaa=legacy');draw(f);
    const main=f.renderer.calls.find(x=>x.kind==='scene'),vertical=f.renderer.calls.find(x=>x.kind==='vertical');
    assert.equal(main.target.samples,4);assert.equal(vertical.target.samples,4);
    assert.deepEqual(f.renderer.finalTrace,['scene','horizontal','vertical','output','studio']);dispose(f);
  });
  await t.test('live AA switches preserve pipeline identities and dispose only changed scene storage',()=>{
    const f=fixture();draw(f);const sceneTarget=f.renderer.calls.find(x=>x.kind==='scene').target;
    const texture=sceneTarget.texture,passObjects=[...f.post.composer.passes],postTargets=[f.post.composer.renderTarget1,f.post.composer.renderTarget2];let released=0;
    sceneTarget.addEventListener('dispose',()=>released++);
    for(const requested of [1,2,4,4]){
      const status=f.post.setAntialias(requested);assert.equal(status.effectiveAntialias,requested);
      f.renderer.calls=[];draw(f);const scene=f.renderer.calls.find(x=>x.kind==='scene').target;
      assert.equal(scene,sceneTarget);assert.equal(scene.texture,texture);assert.equal(scene.samples,requested===1?0:requested);
      assert.deepEqual(f.post.composer.passes,passObjects);assert.deepEqual([f.post.composer.renderTarget1,f.post.composer.renderTarget2],postTargets);
      assert.deepEqual(f.renderer.finalTrace,['scene','horizontal','vertical','output','studio']);
    }
    assert.equal(released,3,'reapplying unchanged AA does not dispose the scene framebuffer');
    f.post.setSize(1920,1080);f.renderer.calls=[];draw(f);assert.equal(sceneTarget.samples,4);assert.equal(sceneTarget.width,2880);dispose(f);
  });

  await t.test('format-specific colour/depth sample counts negotiate down and report effective AA',()=>{
    const f=fixture();f.renderer.capabilities={maxSamples:8};let colour=[4,2],depth=[4];
    f.renderer.getContext=()=>({RENDERBUFFER:1,RGBA16F:2,DEPTH_COMPONENT24:3,SAMPLES:4,getInternalformatParameter:(_,format)=>format===2?colour:depth});
    let status=f.post.setAntialias(2);assert.equal(status.requestedAntialias,2);assert.equal(status.effectiveAntialias,1);assert.deepEqual(status.availableAntialias,[1,4]);
    f.renderer.calls=[];draw(f);assert.equal(f.renderer.calls.find(x=>x.kind==='scene').target.samples,0);
    depth=[4,2];status=f.post.setAntialias(2);assert.equal(status.effectiveAntialias,2,'context restoration renegotiates unchanged request');
    f.renderer.capabilities.maxSamples=2;status=f.post.setAntialias(4);assert.equal(status.effectiveAntialias,2);assert.deepEqual(status.availableAntialias,[1,2]);
    colour=[];depth=[];status=f.post.setAntialias(4);assert.equal(status.effectiveAntialias,1);
    f.renderer.getContext=()=>({getInternalformatParameter(){throw Error('context lost');}});assert.equal(f.post.setAntialias(4).effectiveAntialias,1);
    assert.throws(()=>f.post.setAntialias(3),RangeError);dispose(f);
  });

  await t.test('legacy debug route also changes scene samples without rebuilding effects',()=>{
    const f=fixture('?post_msaa=legacy');const target=f.post.composer.renderTarget1;
    assert.equal(f.post.setAntialias(1).effectiveAntialias,1);draw(f);assert.equal(f.renderer.calls.find(x=>x.kind==='scene').target,target);assert.equal(target.samples,0);
    f.post.setAntialias(2);assert.equal(target.samples,2);f.renderer.calls=[];draw(f);assert.deepEqual(f.renderer.finalTrace,['scene','horizontal','vertical','output','studio']);dispose(f);
  });

  await t.test('fractional scale on odd CSS dimensions uses the same integer grid for scene and post',()=>{
    const f=fixture();f.renderer.ratio=1.875;f.post.setSize(1165,837);f.renderer.calls=[];draw(f);
    const width=Math.floor(1165*1.875),height=Math.floor(837*1.875),target=f.renderer.calls.find(x=>x.kind==='scene').target;
    assert.equal(target.width,width);assert.equal(target.height,height);
    for(const target of [f.post.composer.renderTarget1,f.post.composer.renderTarget2]){assert.equal(target.width,width);assert.equal(target.height,height);}
    assert.equal(f.post.composer.passes[1].uniforms.dir.value.x,1/width);assert.equal(f.post.composer.passes[2].uniforms.dir.value.y,1/height);
    const studio=f.post.composer.passes.at(-1);assert.equal(studio.size.x,width);assert.equal(studio.size.y,height);dispose(f);
  });

});
