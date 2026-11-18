import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFile} from 'node:fs/promises';
import {dirname,resolve} from 'node:path';
import {pathToFileURL,fileURLToPath} from 'node:url';

// Reuse an existing dependency fixture; never install or fetch dependencies here.
const here=dirname(fileURLToPath(import.meta.url));
let threeRoot=null;
for(const base of [process.env.OFFICE_THREE_TEST_ROOT,here,process.cwd()].filter(Boolean)){
  try{const req=createRequire(resolve(base,'package.json'));threeRoot=resolve(dirname(req.resolve('three')),'..');break;}catch{}
}
const moduleURL=source=>`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;

test('sun relight instancing and fallback integration with existing Three fixture',{skip:!threeRoot},async t=>{
  const THREE=await import(pathToFileURL(resolve(threeRoot,'build/three.module.js')).href);
  const threeURL=pathToFileURL(resolve(threeRoot,'build/three.module.js')).href;
  const sceneDir=resolve(here,'public/office/scene');
  const sunSource=(await readFile(resolve(sceneDir,'sunpatch.js'),'utf8')).replace("from 'three';",`from '${threeURL}';`);
  const sunURL=moduleURL(sunSource);
  const {createSunPatch}=await import(sunURL);

  await t.test('compile hook keeps native shader chunks, prior patches and both instance branches',()=>{
    const sun=new THREE.DirectionalLight(),patch=createSunPatch(sun);
    for(const kind of ['basic','standard']){
      const mat=kind==='basic'?new THREE.MeshBasicMaterial({map:new THREE.Texture()}):new THREE.MeshStandardMaterial({map:new THREE.Texture()});
      let called=0;
      mat.onBeforeCompile=shader=>{called++;shader.uniforms.priorPatch={value:1};shader.fragmentShader+='\n// prior patch';};
      mat.customProgramCacheKey=()=>`prior-${kind}`;
      patch.relight(mat);patch.relight(mat);
      const native=THREE.ShaderLib[kind];
      const shader={vertexShader:native.vertexShader,fragmentShader:native.fragmentShader,uniforms:{}};
      mat.onBeforeCompile(shader,{});
      assert.equal(called,1,'relight installs exactly once');
      assert.equal(shader.uniforms.priorPatch.value,1);
      assert.ok(shader.uniforms.bqShadowMatrix.value===sun.shadow.matrix);
      assert.equal(mat.customProgramCacheKey(),`prior-${kind}|sun`);
      assert.ok(shader.fragmentShader.endsWith('// prior patch'));
      assert.equal(shader.vertexShader.split('varying vec3 vBqWorld;').length-1,1);
      assert.match(shader.vertexShader,/#include <project_vertex>\s+mat4 bqModelMatrix = modelMatrix;/);
      assert.match(shader.vertexShader,/#ifdef USE_INSTANCING\s+bqModelMatrix = modelMatrix \* instanceMatrix;\s+#endif/);
      assert.match(shader.vertexShader,/vBqWorld = \( bqModelMatrix \* vec4\( transformed, 1\.0 \) \)\.xyz;/);
      assert.match(shader.vertexShader,/vBqNormal = normalize\( mat3\( bqModelMatrix \) \* normal \);/);
      // Three still owns projected position, including its own instance transform; the relight must
      // not mutate transformed or apply instanceMatrix twice to the native projection.
      assert.match(THREE.ShaderChunk.project_vertex,/#ifdef USE_INSTANCING[\s\S]*mvPosition = instanceMatrix \* mvPosition;/);
      assert.doesNotMatch(shader.vertexShader,/transformed\s*=\s*\(?\s*instanceMatrix/);
    }
    assert.ok(Number(THREE.REVISION)>=170,'fixture supports the deployed shader contract');
  });

  await t.test('batched world position and authored normal equal ordinary meshes under rotated nonuniform scales',()=>{
    const parent=new THREE.Object3D();parent.position.set(4,-2,7);parent.rotation.set(.12,.43,-.21);parent.scale.set(1.1,.8,1.4);parent.updateMatrixWorld(true);
    const source=new THREE.Object3D();source.position.set(-3,.6,2);source.rotation.set(.4,-.7,.17);source.scale.set(2,.35,.75);parent.add(source);parent.updateMatrixWorld(true);
    const batchModel=parent.matrixWorld,instance=source.matrix,combined=new THREE.Matrix4().multiplyMatrices(batchModel,instance);
    const normalMatrix=new THREE.Matrix3().setFromMatrix4(combined);
    const sourceNormalMatrix=new THREE.Matrix3().setFromMatrix4(source.matrixWorld);
    for(const point of [new THREE.Vector3(),new THREE.Vector3(1.2,-.7,.5),new THREE.Vector3(-2,3,4)]){
      assert.ok(point.clone().applyMatrix4(combined).distanceTo(point.clone().applyMatrix4(source.matrixWorld))<1e-12);
    }
    for(const normal of [new THREE.Vector3(0,1,0),new THREE.Vector3(.3,.5,-.2).normalize()]){
      assert.ok(normal.clone().applyMatrix3(normalMatrix).normalize().distanceTo(normal.clone().applyMatrix3(sourceNormalMatrix).normalize())<1e-12);
    }
    assert.ok(new THREE.Vector3(1,0,0).applyMatrix4(combined).distanceTo(new THREE.Vector3(1,0,0).applyMatrix4(batchModel))>.1,'fixture catches a missing instanceMatrix');
  });

  await t.test('daylight patches and tints detached originals before a material fallback',async()=>{
    const daylightSource=(await readFile(resolve(sceneDir,'daylight.js'),'utf8'))
      .replace("from 'three';",`from '${threeURL}';`)
      .replace("from './sunpatch.js';",`from '${sunURL}';`)
      .replace("from './spring.js';",`from '${pathToFileURL(resolve(sceneDir,'spring.js')).href}';`)
      .replace("from './lighting-layout.js';",`from '${pathToFileURL(resolve(sceneDir,'lighting-layout.js')).href}';`);
    const {createDaylight}=await import(moduleURL(daylightSource));
    const saved={location:globalThis.location,window:globalThis.window,document:globalThis.document};
    globalThis.location={search:'?time=13:00'};globalThis.window={};
    globalThis.document={documentElement:{dataset:{},style:{setProperty(){}}}};
    try{
      const scene=new THREE.Scene(),group=new THREE.Group(),root=new THREE.Group(),obj=new THREE.Group();scene.add(group);group.add(root);root.add(obj);
      const batchMaterial=new THREE.MeshBasicMaterial({map:new THREE.Texture()});batchMaterial.userData.nightPatched=true;
      const original=batchMaterial.clone();original.color.setRGB(.2,.3,.4);
      const geometry=new THREE.BoxGeometry(),batch=new THREE.InstancedMesh(geometry,batchMaterial,1);
      batch.officeSourceMaterials=[original,batchMaterial,original];obj.add(batch);
      const day=createDaylight({scene,renderer:{shadowMap:{},capabilities:{maxTextureSize:4096}}});
      day.setBuilding({group,activeOpen:()=>({root,obj}),activeSlot:{kind:'workspace'}});
      day.update(.016,1);
      assert.equal(batchMaterial.userData.sunRelit,true);assert.equal(original.userData.sunRelit,true);
      assert.ok(original.color.equals(batchMaterial.color),'fallback source already has the live ambient tint');
      const shader={vertexShader:THREE.ShaderLib.basic.vertexShader,fragmentShader:THREE.ShaderLib.basic.fragmentShader,uniforms:{}};
      original.onBeforeCompile(shader,{});assert.ok(shader.uniforms.bqSun);assert.match(shader.vertexShader,/bqModelMatrix/);
      assert.equal(original.customProgramCacheKey().endsWith('|sun'),true);
      geometry.dispose();batchMaterial.dispose();original.dispose();
    }finally{
      for(const [key,value]of Object.entries(saved)){if(value===undefined)delete globalThis[key];else globalThis[key]=value;}
    }
  });
});
