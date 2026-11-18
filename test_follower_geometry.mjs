import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {followerSlots,FollowerMotion} from './public/office/subagent-view.js';
import {collectFollowerObstacles,createFollowerObstacles,createFollowerNavigator} from './public/office/scene/follower-obstacles.js';
import {batchStaticFurniture} from './public/office/scene/static-furniture.js';
import {groundLayout} from './public/office/overview.js';
import {NATIVE_LAMP,lobbyLightLayout} from './public/office/scene/lighting-layout.js';

const bounds={minX:-9.5,maxX:9.5,minZ:-7.5,maxZ:7.5};
const bots=Array.from({length:3},(_,n)=>({id:'child:'+n,agent_id:'native:'+n,root_agent_id:'parent'}));
const roots=(x,z)=>new Map([['parent',{pos:{x,y:0,z},yaw:0}]]);

test('standing clearance reserves whole table footprints, including rotated/long furniture away from seat centers',()=>{
  const obstacles=createFollowerObstacles([{minX:-3,maxX:3,minZ:1,maxZ:2.2}]);
  const slots=followerSlots(bots,roots(0,0),bounds,new Map(),[],null,obstacles);
  assert.equal(slots.size,bots.length);
  for(const at of slots.values())assert.equal(obstacles.blocks(at),false);
  assert.equal(obstacles.blocks({x:0,z:1.65}),true,'old first-child location intersects the tabletop');
});

test('moving parents do not teleport children to destination-derived offsets or through furniture',()=>{
  const obstacles=createFollowerObstacles([{minX:-.7,maxX:.7,minZ:-4,maxZ:4}]);
  const nav=createFollowerNavigator(bounds,obstacles),motion=new FollowerMotion(nav);
  let targets=followerSlots(bots.slice(0,1),roots(8,6),bounds,new Map(),[],null,obstacles);
  let positions=motion.step(targets),last={...positions.get('child:0')};
  targets=followerSlots(bots.slice(0,1),roots(-8,-6),bounds,targets,[],null,obstacles);
  for(let frame=0;frame<900;frame++){
    const at=motion.step(targets,1/30).get('child:0');
    assert.ok(Math.hypot(at.x-last.x,at.z-last.z)<=3.5/30+1e-7,'movement must be elapsed-time limited');
    assert.equal(nav.clear(at),true);assert.equal(nav.segment(last,at),true);last={...at};
  }
  const target=targets.get('child:0');
  assert.ok(Math.hypot(last.x-target.x,last.z-target.z)<.03,'child routes around furniture to its new parent');
  assert.equal(motion.moving,false,'finished following must allow GPU idle');
});

test('parent live motion remains bounded while its preferred child offset changes near floor corners',()=>{
  const nav=createFollowerNavigator(bounds,createFollowerObstacles([])),motion=new FollowerMotion(nav);
  let slots=new Map(),previous=null;
  for(let frame=0;frame<600;frame++){
    const t=Math.min(frame/150,1),parents=roots(8-16*t,6-12*t);
    slots=followerSlots(bots.slice(0,1),parents,bounds,slots);
    const at=motion.step(slots,1/30).get('child:0');
    assert.equal(nav.clear(at),true);
    if(previous)assert.ok(Math.hypot(at.x-previous.x,at.z-previous.z)<=3.5/30+1e-7);
    previous={...at};
  }
  assert.ok(Math.hypot(previous.x-slots.get('child:0').x,previous.z-slots.get('child:0').z)<.03);
  assert.equal(motion.moving,false);
});

test('crossing and exchanged sibling reservations arrive without blocking or permanent overlap',()=>{
  const nav=createFollowerNavigator(bounds,createFollowerObstacles([])),motion=new FollowerMotion(nav);
  const targets=new Map([['child:a',{x:-3,z:0}],['child:b',{x:3,z:0}]]);
  let positions=motion.step(targets);
  targets.set('child:a',{x:3,z:0});targets.set('child:b',{x:-3,z:0});
  for(let frame=0;frame<600;frame++){
    const next=motion.step(targets,1/30),a=next.get('child:a'),b=next.get('child:b');
    for(const [id,at] of next){const old=positions.get(id);assert.ok(Math.hypot(at.x-old.x,at.z-old.z)<=3.5/30+1e-7);}
    positions=next;
  }
  for(const [id,target] of targets){const at=positions.get(id);assert.ok(Math.hypot(at.x-target.x,at.z-target.z)<.03,id+' remains blocked');}
  assert.equal(motion.moving,false);
});

