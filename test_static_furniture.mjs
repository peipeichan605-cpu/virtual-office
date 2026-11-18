import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
import path from 'node:path';
import {batchStaticFurniture,furnitureSeatIds} from './public/office/scene/static-furniture.js';
import {groundLayout} from './public/office/overview.js';
import {NATIVE_LAMP,lobbyLightLayout} from './public/office/scene/lighting-layout.js';

let THREE;
try {
  const require=createRequire(process.env.OFFICE_THREE_TEST_ROOT ? path.join(path.resolve(process.env.OFFICE_THREE_TEST_ROOT),'package.json') : import.meta.url);
  THREE=require('three');
} catch {}
const check=(name,fn)=>test(name,{skip:!THREE&&'Three.js fixture unavailable; use installed three or OFFICE_THREE_TEST_ROOT'},fn);
// Instanced matrices are uploaded as Float32, like GPU mesh matrices.
const close=(one,two,epsilon=1e-6)=>assert.ok(Math.abs(one-two)<epsilon,`${one} != ${two}`);
const sameMatrix=(one,two)=>one.elements.forEach((n,index)=>close(n,two.elements[index]));

function furniture(root,geometry,material,x,id,key='oak'){
  const station=new THREE.Group();station.position.set(x,.1,x*.2);station.rotation.y=x*.07;station.userData.officeRoomId='room-a';
  const mesh=new THREE.Mesh(geometry,material);mesh.position.set(.2,.7,-.3);mesh.castShadow=mesh.receiveShadow=true;
  mesh.userData.officeStaticBatchKey=key;
  if(id)mesh.userData.officeSeatIds=[id];
  station.add(mesh);root.add(station);return mesh;
}

check('batching preserves nested world transforms, normal directions, bounds and per-instance seats',()=>{
  const scene=new THREE.Scene(),root=new THREE.Group();root.position.set(3,7,-2);root.rotation.y=.41;scene.add(root);
  const geometry=new THREE.BoxGeometry(1,.2,2),material=new THREE.MeshStandardMaterial({color:'#bfa070'});
  const first=furniture(root,geometry,material,1,'desk-a'),second=furniture(root,geometry,material,4,'desk-b');
  second.scale.set(1.5,.7,2);scene.updateMatrixWorld(true);
  const expected=[first.matrixWorld.clone(),second.matrixWorld.clone()];
  const controller=batchStaticFurniture(THREE,root);scene.updateMatrixWorld(true);
  assert.equal(controller.sourceCount,2);assert.equal(controller.batches.length,1);
  const batch=controller.batches[0],matrix=new THREE.Matrix4();
  for(let index=0;index<2;index++){
    batch.getMatrixAt(index,matrix);const world=new THREE.Matrix4().multiplyMatrices(batch.matrixWorld,matrix);sameMatrix(world,expected[index]);
    const normal=new THREE.Vector3(.3,.8,.1).normalize();
    const originalNormal=normal.clone().applyMatrix3(new THREE.Matrix3().getNormalMatrix(expected[index])).normalize();
    const columns=[0,1,2].map(axis=>new THREE.Vector3().setFromMatrixColumn(matrix,axis));
    const instanceNormal=normal.clone().divide(new THREE.Vector3(...columns.map(v=>v.lengthSq()))).applyMatrix3(new THREE.Matrix3().setFromMatrix4(matrix));
    instanceNormal.applyMatrix3(new THREE.Matrix3().getNormalMatrix(batch.matrixWorld)).normalize();
    close(instanceNormal.distanceTo(originalNormal),0);
    const originalCorners=geometry.boundingBox||geometry.computeBoundingBox()||geometry.boundingBox;
    for(const corner of [originalCorners.min,originalCorners.max])assert.ok(batch.boundingBox.containsPoint(corner.clone().applyMatrix4(matrix)));
  }
  assert.deepEqual(batch.userData.officeSeatIds,['desk-a','desk-b']);
  assert.deepEqual(furnitureSeatIds(batch,0),['desk-a']);assert.deepEqual(furnitureSeatIds(batch,1),['desk-b']);
  assert.equal(furnitureSeatIds(batch),null);assert.equal(furnitureSeatIds(batch,-1),null);assert.equal(furnitureSeatIds(batch,2),null);
  controller.dispose();assert.equal(first.parent?.userData.officeRoomId,'room-a');
});

