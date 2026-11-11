import test from 'node:test';
import assert from 'node:assert/strict';
import {groundLayout,OFFICE_CAPACITIES} from './public/office/overview.js';

const orthogonal=angle=>Math.abs(angle/(Math.PI/2)-Math.round(angle/(Math.PI/2)))<1e-8;
const bounds=(seat,minX,maxX,minZ,maxZ)=>{
  const angle=seat.rotation,points=[[minX,minZ],[minX,maxZ],[maxX,minZ],[maxX,maxZ]].map(([x,z])=>({x:seat.x+x*Math.cos(angle)+z*Math.sin(angle),z:seat.z-x*Math.sin(angle)+z*Math.cos(angle)}));
  return {minX:Math.min(...points.map(p=>p.x)),maxX:Math.max(...points.map(p=>p.x)),minZ:Math.min(...points.map(p=>p.z)),maxZ:Math.max(...points.map(p=>p.z))};
};
const parts=seat=>[
  bounds(seat,-seat.desk_width/2,seat.desk_width/2,-1.25,.15),
  bounds(seat,-.55,.55,.245,1.155),
];
const separate=(one,two)=>one.maxX<=two.minX+1e-8||two.maxX<=one.minX+1e-8||one.maxZ<=two.minZ+1e-8||two.maxZ<=one.minZ+1e-8;

test('G/F uses orthogonal lounge groups and varied office capacities with humane furniture clearance',()=>{
  const layout=groundLayout(2);
  assert.ok(layout.width*layout.depth>=3*28*22);
  assert.deepEqual(OFFICE_CAPACITIES,[8,6,4,6,8]);
  assert.equal(layout.rooms.length,5);assert.equal(layout.mainCount,36);assert.equal(layout.seats.length,69);
  assert.equal(layout.lounges.length,6);
  assert.deepEqual(new Set(layout.lounges.map(cluster=>cluster.style)),new Set(['square','parallel','L','sofa-row']));
  assert.equal(layout.seats.filter(seat=>seat.kind==='desk').length,33);
  assert.ok(layout.seats.every(seat=>orthogonal(seat.yaw)&&(!('rotation'in seat)||orthogonal(seat.rotation))));
  const lounge=layout.seats.filter(seat=>seat.kind==='lounge');
  assert.equal(lounge.length,36);assert.ok(lounge.every(seat=>seat.lounge&&seat.room_id==='lounge'&&seat.label.includes('休息位')));
  for(const cluster of layout.lounges){
    const members=lounge.filter(seat=>seat.lounge_id===cluster.id);
    assert.equal(members.length,6);assert.equal(members.filter(seat=>seat.furniture==='sofa').length,cluster.style==='sofa-row'?6:3);
    assert.equal(members.filter(seat=>seat.furniture==='armchair').length,cluster.style==='sofa-row'?0:3);
    if(cluster.sofas){assert.equal(cluster.sofas.length,2);assert.ok(cluster.sofas.every(sofa=>sofa.seat_ids.length===3));assert.ok(Math.abs(cluster.sofas[1].x-cluster.sofas[0].x-3.28)<1e-8);assert.equal(cluster.sofas[0].z,cluster.sofas[1].z);}
    assert.ok(orthogonal(cluster.rotation)&&orthogonal(cluster.sofa.rotation));
  }
  for(const [index,room] of layout.rooms.entries()){
    const seats=layout.seats.filter(seat=>seat.room_id===room.id);
    assert.equal(seats.length,OFFICE_CAPACITIES[index]);assert.equal(room.desk_ids.length,room.capacity);
    assert.deepEqual(new Set(room.desk_ids),new Set(seats.map(seat=>seat.id)));
    for(const seat of seats){
      assert.equal(seat.kind,'desk');assert.equal(seat.yaw,(Math.PI+seat.rotation)%(2*Math.PI));
      for(const box of parts(seat)){
        assert.ok(box.minX>room.minX&&box.maxX<room.maxX,`${seat.id} crosses sidewall`);
        assert.ok(box.minZ>room.minZ&&box.maxZ<room.maxZ,`${seat.id} crosses front/back wall`);
      }
    }
    for(let a=0;a<seats.length;a++)for(let b=a+1;b<seats.length;b++)for(const one of parts(seats[a]))for(const two of parts(seats[b]))assert.ok(separate(one,two),`${seats[a].id} furniture overlaps ${seats[b].id}`);
    for(let desk=1;desk<=4;desk++)assert.ok(room.desk_ids.includes(`gf-office-${index+1}-${String(desk).padStart(3,'0')}`));
    assert.ok(room.entrance.width>=2);
    if(room.capacity===8){
      const benches=seats.map(seat=>parts(seat)[0]);
      assert.ok(benches.filter(box=>box.maxX<=room.x).every(box=>box.maxX<=room.x-.6+1e-8));
      assert.ok(benches.filter(box=>box.minX>=room.x).every(box=>box.minX>=room.x+.6-1e-8));
      assert.ok(benches.every(box=>box.minZ>room.minZ+1.8&&box.maxZ<room.maxZ-1.8));
    }
  }
  for(let a=0;a<layout.rooms.length;a++)for(let b=a+1;b<layout.rooms.length;b++)assert.ok(separate(layout.rooms[a],layout.rooms[b]));
  const ceo=layout.ceo;assert.equal(ceo.id,'ceo-office');assert.equal(ceo.capacity,1);assert.equal(ceo.width,8.8);assert.equal(ceo.depth,11.8);
  assert.deepEqual(ceo.desk_ids,['gf-ceo-001']);
  const ceoSeat=layout.seats.find(seat=>seat.id==='gf-ceo-001');assert.ok(ceoSeat.executive&&ceoSeat.room_id===ceo.id);
  for(const box of parts(ceoSeat))assert.ok(box.minX>ceo.minX&&box.maxX<ceo.maxX&&box.minZ>ceo.minZ&&box.maxZ<ceo.maxZ);
  assert.ok(layout.rooms.every(room=>separate(ceo,room)));assert.ok(layout.rooms[3].minZ-ceo.maxZ>=1.6);
  assert.equal(ceo.entrance.side,'south');assert.equal(ceo.entrance.width,2.2);
  const last=layout.rooms[4];assert.equal(last.entrance.z,last.z+2.8);assert.ok(last.entrance.z+last.entrance.width/2<last.maxZ);
  assert.ok(layout.pantry.label.includes('Pantry'));assert.ok(layout.pantry.z-layout.pantry.depth/2>last.maxZ+1);
});