test('temporary reservation exhaustion retains an active child without a jump or false membership retirement',()=>{
  const nav=createFollowerNavigator(bounds,createFollowerObstacles([])),motion=new FollowerMotion(nav);
  const initial=new Map([['child:a',{x:-2,z:1,root_agent_id:'parent'}]]);
  const before=motion.step(initial).get('child:a');
  const held=motion.step(new Map(),1/30,new Set(['child:a'])).get('child:a');
  assert.deepEqual(held,before);assert.equal(motion.moving,false);
  const after=motion.step(new Map([['child:a',{x:2,z:1,root_agent_id:'parent'}]]),1/30,new Set(['child:a'])).get('child:a');
  assert.ok(Math.hypot(after.x-held.x,after.z-held.z)<=3.5/30+1e-7);
  assert.equal(motion.step(new Map(),1/30,new Set()).size,0);
});

test('selecting an unplaced child reassigns capacity without retaining an overlapping old occupant',()=>{
  const room={minX:-5,maxX:5,minZ:-4,maxZ:4},children=Array.from({length:128},(_,n)=>({id:'child:'+n,agent_id:'native:'+n,root_agent_id:'parent'}));
  const nav=createFollowerNavigator(room,createFollowerObstacles([])),motion=new FollowerMotion(nav),parents=roots(0,0),active=new Set(children.map(b=>b.id));
  let slots=followerSlots(children,parents,room),positions=motion.step(slots,0,active);
  const selected=children.find(bot=>!slots.has(bot.id));assert.ok(selected);
  slots=followerSlots(children,parents,room,slots,[],selected.id);
  for(let frame=0;frame<300;frame++)positions=motion.step(slots,1/30,active);
  assert.ok(positions.has(selected.id));assert.equal(positions.size,slots.size);
  const all=[...positions.values()];for(let a=0;a<all.length;a++)for(let b=0;b<a;b++)assert.ok(Math.hypot(all[a].x-all[b].x,all[a].z-all[b].z)>=1.05-1e-7);
  assert.equal(motion.moving,false);
});

let THREE,GLTFLoader;
try {
  const require=createRequire(process.env.OFFICE_THREE_TEST_ROOT?path.join(path.resolve(process.env.OFFICE_THREE_TEST_ROOT),'package.json'):import.meta.url);
  const build=path.dirname(require.resolve('three'));
  THREE=await import(pathToFileURL(path.join(build,'three.module.js')));
  ({GLTFLoader}=await import(pathToFileURL(path.join(build,'../examples/jsm/loaders/GLTFLoader.js'))));
}catch{}
const check=(name,fn)=>test(name,{skip:!THREE&&'Three.js fixture unavailable; set OFFICE_THREE_TEST_ROOT'},fn);

check('component footprints preserve empty gaps in combined native meshes and normalize floor transforms',()=>{
  const root=new THREE.Group(),object=new THREE.Group();root.add(object);root.position.set(20,8,-9);root.rotation.y=.6;
  const a=new THREE.BoxGeometry(2,.15,1),b=new THREE.BoxGeometry(2,.15,1);a.translate(-4,.75,0);b.translate(4,.75,0);
  const merged=new THREE.BufferGeometry(),one=a.toNonIndexed(),two=b.toNonIndexed();
  merged.setAttribute('position',new THREE.Float32BufferAttribute([...one.attributes.position.array,...two.attributes.position.array],3));
  object.add(new THREE.Mesh(merged,new THREE.MeshBasicMaterial()));
  const index=collectFollowerObstacles(THREE,object,root);
  assert.equal(index.blocks({x:-4,z:0}),true);assert.equal(index.blocks({x:4,z:0}),true);
  assert.equal(index.blocks({x:0,z:0}),false,'combined mesh aggregate box must not consume the aisle');
  assert.equal(index.blocks({x:20,z:-9}),false,'world transform must not leak into floor-local planning');
});

check('batched and authored furniture reserve equivalent per-instance footprints',()=>{
  const root=new THREE.Group(),object=new THREE.Group();root.add(object);
  const geometry=new THREE.BoxGeometry(1,.7,1.4),material=new THREE.MeshStandardMaterial();
  for(const x of [-3,3]){const m=new THREE.Mesh(geometry,material);m.position.set(x,.35,.5);m.rotation.y=.4;m.userData.officeStaticBatchKey='fixture';object.add(m);}
  const before=collectFollowerObstacles(THREE,object,root);const batches=batchStaticFurniture(THREE,object);
  const after=collectFollowerObstacles(THREE,object,root);assert.ok(batches.batches.length);
  for(let x=-5;x<=5;x+=.2)for(let z=-3;z<=3;z+=.2)assert.equal(after.blocks({x,z}),before.blocks({x,z}));
  batches.dispose();
});

