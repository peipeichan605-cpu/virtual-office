import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {IdleRenderController,meaningfulOfficeSignature,hasWorkingAgents,hasSceneTransition,observePublicDialogue} from './public/office/scene/idle-render.js';
import {createFramePacer} from './public/office/scene/frame-pacer.js';
import {Spring,clamp,smoothstep} from './public/office/scene/spring.js';

const fixture=()=>({now:'2026-01-01',version:1,bots:{one:{id:'one',agent_id:'generic:one',name:'One',floor:'floor1',status:'idle',effective_status:'idle',step:'Resting',latest_message:{event_id:'event1',seq:1,phase:'final',role:'assistant',text:'Delivered',at:'2026-01-01T00:00:00Z'},command_capability:{persistent:true,mode:'generic',public_sync:{state:'configured'}}}},floors:[{id:'floor1',name:'Workspace',order:1,bots:['one']}],finished:[{run_id:'run1',bot:'one',status:'done',step:'Delivered',t:'2026-01-01T00:00:00Z'}]});
const view={currentFloor:'floor1',selectedBot:null,simpleMode:false};
const settled=()=>({settled:true});
const building=()=>({pulseT:-1,introDrop:false,slots:[{lift:settled(),open:{alpha:1,screens:[{glow:1,target:1}]}}],roofLift:settled(),cabinY:settled(),shaftTop:settled()});

test('five seconds of quiet switches to persistent one-hertz idle without a waiting limit',()=>{
  const modes=[],control=new IdleRenderController({onTransition:event=>modes.push(event)});
  control.observe(fixture(),view,0);assert.deepEqual(control.frame(0),{draw:true,idle:false});control.didRender(0);
  assert.equal(control.frame(4990).idle,false);control.didRender(4990);
  assert.deepEqual(control.frame(5000),{draw:false,idle:true});
  assert.deepEqual(control.frame(5990),{draw:true,idle:true});control.didRender(5990);
  for(let at=6990;at<=7206990;at+=1000){assert.deepEqual(control.frame(at),{draw:true,idle:true});control.didRender(at);assert.equal(control.frame(at+1).draw,false);}
  assert.equal(modes.length,1);assert.equal(modes[0].idle,true);
  control.wake(7207000,'interaction');assert.deepEqual(control.frame(7207000),{draw:true,idle:false});assert.equal(modes.at(-1).idle,false);
});

test('timestamp-only feed polls and aggregate age/counters never reset inactivity',()=>{
  const control=new IdleRenderController(),state=fixture();control.observe(state,view,0);control.didRender(0);
  for(let second=1;second<7200;second++){
    const next=structuredClone(state);next.now=String(second);next.version=second;next.bots.one.last_seen=second;next.bots.one.since=second;next.bots.one.last_event=second;
    next.bots.one.today={working_min:second,events:second};next.bots.one.command_capability.last_seen=second;
    next.bots.one.task={id:'same',title:'Same task',elapsed:second,updated_at:second};
    state.bots.one.task={id:'same',title:'Same task'};
    if(second===1){control.observe(state,view,0);}
    assert.equal(control.observe(next,view,second*1000),false);const plan=control.frame(second*1000);if(plan.draw)control.didRender(second*1000);
  }
  assert.equal(control.idle,true);assert.equal(control.lastActivity,0);
});