check('palette and room zones stay separate; custom shaders, screens, transparency, mirrors and shear stay authored',()=>{
  const root=new THREE.Group(),geometry=new THREE.BoxGeometry(),oak=new THREE.MeshStandardMaterial(),dark=oak.clone();dark.color.set('#302822');
  furniture(root,geometry,oak,0,null,'oak');furniture(root,geometry,oak,2,null,'oak');
  furniture(root,geometry,dark,4,null,'dark');furniture(root,geometry,dark,6,null,'dark');
  const screen=furniture(root,geometry,oak,8,'screen');delete screen.userData.officeStaticBatchKey;
  const glass=furniture(root,geometry,new THREE.MeshStandardMaterial({transparent:true,opacity:.2}),10);
  const mirrored=furniture(root,geometry,oak,12);mirrored.scale.x=-1;
  const shear=furniture(root,geometry,oak,14);shear.parent.scale.set(2,1,1);shear.rotation.y=.4;
  const other=furniture(root,geometry,oak,16);other.parent.userData.officeRoomId='other';
  const patched=oak.clone();patched.userData.sunRelit=true;patched.onBeforeCompile=()=>{};
  const shaderA=furniture(root,geometry,patched,18),shaderB=furniture(root,geometry,patched,20);
  const keyed=oak.clone();keyed.customProgramCacheKey=()=> 'custom-key';
  const keyA=furniture(root,geometry,keyed,22),keyB=furniture(root,geometry,keyed,24);
  const controller=batchStaticFurniture(THREE,root);
  assert.equal(controller.batches.length,2);assert.equal(controller.sourceCount,4);
  for(const mesh of [screen,glass,mirrored,shear,other,shaderA,shaderB,keyA,keyB])assert.ok(mesh.parent,'excluded mesh remains in scene');
  assert.notEqual(controller.batches[0].material.color.getHex(),controller.batches[1].material.color.getHex());controller.dispose();
});

check('unchanged frames keep matrix versions stable while wrapper moves and visibility invalidate once',()=>{
  const scene=new THREE.Scene(),root=new THREE.Group();scene.add(root);
  const geometry=new THREE.BoxGeometry(),material=new THREE.MeshStandardMaterial();
  const first=furniture(root,geometry,material,1,'a'),second=furniture(root,geometry,material,2,'b');const parent=first.parent;
  const controller=batchStaticFurniture(THREE,root);scene.updateMatrixWorld(true);const batch=controller.batches[0],version=batch.instanceMatrix.version;
  for(let index=0;index<60;index++){root.position.y+=.3;scene.updateMatrixWorld(true);}
  assert.equal(batch.instanceMatrix.version,version,'moving common floor does not upload unchanged local instances');
  parent.position.x+=3;scene.updateMatrixWorld(true);assert.equal(batch.instanceMatrix.version,version+1);
  parent.visible=false;scene.updateMatrixWorld(true);assert.deepEqual(furnitureSeatIds(batch,0),[]);
  assert.ok(batch.boundingBox.min.x>1,'disabled zero-matrix instances do not expand capture bounds to the origin');
  const hiddenVersion=batch.instanceMatrix.version;for(let index=0;index<30;index++)scene.updateMatrixWorld(true);assert.equal(batch.instanceMatrix.version,hiddenVersion);
  parent.visible=true;scene.updateMatrixWorld(true);assert.deepEqual(furnitureSeatIds(batch,0),['a']);
  assert.deepEqual(furnitureSeatIds(batch,1),['b']);assert.ok(batch.boundingBox.max.x>4);controller.dispose();
});

check('later fade, palette or unsupported transform changes fall back without altering other instances',()=>{
  const scene=new THREE.Scene(),root=new THREE.Group();scene.add(root);const geometry=new THREE.BoxGeometry();
  const first=furniture(root,geometry,new THREE.MeshStandardMaterial({color:'#a08050'}),1,'a');
  const second=furniture(root,geometry,first.material.clone(),2,'b');const parent=first.parent;
  const controller=batchStaticFurniture(THREE,root);scene.updateMatrixWorld(true);const batch=controller.batches[0],color=batch.material.color.getHex();
  assert.deepEqual(batch.officeSourceMaterials,[first.material,second.material]);
  assert.equal(batch.userData.officeSourceMaterials,undefined,'material references are not serialized in Object3D metadata');
  // A scene lighting scan can patch both the rendered batch and its detached
  // fallback materials before any source has to return to the scene.
  const patch=()=>{};for(const material of [batch.material,...batch.officeSourceMaterials])material.onBeforeCompile=patch;
  first.material.opacity=.5;first.material.transparent=true;scene.updateMatrixWorld(true);
  assert.equal(first.parent,parent);assert.deepEqual(furnitureSeatIds(batch,0),[]);assert.deepEqual(furnitureSeatIds(batch,1),['b']);
  assert.equal(first.material.onBeforeCompile,patch);
  assert.equal(batch.material.opacity,1);assert.equal(batch.material.color.getHex(),color);
  first.material.opacity=1;first.material.transparent=false;scene.updateMatrixWorld(true);assert.equal(first.parent,null);
  second.material.color.set('#ff0000');scene.updateMatrixWorld(true);assert.ok(second.parent);assert.equal(batch.material.color.getHex(),color);
  first.scale.x=-1;scene.updateMatrixWorld(true);assert.equal(first.parent,parent);assert.equal(batch.visible,false);controller.dispose();
});

