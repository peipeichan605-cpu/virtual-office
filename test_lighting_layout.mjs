import test from 'node:test';
import assert from 'node:assert/strict';
import {shadowVolume,shadowMapSize,resizeShadowMap,NATIVE_LAMP,lobbyLightLayout} from './public/office/scene/lighting-layout.js';
import {groundLayout} from './public/office/overview.js';

test('native workspace floors retain their existing lighting envelope',()=>{
  assert.deepEqual(shadowVolume(),{x:8.3,z:6.3,y0:-.05,y1:3,cx:0,cz:0,distance:30});
});

test('expanded hall restores shadow detail within desktop, touch and GPU limits',()=>{
  assert.equal(shadowMapSize(),2048);
  assert.equal(shadowMapSize(true),4096);
  assert.equal(shadowMapSize(false,true),1024);
  assert.equal(shadowMapSize(true,true),2048);
  assert.equal(shadowMapSize(true,false,2048),2048);
  assert.equal(shadowMapSize(true,false,3000),2048);
});

test('switching hall and native floors replaces one shadow target only when required',()=>{
  let disposed=0,passesDisposed=0;
  const shadow={mapSize:{x:2048,y:2048,setScalar(n){this.x=this.y=n;}},
    map:{dispose(){disposed++;}},mapPass:{dispose(){passesDisposed++;}},needsUpdate:false};
  assert.equal(resizeShadowMap(shadow,2048),false);
  assert.equal(disposed,0);
  assert.equal(resizeShadowMap(shadow,4096),true);
  assert.equal(disposed,1);assert.equal(passesDisposed,1);assert.equal(shadow.map,null);assert.equal(shadow.needsUpdate,true);
  // The renderer allocates a new target after the resize. Repeated frames keep it.
  shadow.map={dispose(){disposed++;}};
  for(let frame=0;frame<120;frame++)assert.equal(resizeShadowMap(shadow,4096),false);
  assert.equal(disposed,1);
  assert.equal(resizeShadowMap(shadow,2048),true);assert.equal(disposed,2);
  shadow.map={dispose(){disposed++;}};
  assert.equal(resizeShadowMap(shadow,4096),true);assert.equal(disposed,3);
});

test('the shadow volume covers asymmetric lounge bounds and keeps all overflow corners in front',()=>{
  for(const count of [0,2,80,400]){
    const layout=groundLayout(count),volume=shadowVolume(layout);
    assert.ok(volume.cx-volume.x<=layout.minX&&volume.cx+volume.x>=layout.maxX);
    assert.ok(volume.cz-volume.z<=layout.minZ&&volume.cz+volume.z>=layout.maxZ);
    assert.ok(volume.y1>=4.3);
    const cornerRadius=Math.hypot(volume.x,volume.z,(volume.y1-volume.y0)/2);
    assert.ok(volume.distance>=cornerRadius+5.9);
  }
});

test('central lounge seats and the L-shaped Pantry counter receive local night light',()=>{
  const layout=groundLayout(),lights=lobbyLightLayout(layout);
  // Independent point-on-surface estimate: inverse-square distance, Lambert
  // incidence and the native spotlight's soft angular cutoff.
  const received=(light,point)=>{
    const dx=light.x-point.x,dy=light.y-point.y,dz=light.z-point.z,distance2=dx*dx+dy*dy+dz*dz;
    const facing=dy/Math.sqrt(distance2),outer=Math.cos(light.angle),inner=Math.cos(light.angle*(1-NATIVE_LAMP.penumbra));
    const fraction=Math.max(0,Math.min(1,(facing-outer)/(inner-outer)));
    return light.intensity/distance2*facing*fraction*fraction*(3-2*fraction);
  };
  assert.equal(lights.length,3);
  for(const seat of layout.seats.filter(seat=>seat.lounge)){
    const direct=lights.reduce((n,light)=>n+received(light,{x:seat.x,y:.48,z:seat.z}),0);
    assert.ok(direct>.075,seat.id+' must not rely only on global ambient light');
    assert.ok(direct<1.5,seat.id+' stays near the native desk-light range');
  }
  const pantry=lights.find(light=>light.id==='pantry');
  for(const point of [{x:-20.5,y:.9,z:19.8},{x:-22.68,y:.9,z:21.12}])assert.ok(received(pantry,point)>.5,'Both native coffee counters receive a warm pool');
});

test('local lobby lighting is bounded when the hall grows and does not affect native floor layout',()=>{
  assert.deepEqual(lobbyLightLayout(),[]);
  const base=lobbyLightLayout(groundLayout());
  for(const count of [80,120,400]){
    const lamps=lobbyLightLayout(groundLayout(count));
    assert.equal(lamps.length,3,'Overflow must not allocate more GPU lights');
    assert.deepEqual(lamps,base,'The permanent leisure and Pantry zones stay stable');
  }
  assert.deepEqual(shadowVolume(),{x:8.3,z:6.3,y0:-.05,y1:3,cx:0,cz:0,distance:30});
});