test('public messages, same-text new event, delivery, membership, layout and view changes wake immediately',()=>{
  const mutations=[
    state=>state.bots.one.latest_message.text='New public response',
    state=>state.bots.one.latest_message.event_id='event2',
    state=>state.bots.one.latest_message.at='2026-01-01T00:01:00Z',
    state=>state.finished[0].step='New delivery body',
    state=>state.finished[0].run_id='run2',
    state=>state.finished[0].t='2026-01-01T00:01:00Z',
    state=>state.bots.two={id:'two',status:'waiting_user'},
    state=>delete state.bots.one,
    state=>state.bots.one.ground_desk_id='desk2',
    state=>state.bots.one.look={shape:'round',color:'blue'},
    state=>state.bots.one.command_capability.public_sync.state='unavailable',
    state=>state.floors[0].name='Changed workspace',
  ];
  for(const mutate of mutations){
    const state=fixture(),control=new IdleRenderController();control.observe(state,view,0);control.didRender(0);assert.equal(control.frame(6000).idle,true);
    mutate(state);assert.equal(control.observe(state,view,6001),true);assert.deepEqual(control.frame(6001),{draw:true,idle:false});
  }
  for(const update of [{currentFloor:'other'},{selectedBot:'one'},{simpleMode:true}]){
    const control=new IdleRenderController();control.observe(fixture(),view,0);control.didRender(0);control.frame(6000);
    assert.equal(control.observe(fixture(),{...view,...update},6001),true);assert.equal(control.idle,false);
  }
  const a=fixture(),b=fixture();a.bots.one.look={shape:'round',color:'blue'};b.bots.one.look={color:'blue',shape:'round'};
  assert.equal(meaningfulOfficeSignature(a,view),meaningfulOfficeSignature(b,view),'object insertion order is not new activity');
});

test('working members or children on any floor keep normal rendering',()=>{
  const state=fixture();state.bots.hidden={id:'hidden',floor:'other',status:'working',effective_status:'stuck',is_subagent:true};
  assert.equal(hasWorkingAgents(state),true,'a stale display status cannot freeze a source still reporting work');
  const control=new IdleRenderController();control.observe(state,view,0);control.didRender(0);
  assert.deepEqual(control.frame(7200000),{draw:true,idle:false});
  state.bots.hidden.status='done';state.bots.hidden.effective_status='done';control.observe(state,view,7200001);
  assert.equal(control.frame(7204999).idle,false);assert.equal(control.frame(7205001).idle,true);
});

test('real transitions inhibit sleep while recurring cosmetics do not reset its deadline',()=>{
  const actor={blinkT:-1,nextAction:-5,pivot:{scale:{x:1.01}},ring:{material:{opacity:.45}},scaleS:settled(),yawS:settled(),eyeX:settled(),eyeY:settled()};
  const base={camera:{zoom:settled()},building:building(),actors:[actor],retiring:new Set(),effects:[]};
  assert.equal(hasSceneTransition(base),false,'breathing, scheduled waiting hops and pulse values are cosmetic');
  for(const change of [f=>f.drag={},f=>f.pinch={},f=>f.seatSaving=true,f=>f.effects=[{}],f=>f.retiring=new Set([{}]),f=>f.camera.zoom.settled=false,f=>f.building.introDrop=true,f=>f.building.pulseT=0,f=>f.building.slots[0].lift.settled=false,f=>f.building.slots[0].open.screens[0].target=0]){
    const next=structuredClone(base);change(next);assert.equal(hasSceneTransition(next),true);
  }
  for(const anim of ['travel','drop','hop']){
    actor.anim={kind:anim};assert.equal(hasSceneTransition(base),true);actor.anim=null;
  }
  actor.fx={};assert.equal(hasSceneTransition(base),true);actor.fx=null;
  const control=new IdleRenderController();control.observe(fixture(),view,0);control.didRender(0);
  for(let now=0;now<=5400;now+=100){const busy=now>=4900;assert.equal(control.frame(now,{busy}).idle,false);control.didRender(now);}
  assert.equal(control.lastActivity,0,'finishing a hop does not restart the five-second timer');
  assert.equal(control.frame(5500,{busy:false}).idle,true);
});

test('hidden pages do not draw and resume wakes; disabled policy preserves normal rendering',()=>{
  const control=new IdleRenderController();control.observe(fixture(),view,0);control.didRender(0);control.frame(6000);
  assert.equal(control.frame(6001,{hidden:true}).draw,false);assert.equal(control.frame(7200000,{hidden:true}).draw,false);
  assert.deepEqual(control.frame(7200001),{draw:true,idle:false});assert.equal(control.lastActivity,7200001);
  const off=new IdleRenderController({enabled:false});off.observe(fixture(),view,0);off.didRender(0);
  assert.deepEqual(off.frame(7200000),{draw:true,idle:false});
});

