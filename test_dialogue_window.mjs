import test from 'node:test';
import assert from 'node:assert/strict';
import {DialogueWindow,clampDialogueFrame,changeDialogueFrame} from './public/office/scene/dialogue-window.js';

const viewport={width:1000,height:800};
const frame={left:200,top:150,width:450,height:400};

test('floating geometry stays inside tiny screens and rejects invalid saved dimensions',()=>{
  for(const size of [{width:230,height:170},{width:1,height:1},viewport]){
    const rect=clampDialogueFrame({left:Infinity,top:-400,width:NaN,height:99999},size);
    assert.ok(Object.values(rect).every(Number.isFinite));
    assert.ok(rect.left>=0&&rect.top>=0&&rect.width>0&&rect.height>0);
    assert.ok(rect.left+rect.width<=size.width&&rect.top+rect.height<=size.height);
  }
});

test('moving and resizing clamp without moving the opposite resize edge',()=>{
  const moved=changeDialogueFrame(frame,'move',9999,-9999,viewport);
  assert.equal(moved.left+moved.width,988);assert.equal(moved.top,12);
  const west=changeDialogueFrame(frame,'w',9999,0,viewport);
  assert.equal(west.width,320);assert.equal(west.left+west.width,650);
  const north=changeDialogueFrame(frame,'n',0,9999,viewport);
  assert.equal(north.height,260);assert.equal(north.top+north.height,550);
  const corner=changeDialogueFrame(frame,'se',9999,9999,viewport);
  assert.equal(corner.left,200);assert.equal(corner.top,150);
  assert.equal(corner.left+corner.width,988);assert.equal(corner.top+corner.height,788);
});

class Target {
  constructor(){this.events=new Map();}
  addEventListener(type,callback){if(!this.events.has(type))this.events.set(type,new Set());this.events.get(type).add(callback);}
  removeEventListener(type,callback){this.events.get(type)?.delete(callback);}
  emit(type,extra={}){
    const event={type,target:this,button:0,isPrimary:true,pointerId:1,clientX:300,clientY:200,
      preventDefault(){this.prevented=true;},stopPropagation(){this.stopped=true;},...extra};
    for(const callback of [...(this.events.get(type)||[])])callback(event);return event;
  }
  count(){return [...this.events.values()].reduce((sum,set)=>sum+set.size,0);}
}
class Node extends Target {
  constructor(tag='div'){
    super();this.tag=tag;this.style={};this.dataset={};this.attributes={};this.children=[];this.parentNode=null;
    this.scrollTop=0;this.textContent='';this.captures=new Set();
    const classes=new Set();this.classList={add:key=>classes.add(key),remove:key=>classes.delete(key),contains:key=>classes.has(key)};
  }
  setAttribute(key,value){this.attributes[key]=value;}
  append(...nodes){for(const node of nodes)this.insertBefore(node,null);}
  insertBefore(node,before){node.remove();const at=this.children.indexOf(before);this.children.splice(at<0?this.children.length:at,0,node);node.parentNode=this;}
  remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(node=>node!==this);this.parentNode=null;}
  get nextSibling(){const children=this.parentNode?.children||[];return children[children.indexOf(this)+1]||null;}
  contains(node){return node===this||this.children.some(child=>child.contains(node));}
  closest(){return ['button','input','textarea','a','select'].includes(this.tag)?this:null;}
  focus(){document.activeElement=this;}
  getBoundingClientRect(){return {left:150,top:120,width:350,height:300};}
  setPointerCapture(id){this.captures.add(id);}
  hasPointerCapture(id){return this.captures.has(id);}
  releasePointerCapture(id){this.captures.delete(id);}
}
function fixture(t,saved=null){
  const original={window:globalThis.window,document:globalThis.document};
  const values=new Map();if(saved)values.set('office-dialogue-frame-v1',JSON.stringify(saved));
  const win=Object.assign(new Target(),{innerWidth:1000,innerHeight:800,
    localStorage:{getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value)}});
  const doc=Object.assign(new Target(),{body:new Node('body'),createElement:tag=>new Node(tag),hidden:false});
  globalThis.window=win;globalThis.document=doc;
  const home=new Node(),element=new Node('section'),handle=new Node(),list=new Node(),input=new Node('textarea'),tail=new Node();
  element.append(handle,list,input);home.append(element,tail);doc.body.append(home);
  element.style.maxHeight='290px';list.style.maxHeight='110px';list.scrollTop=75;input.value='保留草稿';input.focus();
  let changes=0,docks=0;
  const controller=new DialogueWindow({element,handle,home,list,onChange:()=>changes++,onDock:()=>docks++});
  t.after(()=>{controller.dispose();for(const [key,value] of Object.entries(original)){
    if(value===undefined)delete globalThis[key];else globalThis[key]=value;
  }});
  return {controller,home,element,handle,list,input,tail,win,doc,values,
    get changes(){return changes;},get docks(){return docks;}};
}