check('seat resolution handles ordinary ancestors, instance zero, desk-only entries and missing seats',()=>{
  const station=new THREE.Group(),mesh=new THREE.Mesh(new THREE.BoxGeometry(),new THREE.MeshBasicMaterial());station.userData.officeDeskId='ordinary';station.add(mesh);
  assert.deepEqual(furnitureSeatIds(mesh),['ordinary']);
  const batch={userData:{officeSeatIds:['union-must-never-be-used'],officeInstanceMetadata:[{officeDeskId:'zero'},{officeSeatIds:[]},{officeSeatIds:['shared-a','shared-b']}]}};
  assert.deepEqual(furnitureSeatIds(batch,0),['zero']);assert.deepEqual(furnitureSeatIds(batch,1),[]);
  assert.deepEqual(furnitureSeatIds(batch,2),['shared-a','shared-b']);assert.equal(furnitureSeatIds(batch,undefined),null);assert.equal(furnitureSeatIds(batch,3),null);
});

check('fallback is correct in its first frame and removed wrappers or changed geometry are not drawn as old instances',()=>{
  const scene=new THREE.Scene(),root=new THREE.Group();scene.add(root);const geometry=new THREE.BoxGeometry();
  const first=furniture(root,geometry,new THREE.MeshStandardMaterial(),0,'a');
  const second=furniture(root,geometry,first.material.clone(),3,'b');const firstParent=first.parent,secondParent=second.parent;
  const controller=batchStaticFurniture(THREE,root);root.position.y=7;
  first.material.opacity=.5;first.material.transparent=true;scene.updateMatrixWorld(true);
  assert.equal(first.parent,firstParent);close(first.matrixWorld.elements[13],7.8);
  const batch=controller.batches[0];assert.deepEqual(furnitureSeatIds(batch,0),[]);
  root.remove(secondParent);scene.updateMatrixWorld(true);assert.deepEqual(furnitureSeatIds(batch,1),[]);assert.equal(batch.visible,false);
  root.add(secondParent);scene.updateMatrixWorld(true);assert.equal(second.parent,null);assert.deepEqual(furnitureSeatIds(batch,1),['b']);
  second.geometry=new THREE.SphereGeometry(.6,8,6);scene.updateMatrixWorld(true);
  assert.equal(second.parent,secondParent);assert.deepEqual(furnitureSeatIds(batch,1),[]);close(second.matrixWorld.elements[13],7.8);
  controller.dispose();
});

check('batch disposal restores source nodes and never disposes borrowed geometry or materials',()=>{
  const root=new THREE.Group(),geometry=new THREE.BoxGeometry(),material=new THREE.MeshStandardMaterial();let geometries=0,materials=0,instances=0,batchMaterials=0;
  geometry.addEventListener('dispose',()=>geometries++);material.addEventListener('dispose',()=>materials++);
  const one=furniture(root,geometry,material,0,'a'),two=furniture(root,geometry,material,2,'b');const parent=one.parent;
  const controller=batchStaticFurniture(THREE,root),batch=controller.batches[0];batch.addEventListener('dispose',()=>instances++);batch.material.addEventListener('dispose',()=>batchMaterials++);
  controller.dispose();controller.dispose();assert.equal(one.parent,parent);assert.ok(two.parent);assert.equal(instances,1);assert.equal(batchMaterials,1);assert.equal(geometries,0);assert.equal(materials,0);
  assert.deepEqual(batch.officeSourceMaterials,[]);
  assert.equal(root.updateMatrixWorld,THREE.Object3D.prototype.updateMatrixWorld);
});