test('dialogue observer compares semantic text, ignoring identical poll replacements and render styles',()=>{
  let texts=['Public response','Completed'],wakes=0;
  const element={nodeType:1,matches:()=>true};
  const root={querySelectorAll:()=>texts.map(textContent=>({textContent})),querySelector:()=>element};
  class Observer{constructor(callback){this.callback=callback;}observe(target,options){this.target=target;this.options=options;}disconnect(){}}
  const observer=observePublicDialogue(root,()=>wakes++,Observer);
  assert.equal(observer.options.attributes,undefined,'canvas/label style writes are not observed');
  for(let i=0;i<100;i++)observer.callback([{target:element,addedNodes:[],removedNodes:[]}]);
  assert.equal(wakes,0);texts=['New public response','Completed'];observer.callback([{target:element}]);assert.equal(wakes,1);
  observer.callback([{target:element}]);assert.equal(wakes,1);
  texts.push('New delivery');observer.callback([{target:root,addedNodes:[element],removedNodes:[]}]);assert.equal(wakes,2);
});

test('actual frame loop freezes cosmetics at idle heartbeat and resumes without elapsed-time catch-up',async()=>{
  const source=await readFile(new URL('./public/office/scene/scene.js',import.meta.url),'utf8');
  const start=source.indexOf('\nfunction frame(t) {');assert.ok(start>0);
  const frameSource=source.slice(start).trim();
  let now=0,lastClock=0,actorTicks=0,effectTicks=0,renders=0;const lightDeltas=[],physicsDeltas=[];
  const control=new IdleRenderController();control.observe(fixture(),view,0);
  const model={blinkT:-1,tick(dt){actorTicks++;physicsDeltas.push(dt);}},models=new Map([['one',model]]);
  const scope={document:{hidden:false},clock:{elapsedTime:0,getDelta(){const delta=(now-lastClock)/1000;lastClock=now;this.elapsedTime=now/1000;return delta;}},performance:{now:()=>now},
    shouldDraw:createFramePacer(30),idleRender:control,idleProfile:false,hasSceneTransition,cam:{zoom:settled()},building:{...building(),activeOpen:()=>({alpha:1}),cabinY:{...settled(),x:0},update(){}},
    actors:models,retiring:new Set(),effects:[],drag:null,pinch:null,seatSaving:false,followerMotion:{moving:false},lastRenderMs:null,frozen:false,nowSec:0,
    daylight:{update(dt){lightDeltas.push(dt);}},stepCamera(){},pointerDirty:false,setHover(){},pick(){},updateCursorGround(){},positionFollowers(){},stepEffects(){effectTicks++;},
    post:{render(){renders++;}},focusV:{set(){return this;},project(){return {y:0};}},clamp:(v,a,b)=>Math.min(b,Math.max(a,v)),camera:{zoom:1},
    WALLPAPER:false,WALLPAPER_TAGS:false,layoutDialogues(){},labels:{render(){}},scene:{},renderer:{render(){throw Error('unexpected fallback');}}};
  const frame=Function(...Object.keys(scope),`${frameSource}\nreturn frame;`)(...Object.values(scope));
  for(now=0;now<5000;now+=1000/60)frame(now);
  const activeTicks=actorTicks;assert.ok(activeTicks>=148&&activeTicks<=152);assert.equal(effectTicks,activeTicks);
  for(;now<12000;now+=1000/60)frame(now);
  assert.equal(control.idle,true);assert.equal(actorTicks,activeTicks);assert.equal(effectTicks,activeTicks);
  assert.ok(renders-activeTicks>=6&&renders-activeTicks<=8,'idle still refreshes lighting and labels about once a second');
  assert.ok(lightDeltas.some(dt=>dt>.9),'lighting uses elapsed heartbeat time, not only the latest RAF delta');
  assert.ok(physicsDeltas.every(dt=>dt<=.05));
  scope.followerMotion.moving=true;
  for(let n=0;n<4;n++){now+=1000/30;frame(now);}
  assert.equal(control.idle,false,'a travelling child resumes full rendering without a new Agent event');
  const followingTicks=actorTicks;assert.ok(followingTicks>activeTicks);
  scope.followerMotion.moving=false;
  const settledAt=now;
  for(;now<settledAt+6500;now+=1000/60)frame(now);
  assert.equal(control.idle,true,'finished following returns to idle rendering');
  control.wake(now,'message');frame(now);now+=1000/30;frame(now);
  assert.equal(control.idle,false);assert.ok(actorTicks>activeTicks);assert.ok(physicsDeltas.at(-1)<=.05);
  scope.document.hidden=true;const beforeHidden=renders;now+=1000;frame(now);assert.equal(renders,beforeHidden);
  scope.document.hidden=false;now+=7200000;frame(now);assert.ok(renders>beforeHidden);assert.ok(physicsDeltas.at(-1)<=.05);
});