test('detach and dock retain one reader, message list, focused draft and original insertion point',t=>{
  const f=fixture(t);f.controller.open();
  assert.equal(f.controller.floating,false);assert.equal(f.element.parentNode,f.home);
  f.controller.float();
  assert.equal(f.element.parentNode,f.doc.body);assert.equal(f.controller.floating,true);
  assert.equal(f.controller.frame.width,440);assert.equal(f.controller.frame.height,420);
  assert.equal(f.input.value,'保留草稿');assert.equal(f.doc.activeElement,f.input);assert.equal(f.list.scrollTop,75);
  assert.equal(f.list.style.maxHeight,'none');assert.equal(f.element.style.maxHeight,'none');
  f.controller.dock();
  assert.deepEqual(f.home.children,[f.element,f.tail]);assert.equal(f.element.style.maxHeight,'290px');
  assert.equal(f.list.style.maxHeight,'110px');assert.equal(f.doc.activeElement,f.input);
  assert.equal(f.controller.floating,false);assert.equal(f.docks,1);assert.equal(f.changes,2);
});

test('layout preferences contain geometry only and reopening preserves float choice',t=>{
  const f=fixture(t);f.controller.label('Private Agent Name');f.controller.open();f.controller.float();
  const raw=f.values.get('office-dialogue-frame-v1'),saved=JSON.parse(raw);
  assert.deepEqual(Object.keys(saved).sort(),['floating','frame']);
  assert.equal(saved.floating,true);assert.deepEqual(Object.keys(saved.frame).sort(),['height','left','top','width']);
  assert.equal(raw.includes('Private'),false);assert.equal(raw.includes('草稿'),false);
  f.controller.close();assert.equal(f.element.parentNode,f.home);assert.equal(f.win.count(),0);assert.equal(f.doc.count(),0);
  assert.equal(f.values.get('office-dialogue-frame-v1'),raw);
  f.controller.open();assert.equal(f.controller.floating,true);assert.equal(f.element.parentNode,f.doc.body);
});

test('viewport resize follows latest replies, preserves older reading, and moving does not read scroll geometry',t=>{
  const f=fixture(t);let top=0,reads=0,client=220;
  f.list.scrollHeight=1000;
  Object.defineProperty(f.list,'clientHeight',{get:()=>{reads++;return client;}});
  Object.defineProperty(f.list,'scrollTop',{get:()=>top,set:value=>{top=Math.max(0,Math.min(value,1000-client));}});
  f.element.style=new Proxy(f.element.style,{set(target,key,value){target[key]=value;if(key==='height')client=Math.max(36,parseFloat(value)-200);return true;}});
  f.controller.open();f.controller.float();f.list.scrollTop=1000;
  f.controller.apply({...f.controller.frame,height:300});assert.equal(top,900,'keyboard-sized reader keeps the latest reply in view');
  f.list.scrollTop=120;f.controller.apply({...f.controller.frame,height:450});assert.equal(top,120,'resizing never jumps away from manually selected older content');
  const before=reads;f.controller.apply({...f.controller.frame,left:30});assert.equal(reads,before,'position-only changes do not measure scrolling');
  assert.equal(top,120);
});

test('a new reader uses shared saved geometry without storing or binding Agent identity',t=>{
  const f=fixture(t,{floating:true,frame:{left:900,top:700,width:600,height:450}});
  f.controller.open();assert.equal(f.controller.floating,true);
  assert.deepEqual(f.controller.frame,{left:388,top:338,width:600,height:450});
});

test('a docked shared preference keeps its latest geometry on the next explicit float',t=>{
  const f=fixture(t,{floating:false,frame:{left:70,top:80,width:520,height:470}});
  f.controller.frame={...frame};f.controller.open();assert.equal(f.controller.floating,false);
  f.controller.float();assert.deepEqual(f.controller.frame,{left:70,top:80,width:520,height:470});
});

test('clicking title does not detach; only primary dragging does, and unrelated pointers are ignored',t=>{
  const f=fixture(t);f.controller.open();
  f.handle.emit('pointerdown',{button:2});assert.equal(f.controller.gesture,null);
  f.handle.emit('pointerdown',{isPrimary:false});assert.equal(f.controller.gesture,null);
  f.handle.emit('pointerdown');f.handle.emit('pointermove',{clientX:302});f.handle.emit('pointerup');
  assert.equal(f.controller.floating,false);assert.equal(f.handle.captures.size,0);
  f.handle.emit('pointerdown');f.handle.emit('pointermove',{pointerId:2,clientX:500});
  assert.equal(f.controller.floating,false);
  f.handle.emit('pointermove',{clientX:400,clientY:250});
  assert.equal(f.controller.floating,true);assert.equal(f.controller.frame.left,250);assert.equal(f.controller.frame.top,170);
  f.handle.emit('pointerup');assert.equal(f.controller.gesture,null);
  assert.equal(JSON.parse(f.values.get('office-dialogue-frame-v1')).floating,true);
});

