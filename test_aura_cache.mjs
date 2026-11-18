import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFile} from 'node:fs/promises';
import {dirname,resolve} from 'node:path';
import {pathToFileURL,fileURLToPath} from 'node:url';
import {AuraCache,withHidden} from './public/office/scene/aura-cache.js';

const matrix = () => ({elements:[1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1]});
const node = (options = {}) => ({visible:true,children:[],layers:{mask:1},matrixWorld:matrix(),...options});
const mesh = (options = {}) => node({isMesh:true,geometry:{attributes:{position:{version:0,count:3}},groups:[],drawRange:{start:0,count:3}},material:{visible:true,opacity:1},...options});
const camera = () => ({projectionMatrix:matrix(),matrixWorld:matrix(),matrixWorldInverse:matrix(),layers:{mask:1}});
const add = (parent,...objects) => {for (const object of objects) {parent.children.push(object);object.parent=parent;}return parent;};
const inspect = (cache,scene,view) => {const result=cache.inspect(scene,view);cache.commit();return result;};

test('unchanged static geometry is reused and removals release cache references',()=>{
  const cache=new AuraCache(),scene=node(),view=camera(),one=mesh(),two=mesh();add(scene,one,two);
  assert.equal(inspect(cache,scene,view).changed,true);
  for(let frame=0;frame<100;frame++)assert.equal(inspect(cache,scene,view).changed,false);
  scene.children.splice(0,1);assert.equal(inspect(cache,scene,view).changed,true);
  assert.equal(cache.records.has(one),false);assert.equal(cache.records.size,1);
});

test('camera, layers, parent visibility and scene shape changes invalidate static pixels',()=>{
  const cache=new AuraCache(),scene=node(),group=node(),one=mesh(),view=camera();add(scene,group);add(group,one);inspect(cache,scene,view);
  for(const change of [()=>view.projectionMatrix.elements[0]=2,()=>view.matrixWorld.elements[12]=3,()=>view.matrixWorldInverse.elements[13]=4,()=>view.layers.mask=3,()=>one.layers.mask=2,()=>one.matrixWorld.elements[14]=5,()=>one.geometry.drawRange.count=2,()=>one.geometry.groups.push({start:0,count:1,materialIndex:0}),()=>one.material.visible=false]){
    change();assert.equal(inspect(cache,scene,view).changed,true);
    assert.equal(inspect(cache,scene,view).changed,false);
  }
  group.visible=false;assert.equal(inspect(cache,scene,view).changed,true);assert.equal(cache.records.size,0);
  group.visible=true;assert.equal(inspect(cache,scene,view).changed,true);
  one.material.opacity=0;assert.equal(inspect(cache,scene,view).changed,false,'the existing white mask ignores source opacity');
});

test('attribute identity, interleaved buffers and instance updates invalidate even at version zero',()=>{
  const cache=new AuraCache(),scene=node(),one=mesh({isInstancedMesh:true,count:4,instanceMatrix:{version:0,count:4}}),view=camera();add(scene,one);inspect(cache,scene,view);
  one.geometry.attributes.position={version:0,count:3};assert.equal(inspect(cache,scene,view).changed,true);
  one.geometry.attributes.position={count:3,data:{version:0}};inspect(cache,scene,view);
  one.geometry.attributes.position.data.version++;assert.equal(inspect(cache,scene,view).changed,true);
  one.instanceMatrix.version++;assert.equal(inspect(cache,scene,view).changed,true);
  one.instanceMatrix={version:0,count:4};assert.equal(inspect(cache,scene,view).changed,true);
  one.count=3;assert.equal(inspect(cache,scene,view).changed,true);
  one.boundingSphere={center:{x:0,y:0,z:0},radius:2};inspect(cache,scene,view);
  one.boundingSphere.radius=4;assert.equal(inspect(cache,scene,view).changed,true);
});