let fixture;
async function realFloor(){
  if(fixture)return fixture;
  const loader=new GLTFLoader();loader.register(()=>({name:'offline-texture-fixture',loadTexture:()=>Promise.resolve(new THREE.Texture())}));
  const bytes=readFileSync(new URL('./public/assets/office/office.glb',import.meta.url));
  const gltf=await loader.parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'');
  fixture=gltf.scene.getObjectByName('floor_open');assert.ok(fixture);return fixture;
}
async function hall(batching){
  const floor=await realFloor(),context=new Proxy({createRadialGradient:()=>({addColorStop(){}})},{get:(o,k)=>o[k]||(()=>{})});
  const old=globalThis.document;globalThis.document={createElement:()=>({style:{},remove(){},getContext:()=>context})};
  class CSS2DObject extends THREE.Object3D{constructor(element){super();this.element=element;}}
  class WallDisplays{constructor(screen){this.screen={tex:new THREE.Texture(),mat:new THREE.MeshStandardMaterial()};this.materials=[this.screen.mat];screen.material=this.screen.mat;}setAccent(){}}
  const source=readFileSync(new URL('./public/office/scene/ground-hall.js',import.meta.url),'utf8').replace(/^import .*;\n/gm,'').replace('export class GroundHall','class GroundHall');
  const GroundHall=new Function('THREE','CSS2DObject','WallDisplays','groundLayout','NATIVE_LAMP','lobbyLightLayout','batchStaticFurniture',source+'\nreturn GroundHall;')(THREE,CSS2DObject,WallDisplays,groundLayout,NATIVE_LAMP,lobbyLightLayout,batchStaticFurniture);
  const instance=new GroundHall({templates:{floor_open:floor}},{batching});instance.setShown(true);
  globalThis.document=old;return instance;
}

check('all 69 real G/F parent seats place their first child outside actual furniture, batched and unbatched',async()=>{
  for(const batching of [false,true]){
    const h=await hall(batching),obstacles=collectFollowerObstacles(THREE,h.obj,h.root);createFollowerNavigator(h.layout,obstacles);let placed=0;
    for(const seat of h.spots){
      const at=followerSlots(bots.slice(0,1),new Map([['parent',seat]]),h.layout,new Map(),h.spots,null,obstacles).get('child:0');
      assert.ok(at,'standing position missing for '+seat.id);placed++;
      assert.equal(obstacles.blocks(at),false,seat.id+' overlaps static geometry');
      const ray=new THREE.Raycaster(new THREE.Vector3(at.x,1,at.z),new THREE.Vector3(0,-1,0));
      const furniture=ray.intersectObject(h.obj,true).find(hit=>hit.point.y>.1&&hit.point.y<1);
      assert.equal(furniture,undefined,seat.id+' child center intersects real rendered triangles');
    }
    assert.equal(placed,69);h.clear();
  }
});

check('real G/F room-to-room and lounge-to-CEO travel routes arrive without crossings or jumps',async()=>{
  const h=await hall(true),obstacles=collectFollowerObstacles(THREE,h.obj,h.root),nav=createFollowerNavigator(h.layout,obstacles);
  const pairs=[['gf-office-4-001','gf-office-5-001'],['gf-main-002','gf-ceo-001'],['gf-ceo-001','gf-main-035']];
  for(const [fromId,toId] of pairs){
    const from=h.spots.find(s=>s.id===fromId),to=h.spots.find(s=>s.id===toId);assert.ok(from&&to);
    let slots=followerSlots(bots.slice(0,1),new Map([['parent',from]]),h.layout,new Map(),h.spots,null,obstacles);
    const motion=new FollowerMotion(nav);let last=motion.step(slots).get('child:0');
    slots=followerSlots(bots.slice(0,1),new Map([['parent',to]]),h.layout,slots,h.spots,null,obstacles);
    for(let frame=0;frame<1800;frame++){
      const at=motion.step(slots,1/30).get('child:0');
      assert.equal(nav.clear(at),true);assert.equal(nav.segment(last,at),true);
      assert.ok(Math.hypot(at.x-last.x,at.z-last.z)<=3.5/30+1e-7);last={...at};
    }
    const target=slots.get('child:0');assert.ok(Math.hypot(last.x-target.x,last.z-target.z)<.03,fromId+' -> '+toId+' failed to arrive');
    assert.equal(motion.moving,false);
  }
  h.clear();
});