function groundClass(){
  const context=new Proxy({createRadialGradient:()=>({addColorStop(){}})},{get:(target,key)=>target[key]||(()=>{})});
  globalThis.document={createElement:()=>({style:{},remove(){},getContext:()=>context})};
  class CSS2DObject extends THREE.Object3D{constructor(element){super();this.element=element;}}
  class WallDisplays{constructor(screen){this.screen={tex:new THREE.Texture(),mat:new THREE.MeshStandardMaterial()};this.materials=[this.screen.mat];screen.material=this.screen.mat;}setAccent(){}}
  const source=readFileSync(new URL('./public/office/scene/ground-hall.js',import.meta.url),'utf8').replace(/^import .*;\n/gm,'').replace('export class GroundHall','class GroundHall');
  return new Function('THREE','CSS2DObject','WallDisplays','groundLayout','NATIVE_LAMP','lobbyLightLayout','batchStaticFurniture',source+'\nreturn GroundHall;')(THREE,CSS2DObject,WallDisplays,groundLayout,NATIVE_LAMP,lobbyLightLayout,batchStaticFurniture);
}

function nativeFixture(){
  const floor=new THREE.Group(),oak=new THREE.MeshStandardMaterial({color:'#b7976e'});oak.name='oak';
  for(const [name,size] of Object.entries({table_1:[2,.8,1],sofa:[3,1,1],armchair_1:[1,1,1],coffee_table:[1,.5,1],tree_2:[.8,2,.8],coffee_bar:[2,1,.7],bar_stools:[2,1,.5],slab_open:[16,.3,12],wall_left_core:[.2,1,3],glass_back:[4,1,.05],wall_screen:[.03,1,1.5],curtain_wall:[16,3,.1]})){
    const mesh=new THREE.Mesh(new THREE.BoxGeometry(...size),oak);mesh.name=name;floor.add(mesh);
  }
  const body=new THREE.Mesh(new THREE.BoxGeometry(.4,.025,.4).translate(-5.7,.76,-2.9),oak);body.name='laptops';floor.add(body);
  const screenMaterial=new THREE.MeshStandardMaterial();screenMaterial.name='night_screen';
  const screen=new THREE.Mesh(new THREE.BoxGeometry(.4,.3,.02),screenMaterial);screen.name='laptop_screen_01';screen.position.set(-5.7,.95,-2.9);floor.add(screen);
  return {templates:{floor_open:floor}};
}

check('GroundHall keeps monitor material independence, seat metadata and safe rebuild/disposal',()=>{
  const GroundHall=groundClass(),assets=nativeFixture(),borrowed=[...assets.templates.floor_open.children].filter(x=>x.isMesh).map(x=>x.geometry);let borrowedDisposed=0;
  borrowed.forEach(geometry=>geometry.addEventListener('dispose',()=>borrowedDisposed++));
  const hall=new GroundHall(assets);hall.root.updateMatrixWorld(true);
  assert.ok(hall.staticFurniture.sourceCount>100);assert.ok(hall.staticFurniture.batches.length<40);
  const screens=hall.screens.filter(Boolean);assert.equal(screens.length,33);assert.equal(new Set(screens.map(x=>x.mat)).size,33);
  const before=screens[1].mat.emissiveIntensity;screens[0].mat.emissiveIntensity=.93;assert.equal(screens[1].mat.emissiveIntensity,before);
  const seatIds=new Set(hall.layout.seats.map(x=>x.id));assert.ok(hall.deskMeshes.every(mesh=>furnitureSeatIds(mesh,mesh.isInstancedMesh?0:undefined)?.every(id=>seatIds.has(id))));
  let shadows=0,glass=0;hall.obj.traverse(object=>{if(object.userData.contactShadow)shadows++;if(object.material?.name==='ground-glass')glass++;});
  assert.equal(shadows,131,'transparent contact shadows preserve their independent blend order');assert.ok(glass>0);
  const controller=hall.staticFurniture;assert.equal(hall.configure(0),false);assert.equal(hall.staticFurniture,controller);
  hall.setShown(true);assert.equal(hall.root.visible,true);hall.setShown(false);assert.equal(hall.root.visible,false);
  assert.equal(hall.configure(100),true);assert.notEqual(hall.staticFurniture,controller);hall.clear();assert.equal(borrowedDisposed,0);assert.equal(hall.staticFurniture,null);
  const reference=new GroundHall(assets,{batching:false});assert.equal(reference.staticFurniture,null);reference.clear();delete globalThis.document;
});