test('an interrupted signature scan invalidates pixels even after updating an earlier record',()=>{
  const cache=new AuraCache(),scene=node(),view=camera(),one=mesh(),two=mesh();add(scene,one,two);inspect(cache,scene,view);
  const attributes=two.geometry.attributes;let fail=true;
  Object.defineProperty(two.geometry,'attributes',{get(){if(fail)throw Error('attribute unavailable');return attributes;}});
  one.matrixWorld.elements[12]=9;assert.throws(()=>cache.inspect(scene,view),/unavailable/);assert.equal(cache.valid,false);
  fail=false;assert.equal(inspect(cache,scene,view).changed,true);assert.equal(inspect(cache,scene,view).changed,false);
});

test('dynamic actors never enter static records and static mesh ancestors stay visible',()=>{
  const cache=new AuraCache(),scene=node(),parent=mesh(),actor=node({userData:{auraDynamic:true}}),body=mesh(),other=mesh(),view=camera();
  add(scene,parent,other);add(parent,actor);add(actor,body);
  const result=inspect(cache,scene,view);
  assert.deepEqual(result.dynamicRoots,[actor]);assert.equal(cache.records.has(body),false);
  assert.equal(result.hideForDynamic.includes(parent),false);assert.equal(result.hideForDynamic.includes(other),true);
  body.matrixWorld.elements[12]=42;assert.equal(inspect(cache,scene,view).changed,false);
  withHidden(result.hideForDynamic,()=>{assert.equal(parent.visible,true);assert.equal(actor.visible,true);assert.equal(other.visible,false);});
  assert.equal(other.visible,true);
});

test('skeletal, morph, custom callbacks and non-mesh shapes are handled conservatively',()=>{
  const cache=new AuraCache(),scene=node(),view=camera(),line=node({isLine:true,geometry:{attributes:{position:{version:0}}},material:{visible:true}});
  const skin=mesh({isSkinnedMesh:true}),morph=mesh({morphTargetInfluences:[0]}),custom=mesh({onBeforeRender(){}});add(scene,line,skin,morph,custom);
  const first=inspect(cache,scene,view);assert.equal(first.dynamicRoots.length,3);assert.deepEqual(first.staticObjects,[line]);
  line.geometry.attributes.position.version++;assert.equal(inspect(cache,scene,view).changed,true);
  cache.invalidate();assert.equal(inspect(cache,scene,view).changed,true);
});

test('an explicitly static batch callback remains guarded by its instance buffer version',()=>{
  const cache=new AuraCache(),scene=node(),view=camera(),batch=mesh({userData:{auraStatic:true},onBeforeRender(){},isInstancedMesh:true,instanceMatrix:{version:0,count:3},count:3});add(scene,batch);
  const first=inspect(cache,scene,view);assert.equal(first.dynamicRoots.length,0);assert.deepEqual(first.staticObjects,[batch]);
  assert.equal(inspect(cache,scene,view).changed,false);batch.instanceMatrix.version++;assert.equal(inspect(cache,scene,view).changed,true);
});

test('temporary visibility changes restore all original visible states after an exception',()=>{
  const one=node(),alreadyHidden=node({visible:false});
  assert.throws(()=>withHidden([one,alreadyHidden],()=>{throw Error('render failed');}),/render failed/);
  assert.equal(one.visible,true);assert.equal(alreadyHidden.visible,false);
});

// No dependency is installed by this suite. A developer can point this at an
// existing Three.js installation to run real scene-graph/StudioPass integration.
let threeRoot=null;
const testRoot=dirname(fileURLToPath(import.meta.url));
for(const base of [process.env.OFFICE_THREE_TEST_ROOT,testRoot,process.cwd()].filter(Boolean)){
  try{const require=createRequire(resolve(base,'package.json'));threeRoot=resolve(dirname(require.resolve('three')),'..');break;}catch{}
}