check('regular native office uses actual desks/lounge geometry, with useful child capacity',async()=>{
  const floor=(await realFloor()).clone(true),root=new THREE.Group();root.add(floor);
  const obstacles=collectFollowerObstacles(THREE,floor,root),parents=new Map();createFollowerNavigator(bounds,obstacles);
  const inv=root.matrixWorld.clone().invert(),p=new THREE.Vector3();
  floor.traverse(o=>{if(o.name==='desk_01'){p.setFromMatrixPosition(o.matrixWorld).applyMatrix4(inv);parents.set('parent',{pos:p.clone()});}});
  const children=Array.from({length:8},(_,n)=>({...bots[0],id:'child:'+n,agent_id:'native:'+n}));
  const slots=followerSlots(children,parents,bounds,new Map(),[],null,obstacles);
  assert.equal(slots.size,8);for(const at of slots.values())assert.equal(obstacles.blocks(at),false);
  let seats=0;
  floor.traverse(o=>{if(/^(desk|lounge)_\d\d$/.test(o.name)){
    const pos=new THREE.Vector3().setFromMatrixPosition(o.matrixWorld).applyMatrix4(inv);
    const at=followerSlots(bots.slice(0,1),new Map([['parent',{pos}]]),bounds,new Map(),[],null,obstacles).get('child:0');
    assert.ok(at,o.name+' needs a reachable child slot');assert.equal(obstacles.canStand(at),true);seats++;
    assert.ok(at.x>=obstacles.floorBounds.minX+.65&&at.x<=obstacles.floorBounds.maxX-.65);
    assert.ok(at.z>=obstacles.floorBounds.minZ+.65&&at.z<=obstacles.floorBounds.maxZ-.65);
  }});
  assert.equal(seats,26);
});

check('bounded 3/16/32-child live-parent motion reuses static footprints and settles for idle',async t=>{
  const h=await hall(true),began=performance.now(),obstacles=collectFollowerObstacles(THREE,h.obj,h.root),extractionMs=performance.now()-began;
  const from=h.spots.find(s=>s.id==='gf-main-002'),to=h.spots.find(s=>s.id==='gf-ceo-001'),metrics=[];
  // Runtime receives only the index/navigation interface. Scene geometry can
  // no longer be visited by any motion step after this one extraction.
  h.obj.traverse=()=>{throw new Error('per-frame geometry traversal');};
  for(const count of [3,16,32]){
    const children=Array.from({length:count},(_,n)=>({id:'child:'+String(n).padStart(2,'0'),agent_id:'native:'+n,root_agent_id:'parent'}));
    const motion=new FollowerMotion(createFollowerNavigator(h.layout,obstacles));let slots=new Map(),positions=new Map(),samples=[];
    for(let frame=0;frame<900;frame++){
      const at=performance.now(),v=Math.min(frame/90,1),parents=roots(from.pos.x+(to.pos.x-from.pos.x)*v,from.pos.z+(to.pos.z-from.pos.z)*v);
      if(frame<=90)slots=followerSlots(children,parents,h.layout,slots,h.spots,null,obstacles);
      const next=motion.step(slots,1/30,new Set(children.map(b=>b.id)));
      for(const [id,p] of next){assert.equal(obstacles.blocks(p),false);const old=positions.get(id);if(old)assert.ok(Math.hypot(old.x-p.x,old.z-p.z)<=3.5/30+1e-7);}
      positions=next;samples.push(performance.now()-at);
    }
    assert.equal(motion.moving,false,'completed or safely blocked followers must not hold GPU busy');
    const idleAt=performance.now();for(let n=0;n<300;n++)motion.step(slots,0,new Set(children.map(b=>b.id)));
    const idleMs=(performance.now()-idleAt)/300,sorted=[...samples].sort((a,b)=>a-b);
    const reached=[...slots].filter(([id,target])=>{const at=positions.get(id);return at&&Math.hypot(at.x-target.x,at.z-target.z)<.03;}).length;
    assert.equal(reached,slots.size,'all chosen standing reservations must be reachable');
    const unreached=[...slots].filter(([id,target])=>{const at=positions.get(id);return !at||Math.hypot(at.x-target.x,at.z-target.z)>=.03;}).map(([id,target])=>({id,target,position:positions.get(id),failed:motion.states.get(id)?.failed}));
    metrics.push({children:count,placed:positions.size,targets:slots.size,reached,unreached,averageFrameMs:samples.reduce((a,b)=>a+b,0)/samples.length,p95FrameMs:sorted[Math.floor(sorted.length*.95)],maxFrameMs:sorted.at(-1),idleStepMs:idleMs});
  }
  t.diagnostic(JSON.stringify({extractionMs,footprints:obstacles.boxes.length,metrics}));h.clear();
});