async function actualActorFixture() {
  const source=await readFile(new URL('./public/office/scene/actor.js',import.meta.url),'utf8');
  const constants=source.slice(source.indexOf('const CAMERA_YAW'),source.indexOf('const ringTexture'));
  const methods=source.slice(source.indexOf('export class Actor'),source.indexOf('// ---------- helpers ----------')).replace('export class Actor','class Actor');
  const helpers=source.slice(source.indexOf('// ---------- helpers ----------')).replaceAll('export function','function');
  class Vector {
    constructor(x=0,y=0,z=0){this.set(x,y,z);}set(x,y,z){Object.assign(this,{x,y,z});return this;}
    setScalar(n){return this.set(n,n,n);}copy(other){return this.set(other.x,other.y,other.z);}clone(){return new Vector(this.x,this.y,this.z);}
    subVectors(a,b){return this.set(a.x-b.x,a.y-b.y,a.z-b.z);}distanceTo(other){return Math.hypot(this.x-other.x,this.y-other.y,this.z-other.z);}
    lerpVectors(a,b,k){return this.set(a.x+(b.x-a.x)*k,a.y+(b.y-a.y)*k,a.z+(b.z-a.z)*k);}
  }
  const math=Object.create(Math);math.random=()=>2/15; // waiting interval1.78s + hop.62s =2.4s
  const Actor=Function('Spring','REDUCED','clamp','smoothstep','THREE','Math',`${constants}\nconst CELEBRATE_S=.95;\n${methods}\n${helpers}\nreturn Actor;`)(Spring,false,clamp,smoothstep,{Vector3:Vector},math);
  const make=(phase=0)=>{
    const actor=Object.create(Actor.prototype);
    Object.assign(actor,{status:'waiting_user',phase,retiring:false,hostAlpha:1,hovered:false,selected:false,baseYaw:0,modelScale:1,
      ctx:{cursor:{active:false}},root:{position:new Vector()},pivot:{position:new Vector(),scale:new Vector(1,1,1),rotation:new Vector()},
      yawS:new Spring(.42),scaleS:new Spring(1),eyeX:new Spring(0),eyeY:new Spring(0),ring:{material:{},scale:new Vector()},
      speech:{},tag:{},tagEl:{dataset:{mode:'off'}},dialogue:{visible:true,open:false},eyes:[{scale:new Vector(1,1,1),position:new Vector()}],eyeBase:[1],eyePos:[{x:0,y:0}],
      nextAction:phase<.62?1.78:2.4-phase,hopCooldown:1,nextBlink:phase+.2,blinkT:-1,joy:0,
      anim:phase<.62?{kind:'hop',t:phase,dur:.62,h:.32}:null});
    actor.spotIsLounge=()=>true;return actor;
  };
  return {make,Vector};
}