test('cancelling initial drag restores the head reader; Escape restores a floating frame',t=>{
  const f=fixture(t);f.controller.open();
  f.handle.emit('pointerdown');f.handle.emit('pointermove',{clientX:500});f.handle.emit('pointercancel');
  assert.equal(f.controller.floating,false);assert.equal(f.element.parentNode,f.home);assert.equal(f.values.size,0);
  f.controller.float();const initial={...f.controller.frame};
  f.handle.emit('pointerdown');f.handle.emit('pointermove',{clientX:600});f.doc.emit('keydown',{key:'Escape'});
  assert.equal(f.controller.floating,true);assert.deepEqual(f.controller.frame,initial);assert.equal(f.controller.gesture,null);
});

test('capture loss, blur and hidden document terminate gestures and release all temporary handlers',t=>{
  const f=fixture(t);f.controller.open();f.controller.float();
  for(const finish of [()=>{f.handle.captures.clear();f.handle.emit('lostpointercapture');},
    ()=>f.win.emit('blur'),()=>{f.doc.hidden=true;f.doc.emit('visibilitychange');}]){
    const before=f.handle.count(),rect={...f.controller.frame};
    f.doc.hidden=false;f.handle.emit('pointerdown');f.handle.emit('pointermove',{clientX:500});finish();
    assert.equal(f.controller.gesture,null);assert.equal(f.handle.count(),before);assert.deepEqual(f.controller.frame,rect);
    assert.equal(f.handle.captures.size,0);assert.equal(f.doc.events.get('keydown')?.size||0,0);
  }
});

test('reacquired pointer capture survives an older queued lost-capture notification',t=>{
  const f=fixture(t);f.controller.open();f.handle.emit('pointerdown');
  f.handle.emit('pointermove',{clientX:350});f.handle.emit('lostpointercapture');
  assert.ok(f.controller.gesture);f.handle.emit('pointerup');assert.equal(f.controller.floating,true);
});

test('viewport shrinking clamps an open floating reader and cleans listeners when closed',t=>{
  const f=fixture(t);f.controller.open();f.controller.float();
  f.win.innerWidth=250;f.win.innerHeight=210;f.win.emit('resize');
  assert.deepEqual(f.controller.frame,{left:12,top:12,width:226,height:186});
  f.controller.close();assert.equal(f.win.count(),0);assert.equal(f.doc.count(),0);
  f.controller.close();assert.equal(f.win.count(),0);
});

test('keyboard supports title movement and corner resizing without browser scrolling',t=>{
  const f=fixture(t);f.controller.open();
  const event=f.handle.emit('keydown',{key:'ArrowRight',shiftKey:true});
  assert.equal(event.prevented,true);assert.equal(event.stopped,true);assert.equal(f.controller.floating,true);
  assert.equal(f.controller.frame.left,182);
  const corner=f.controller.resizeHandles.find(node=>node.dataset.edge==='se'),width=f.controller.frame.width;
  corner.emit('keydown',{key:'ArrowRight'});assert.equal(f.controller.frame.width,width+8);
  const before={...f.controller.frame};corner.emit('keydown',{key:'ArrowRight',ctrlKey:true});
  assert.deepEqual(f.controller.frame,before);
});

test('dispose during a gesture removes capture, portal, global listeners and all control handlers',t=>{
  const f=fixture(t);f.controller.open();f.handle.emit('pointerdown');f.handle.emit('pointermove',{clientX:500});
  const handles=[...f.controller.resizeHandles];f.controller.dispose();
  assert.equal(f.controller.disposed,true);assert.equal(f.controller.floating,false);assert.equal(f.element.parentNode,f.home);
  assert.equal(f.win.count(),0);assert.equal(f.doc.count(),0);assert.equal(f.handle.count(),0);assert.equal(f.element.count(),0);
  assert.ok(handles.every(node=>node.count()===0&&node.parentNode===null));
  f.controller.open();assert.equal(f.controller.opened,false);
});

test('unavailable browser storage does not prevent moving or docking',t=>{
  const f=fixture(t);f.win.localStorage={getItem(){throw new Error('denied');},setItem(){throw new Error('denied');}};
  f.controller.open();f.controller.float();assert.equal(f.controller.floating,true);
  f.controller.dock();assert.equal(f.controller.floating,false);
});