test('StudioPass cache integration with an existing Three.js fixture',{skip:!threeRoot},async t=>{
  const THREE=await import(pathToFileURL(resolve(threeRoot,'build/three.module.js')).href);
  const sourcePath=resolve(testRoot,'public/office/scene/studio.js');
  const source=(await readFile(sourcePath,'utf8'))
    .replace("from 'three';",`from '${pathToFileURL(resolve(threeRoot,'build/three.module.js')).href}';`)
    .replace("from 'three/addons/postprocessing/Pass.js';",`from '${pathToFileURL(resolve(threeRoot,'examples/jsm/postprocessing/Pass.js')).href}';`)
    .replace("from './aura-cache.js';",`from '${pathToFileURL(resolve(testRoot,'public/office/scene/aura-cache.js')).href}';`)
    .replace("from './gaussian-kernel.js';",`from '${pathToFileURL(resolve(testRoot,'public/office/scene/gaussian-kernel.js')).href}';`);
  const {StudioPass,STUDIO}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
  class Renderer {
    constructor(scene){this.scene=scene;this.target={name:'external'};this.autoClear=true;this.color=new THREE.Color('#123456');this.alpha=.7;this.shadowMap={enabled:true};this.domElement=new EventTarget();this.sceneCalls=[];this.quadCalls=0;}
    getRenderTarget(){return this.target;}setRenderTarget(target){this.target=target;}
    getClearColor(out){return out.copy(this.color);}getClearAlpha(){return this.alpha;}
    setClearColor(color,alpha){this.color.set(color);this.alpha=alpha;}
    clear(){if(this.target?.texture)this.target.texture.pixels=new Set();}
    render(object,view){
      if(object===this.scene){
        if(object.matrixWorldAutoUpdate)object.updateMatrixWorld();
        const names=[];object.traverseVisible(o=>{if((o.isMesh||o.isLine||o.isPoints||o.isSprite)&&view.layers.test(o.layers)&&(Array.isArray(o.material)?o.material.some(m=>m?.visible):o.material?.visible))names.push(o.name);});
        this.sceneCalls.push({target:this.target,names});
        if(this.fail?.(this.target,names))throw Error('injected render failure');
        if(this.autoClear)this.target.texture.pixels=new Set();
        for(const name of names)(this.target.texture.pixels ||= new Set()).add(name);
      }else if(object.material.uniforms.tMap){
        this.quadCalls++;
        if(this.fail?.(this.target,[]))throw Error('injected render failure');
        this.target.texture.pixels=new Set(object.material.uniforms.tMap.value?.pixels||[]);
      }else if(object.material.uniforms.tHold){this.quadCalls++;this.lastAura=new Set(object.material.uniforms.tHold.value?.pixels||[]);}
    }
  }
  const fixture=()=>{
    const scene=new THREE.Scene(),view=new THREE.OrthographicCamera(-5,5,5,-5,.1,100);view.position.z=10;
    const furniture=new THREE.Mesh(new THREE.BoxGeometry(),new THREE.MeshBasicMaterial());furniture.name='furniture';scene.add(furniture);
    const pass=new StudioPass(scene,view);pass.setSize(400,300);
    return{scene,view,furniture,pass,renderer:new Renderer(scene)};
  };
  const draw=({pass,renderer})=>pass.render(renderer,null,{texture:{}});

  await t.test('static mask and all blur outputs are reused until camera or geometry changes',()=>{
    const f=fixture();draw(f);assert.equal(f.renderer.sceneCalls.length,1);draw(f);assert.equal(f.renderer.sceneCalls.length,1);
    f.view.position.x=1;draw(f);assert.equal(f.renderer.sceneCalls.length,2);
    f.furniture.position.y=2;draw(f);assert.equal(f.renderer.sceneCalls.length,3);
    f.pass.dispose();
  });
  await t.test('dynamic motion uses the cached static mask and disappears without ghosts',()=>{
    const f=fixture(),actor=new THREE.Group(),body=new THREE.Mesh(new THREE.SphereGeometry(),new THREE.MeshBasicMaterial());actor.userData.auraDynamic=true;body.name='actor';actor.add(body);f.scene.add(actor);
    draw(f);assert.deepEqual([...f.renderer.lastAura].sort(),['actor','furniture']);
    const staticCalls=()=>f.renderer.sceneCalls.filter(x=>x.target===f.pass.t.staticMask).length;
    assert.equal(staticCalls(),1);body.scale.y=1.1;draw(f);assert.equal(staticCalls(),1);
    assert.deepEqual(f.renderer.sceneCalls.at(-1).names,['actor']);
    actor.visible=false;draw(f);assert.deepEqual([...f.renderer.lastAura],['furniture']);
    actor.visible=true;draw(f);assert.deepEqual([...f.renderer.lastAura].sort(),['actor','furniture']);
    f.scene.remove(actor);draw(f);assert.deepEqual([...f.renderer.lastAura],['furniture']);f.pass.dispose();
  });
  await t.test('dynamic descendants beneath static meshes survive partitioning',()=>{
    const f=fixture(),actor=new THREE.Group(),body=new THREE.Mesh(new THREE.BoxGeometry(),new THREE.MeshBasicMaterial());actor.userData.auraDynamic=true;body.name='child';actor.add(body);f.furniture.add(actor);draw(f);draw(f);
    assert.deepEqual([...f.renderer.lastAura].sort(),['child','furniture']);assert.equal(f.furniture.visible,true);f.pass.dispose();
  });
  await t.test('scene matrix update hooks cannot re-enable a temporarily hidden batch',()=>{
    const f=fixture(),actor=new THREE.Group(),body=new THREE.Mesh(new THREE.BoxGeometry(),new THREE.MeshBasicMaterial());actor.userData.auraDynamic=true;body.name='actor';actor.add(body);f.scene.add(actor);
    const update=f.scene.updateMatrixWorld.bind(f.scene);f.scene.updateMatrixWorld=(...args)=>{f.furniture.visible=true;return update(...args);};draw(f);draw(f);
    assert.deepEqual(f.renderer.sceneCalls.at(-1).names,['actor']);assert.equal(f.furniture.visible,true);assert.equal(f.scene.matrixWorldAutoUpdate,true);f.pass.dispose();
  });
  await t.test('failure restores renderer/scene state and retries invalid static pixels',()=>{
    const f=fixture(),override={name:'original'},originalTarget=f.renderer.target,color=f.renderer.color.getHex();f.scene.overrideMaterial=override;f.renderer.autoClear=false;f.renderer.shadowMap.enabled=false;
    f.renderer.fail=()=>true;assert.throws(()=>draw(f),/injected/);
    assert.equal(f.scene.overrideMaterial,override);assert.equal(f.furniture.visible,true);assert.equal(f.renderer.target,originalTarget);assert.equal(f.renderer.autoClear,false);assert.equal(f.renderer.shadowMap.enabled,false);assert.equal(f.renderer.alpha,.7);assert.equal(f.renderer.color.getHex(),color);assert.equal(f.pass.auraCache.valid,false);
    f.renderer.fail=null;draw(f);assert.equal(f.pass.auraCache.valid,true);f.pass.dispose();
  });
  await t.test('failed mask copy or dynamic draw restores visibility and invalidates committed pixels',()=>{
    for(const during of ['copy','dynamic']){
      const f=fixture(),actor=new THREE.Group(),body=new THREE.Mesh(new THREE.BoxGeometry(),new THREE.MeshBasicMaterial());actor.userData.auraDynamic=true;body.name='actor';actor.add(body);f.scene.add(actor);draw(f);
      const target=f.renderer.target,color=f.renderer.color.getHex();
      f.renderer.fail=(at,names)=>at===f.pass.t.mask&&(during==='copy'?names.length===0:names.includes('actor'));
      assert.throws(()=>draw(f),/injected/);assert.equal(f.furniture.visible,true);assert.equal(actor.visible,true);assert.equal(f.scene.overrideMaterial,null);assert.equal(f.renderer.target,target);assert.equal(f.renderer.color.getHex(),color);assert.equal(f.renderer.alpha,.7);assert.equal(f.pass.auraCache.valid,false);
      f.renderer.fail=null;const previous=f.renderer.sceneCalls.filter(x=>x.target===f.pass.t.staticMask).length;draw(f);assert.equal(f.renderer.sceneCalls.filter(x=>x.target===f.pass.t.staticMask).length,previous+1);assert.deepEqual([...f.renderer.lastAura].sort(),['actor','furniture']);f.pass.dispose();
    }
  });
  await t.test('share-camera projection and visibility restore both rebuild the proper live silhouette',()=>{
    const f=fixture(),extra=new THREE.Mesh(new THREE.BoxGeometry(),new THREE.MeshBasicMaterial());extra.name='retiring';f.scene.add(extra);draw(f);
    const position=f.view.position.clone(),projection=f.view.projectionMatrix.clone();
    extra.visible=false;f.view.position.set(2,4,6);f.view.zoom=2;f.view.setViewOffset(800,600,50,40,400,300);f.view.updateProjectionMatrix();f.pass.setSize(1600,900);draw(f);assert.deepEqual([...f.renderer.lastAura],['furniture']);
    extra.visible=true;f.view.position.copy(position);f.view.zoom=1;f.view.clearViewOffset();f.view.projectionMatrix.copy(projection);f.pass.setSize(400,300);draw(f);assert.deepEqual([...f.renderer.lastAura].sort(),['furniture','retiring']);
    const count=f.renderer.sceneCalls.length;draw(f);assert.equal(f.renderer.sceneCalls.length,count);f.pass.dispose();
  });
  await t.test('blur scaling can change without reusing the previous completed aura',()=>{
    const f=fixture();draw(f);const before=f.pass.auraUnit;
    f.view.zoom=2;draw(f);assert.notEqual(f.pass.auraUnit,before);assert.equal(f.renderer.sceneCalls.length,1,'static shape itself remains cached when projection is unchanged');f.pass.dispose();
  });
  await t.test('changing exported blur settings invalidates completed aura without redrawing static geometry',()=>{
    const f=fixture(),sigma=STUDIO.layers[0].sigma;draw(f);draw(f);const before=f.renderer.quadCalls;
    try{STUDIO.layers[0].sigma=sigma+1;draw(f);assert.equal(f.renderer.quadCalls-before,10);assert.equal(f.renderer.sceneCalls.length,1);}
    finally{STUDIO.layers[0].sigma=sigma;f.pass.dispose();}
  });
  await t.test('capture-sized resize, return to live size and context restoration rebuild safely',()=>{
    const f=fixture();draw(f);const old=Object.values(f.pass.t);let disposed=0;for(const target of old)target.addEventListener('dispose',()=>disposed++);
    f.pass.setSize(400,300);assert.equal(disposed,0);f.pass.setSize(1600,900);assert.equal(disposed,9);draw(f);
    f.pass.setSize(400,300);draw(f);const count=f.renderer.sceneCalls.length;f.renderer.domElement.dispatchEvent(new Event('webglcontextrestored'));draw(f);assert.equal(f.renderer.sceneCalls.length,count+1);
    const last=Object.values(f.pass.t);let finalDisposed=0;for(const target of last)target.addEventListener('dispose',()=>finalDisposed++);f.pass.dispose();assert.equal(finalDisposed,9);assert.equal(f.pass.auraCache.records.size,0);
  });
  await t.test('background/clipping/debug baseline uses the original full-mask path',()=>{
    for(const mode of ['background','clipping','debug']){
      const f=fixture();if(mode==='background')f.scene.background=new THREE.Color('white');if(mode==='clipping')f.renderer.clippingPlanes=[{}];if(mode==='debug')f.pass.cacheEnabled=false;
      draw(f);draw(f);assert.equal(f.renderer.sceneCalls.length,2);assert.ok(f.renderer.sceneCalls.every(x=>x.target===f.pass.t.mask));f.pass.dispose();
    }
  });
});