test('enrollment and overflow preserve every saved lounge and office seat identity and coordinate',()=>{
  const original=groundLayout(0),byId=new Map(original.seats.map(seat=>[seat.id,seat]));
  for(const count of [1,2,21,36,37,80,275,400]){
    const expanded=groundLayout(count),current=new Map(expanded.seats.map(seat=>[seat.id,seat]));
    assert.ok(expanded.mainCount>=count);assert.ok(expanded.mainCount<=400);
    for(const [id,seat] of byId)assert.deepEqual(current.get(id),seat);
    assert.equal(current.size,expanded.seats.length);
    assert.equal(new Set(expanded.seats.map(seat=>`${seat.x.toFixed(6)}:${seat.z.toFixed(6)}`)).size,expanded.seats.length);
    assert.deepEqual(expanded.rooms,original.rooms);
    assert.equal(expanded.minX,original.minX);assert.equal(expanded.maxX,original.maxX);assert.equal(expanded.minZ,original.minZ);
    assert.ok(expanded.seats.every(seat=>seat.x>expanded.minX+1.5&&seat.x<expanded.maxX-1.5&&seat.z>expanded.minZ+1.5&&seat.z<expanded.maxZ-1.5));
    assert.ok(expanded.seats.every(seat=>orthogonal(seat.yaw)));
  }
  assert.ok(groundLayout(275).seats.some(seat=>seat.id==='gf-main-275'));
  assert.ok(groundLayout(400).seats.some(seat=>seat.id==='gf-main-400'));
});

test('central walkways stay clear of desks, chairs and the pantry',()=>{
  const layout=groundLayout(36);
  for(const aisle of layout.aisles){
    const walk={minX:aisle.x-aisle.width/2,maxX:aisle.x+aisle.width/2,minZ:aisle.z-aisle.depth/2,maxZ:aisle.z+aisle.depth/2};
    for(const seat of layout.seats){
      const furniture=seat.kind==='desk'?parts(seat):[{minX:seat.x-.55,maxX:seat.x+.55,minZ:seat.z-.55,maxZ:seat.z+.55}];
      assert.ok(furniture.every(box=>separate(walk,box)),`${seat.id} blocks a central walkway`);
    }
    const pantry=layout.pantry;assert.ok(separate(walk,{minX:pantry.x-pantry.width/2,maxX:pantry.x+pantry.width/2,minZ:pantry.z-pantry.depth/2,maxZ:pantry.z+pantry.depth/2}));
  }
});