test('four and twenty staggered waiting agents finish current cosmetics and sleep without a shared quiet gap',async()=>{
  const {make}=await actualActorFixture();
  for(const count of [4,20]){
    const models=Array.from({length:count},(_,index)=>make(index*2.4/count));
    const state=fixture();state.bots=Object.fromEntries(models.map((_,index)=>['agent'+index,{id:'agent'+index,status:'waiting_user'}]));
    const control=new IdleRenderController();control.observe(state,{},0);
    let firstIdle=null,ticks=0,afterIdle=0;
    for(let now=0;now<=60000;now+=1000/30){
      const busy=hasSceneTransition({camera:{},building:building(),actors:models});
      const plan=control.frame(now,{busy});if(plan.idle)firstIdle ??= now;if(!plan.draw)continue;
      if(plan.idle){afterIdle++;}
      else for(const actor of models){actor.tick(1/30,now/1000,{spawnCosmetics:!control.quietDue(now)});ticks++;}
      control.didRender(now);
    }
    assert.ok(firstIdle>=5000&&firstIdle<5800,`${count} staggered agents must finish in-flight cosmetics and sleep; got${firstIdle}`);
    assert.ok(afterIdle>=53&&afterIdle<=55);assert.ok(ticks<count*175);
    for(const actor of models){assert.equal(actor.anim,null);assert.equal(actor.blinkT,-1);assert.equal(actor.pivot.position.y,0,'no midair freeze');}
    control.wake(61000,'message');assert.equal(control.frame(61000).idle,false);
    models[0].nextAction=0;models[0].tick(1/30,61,{spawnCosmetics:!control.quietDue(61000)});assert.equal(models[0].anim?.kind,'hop','cosmetics resume after wake');
  }
});

test('real Actor suppresses only new cosmetics while preserving existing hop, drop and travel completion',async()=>{
  const {make,Vector}=await actualActorFixture();
  const actor=make(1);actor.nextAction=0;actor.nextBlink=0;actor.ctx.cursor={active:true,x:0,z:0};actor.hopCooldown=0;
  actor.tick(1/30,0,{spawnCosmetics:false});assert.equal(actor.anim,null);assert.equal(actor.blinkT,-1);
  actor.tick(1/30,0);assert.equal(actor.anim?.kind,'hop','default Actor behavior is unchanged');assert.ok(actor.blinkT>=0);
  for(let frame=0;frame<25;frame++)actor.tick(1/30,frame/30,{spawnCosmetics:false});
  assert.equal(actor.anim,null);assert.equal(actor.blinkT,-1);assert.equal(actor.pivot.position.y,0);
  actor.ctx.cursor.active=false;
  actor.dropIn({pos:new Vector(1,0,0),yaw:0});for(let frame=0;frame<30;frame++)actor.tick(1/30,frame/30,{spawnCosmetics:false});
  assert.equal(actor.anim,null);assert.equal(actor.root.position.x,1);
  actor.travelTo({pos:new Vector(4,0,0),yaw:0});assert.equal(actor.anim.kind,'travel');
  for(let frame=0;frame<60;frame++)actor.tick(1/30,frame/30,{spawnCosmetics:false});
  assert.equal(actor.anim,null);assert.equal(actor.root.position.x,4);assert.equal(actor.pivot.position.y,0);
});

test('invisible or sleeping eyes clear stale blinks instead of preventing idle forever',async()=>{
  const {make}=await actualActorFixture();
  for(const noEyes of [false,true]){
    const actor=make(1);actor.status='idle';actor.spotIsLounge=()=>false;actor.anim=null;actor.blinkT=.05;
    if(noEyes)actor.eyes=[];
    actor.tickEyes(1/30,{spawnCosmetics:false});assert.equal(actor.blinkT,-1);
    assert.equal(hasSceneTransition({camera:{},building:building(),actors:[actor]}),false);
  }
});
