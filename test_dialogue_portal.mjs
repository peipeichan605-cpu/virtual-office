import test from 'node:test';
import assert from 'node:assert/strict';
import {DialogueBubble} from './public/office/scene/dialogue.js';

// Small DOM fixture with real parent/descendant relationships. Unlike the
// scope fixtures, a selector cannot invent a node after its subtree is moved.
class Target {
  constructor(){this.events=new Map();}
  addEventListener(type,fn){if(!this.events.has(type))this.events.set(type,new Set());this.events.get(type).add(fn);}
  removeEventListener(type,fn){this.events.get(type)?.delete(fn);}
  dispatchEvent(event){for(const fn of this.events.get(event.type)||[])fn(event);return true;}
  count(){return [...this.events.values()].reduce((sum,set)=>sum+set.size,0);}
}
class Node extends Target {
  constructor(tag='div',text=''){
    super();this.tag=tag;this.nodeType=tag==='#text'?3:1;this.text=text;
    this.children=[];this.parentNode=null;this.style={};this.dataset={};this.attributes={};this.value='';
    this.scrollTop=0;this.scrollHeight=300;this.clientHeight=100;this.offsetHeight=50;this.hidden=false;
    this.className='';this.captures=new Set();
    const set=()=>new Set(this.className.split(/\s+/).filter(Boolean));
    this.classList={contains:key=>set().has(key),add:key=>{const s=set();s.add(key);this.className=[...s].join(' ');},
      remove:key=>{const s=set();s.delete(key);this.className=[...s].join(' ');},
      toggle:(key,force)=>{const s=set(),on=force??!s.has(key);if(on)s.add(key);else s.delete(key);this.className=[...s].join(' ');return on;}};
  }
  get parentElement(){return this.parentNode?.nodeType===1?this.parentNode:null;}
  get nextSibling(){const a=this.parentNode?.children||[];return a[a.indexOf(this)+1]||null;}
  get firstChild(){return this.children[0]||null;}
  get textContent(){return this.nodeType===3?this.text:this.children.map(n=>n.textContent).join('');}
  set textContent(value){if(this.nodeType===3)this.text=String(value);else this.replaceChildren(new Node('#text',String(value)));}
  set innerHTML(markup){
    this.replaceChildren();const stack=[this];
    for(const token of markup.match(/<[^>]+>|[^<]+/g)||[]){
      if(token.startsWith('</')){stack.pop();continue;}
      if(!token.startsWith('<')){stack.at(-1).append(new Node('#text',token));continue;}
      const tag=token.match(/^<([\w-]+)/)?.[1];if(!tag)continue;
      const node=new Node(tag);
      for(const match of token.slice(tag.length+1,-1).matchAll(/([\w-]+)(?:="([^"]*)")?/g))node.setAttribute(match[1],match[2]??'');
      stack.at(-1).append(node);if(!['input','br','hr','img'].includes(tag))stack.push(node);
    }
  }
  setAttribute(key,value){this.attributes[key]=String(value);if(key==='class')this.className=String(value);if(key==='hidden')this.hidden=true;}
  append(...nodes){for(const node of nodes)this.insertBefore(node,null);}
  prepend(...nodes){for(const node of [...nodes].reverse())this.insertBefore(node,this.firstChild);}
  insertBefore(node,before){node.remove();const i=this.children.indexOf(before);this.children.splice(i<0?this.children.length:i,0,node);node.parentNode=this;}
  replaceChildren(...nodes){for(const child of this.children)child.parentNode=null;this.children=[];this.append(...nodes);}
  remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(n=>n!==this);this.parentNode=null;}
  contains(node){return this===node||this.children.some(n=>n.contains(node));}
  matches(selector){return selector.split(',').some(raw=>{
    const s=raw.trim();if(s.startsWith('.'))return this.classList.contains(s.slice(1));
    const attr=s.match(/^\[([^=\]]+)(?:=([^\]]+))?\]$/);if(attr)return attr[1] in this.attributes&&(attr[2]===undefined||this.attributes[attr[1]]===attr[2]);
    return this.tag===s;
  });}
  closest(selector){return this.matches(selector)?this:this.parentElement?.closest(selector)||null;}
  querySelectorAll(selector){
    const selectors=selector.split(',').map(s=>s.trim()),all=[];
    const scan=node=>{for(const child of node.children){if(selectors.some(s=>{
      const parts=s.split(/\s+/);return parts.length===1?child.matches(s):child.matches(parts.at(-1))&&!!child.parentElement?.closest(parts.slice(0,-1).join(' '));
    }))all.push(child);scan(child);}};scan(this);return all;
  }
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  focus(){document.activeElement=this;}
  getBoundingClientRect(){return {left:140,top:100,width:350,height:330};}
  setPointerCapture(id){this.captures.add(id);}
  hasPointerCapture(id){return this.captures.has(id);}
  releasePointerCapture(id){this.captures.delete(id);}
}

function fixture(t){
  const original=Object.fromEntries(['document','window','location','sessionStorage','MutationObserver'].map(k=>[k,globalThis[k]]));
  const saved=new Map(),observers=[];
  const storage={getItem:k=>saved.get(k)||null,setItem:(k,v)=>saved.set(k,v),removeItem:k=>saved.delete(k)};
  globalThis.window=Object.assign(new Target(),{innerWidth:1100,innerHeight:800,localStorage:storage});
  globalThis.document=Object.assign(new Target(),{body:new Node('body'),documentElement:new Node('html'),createElement:tag=>new Node(tag),hidden:false});
  globalThis.location={hash:'#fixture-office'};globalThis.sessionStorage=storage;
  globalThis.MutationObserver=class {
    constructor(callback){this.callback=callback;this.disconnected=false;observers.push(this);}
    observe(root,options){this.root=root;this.options=options;}
    disconnect(){this.disconnected=true;}
  };
  const readers=[];
  const make=(id,extra={})=>{
    const reader=new DialogueBubble();readers.push(reader);document.body.append(reader.element);
    reader.composer.refresh=()=>{};
    reader.update(bot(id,extra),true);return reader;
  };
  t.mock.method(globalThis,'fetch',async()=>({ok:true,json:async()=>({messages:[],cursor:0,before:null})}));
  t.after(()=>{for(const reader of readers)reader.dispose();for(const [key,value] of Object.entries(original))if(value===undefined)delete globalThis[key];else globalThis[key]=value;});
  return {make,observers,saved};
}
const bot=(id,extra={})=>({id,agent_id:'fixture:'+id,name:'Agent '+id,status:'working',chat_url:'/office/agent.html?agent='+id,
  command_capability:{mode:'agent_inbox',can_send:true,persistent:true},...extra});
const turn=()=>new Promise(resolve=>setImmediate(resolve));

test('phone head chats automatically float within the viewport and keep competing-card suppression across Agent changes',async t=>{
  const {make,saved}=fixture(t);location.pathname='/mobile/';
  window.innerWidth=412;window.innerHeight=915;
  const parent=make('phone-parent'),child=make('phone-child',{is_subagent:true});
  parent.setOpen(true);await turn();
  assert.equal(parent.window.floating,true);assert.equal(parent.detail.parentNode,document.body);
  const frame=parent.window.frame;
  assert.ok(frame.left>=0&&frame.top>=0&&frame.left+frame.width<=412&&frame.top+frame.height<=915);
  assert.equal(document.documentElement.classList.contains('office-mobile-dialogue-open'),true);
  assert.equal(saved.has('office-dialogue-frame-v1'),false,'automatic phone layout does not overwrite the desktop preference');
  child.setOpen(true);await turn();
  assert.equal(parent.open,false);assert.equal(child.open,true);assert.equal(child.window.floating,true);
  parent.closePanels();
  assert.equal(document.documentElement.classList.contains('office-mobile-dialogue-open'),true,'closing an old reader cannot expose a competing Bot card');
  window.innerHeight=360;child.window.onResize();
  assert.ok(child.window.frame.top+child.window.frame.height<=360,'keyboard-sized visual area contains the reader');
  child.setOpen(false);
  assert.equal(document.documentElement.classList.contains('office-mobile-dialogue-open'),false);
});

test('the in-place reader keeps its composer and has no navigation link while portalled',async t=>{
  const {make}=fixture(t),reader=make('A'),composer=reader.composer,input=composer.input;
  assert.equal(reader.detail.querySelector('.speech-history'),null);
  assert.deepEqual(reader.detail.querySelectorAll('a'),[],'the conversation must not navigate away from the office');
  reader.setOpen(true);await turn();input.value='保留呢段草稿';input.focus();reader.window.float();
  assert.equal(reader.detail.parentNode,document.body);assert.equal(reader.headExpanded,false);assert.equal(reader.expanded,true);
  assert.doesNotThrow(()=>reader.update(bot('A',{name:'Agent renamed',chat_url:'/office/agent.html?agent=A-new'}),true));
  assert.equal(reader.detail.querySelector('.speech-history'),null);
  assert.equal(reader.composer,composer);assert.equal(reader.composer.input,input);assert.equal(input.value,'保留呢段草稿');
  assert.equal(document.activeElement,input);assert.equal(reader.windowHandle.textContent,'⠿ Agent renamed');
  reader.window.dock();assert.equal(reader.detail.parentNode,reader.element);assert.equal(reader.headExpanded,true);
  assert.equal(reader.composer.input,input);assert.equal(input.value,'保留呢段草稿');
});

test('the phone close button closes the same floating reader and state refresh does not reopen it',async t=>{
  const {make}=fixture(t);location.pathname='/mobile/';window.innerWidth=412;window.innerHeight=915;
  const reader=make('close-test');reader.setOpen(true);await turn();
  reader.composer.input.value='Keep this unsent draft';
  const close=reader.detail.querySelector('.speech-close'),composer=reader.composer;
  assert.equal(reader.detail.parentNode,document.body);assert.equal(reader.open,true);
  close.dispatchEvent({type:'click'});
  assert.equal(reader.open,false);assert.equal(reader.detail.hidden,true);
  assert.equal(reader.detail.parentNode,reader.element);assert.equal(reader.window.floating,false);
  assert.equal(reader.timer,null);assert.equal(document.documentElement.classList.contains('office-mobile-dialogue-open'),false);
  reader.update(bot('close-test',{status:'done'}),true);
  assert.equal(reader.open,false,'retained selection and a fresh status must not reopen the closed reader');
  assert.equal(reader.composer,composer);assert.equal(reader.composer.input.value,'Keep this unsent draft');
  reader.peek.dispatchEvent({type:'click'});await turn();
  assert.equal(reader.open,true);assert.equal(reader.composer,composer);assert.equal(reader.detail.parentNode,document.body);
});

test('a portalled reader renders revision text in the original node and retains reading position',async t=>{
  const {make}=fixture(t),reader=make('A');let revision=1;
  t.mock.method(globalThis,'fetch',async()=>({ok:true,json:async()=>({cursor:revision,before:null,messages:[{
    event_id:'stable',seq:revision,agent_id:'fixture:A',run_id:'run-a',role:'assistant',phase:revision===1?'commentary':'final',text:revision===1?'準備中':'已交付',at:'2026-11-12T00:00:00Z'}]})}));
  reader.setOpen(true);await turn();reader.window.float();const article=reader.list.firstChild;
  reader.list.scrollTop=45;revision=2;await reader.refresh();
  assert.equal(reader.list.children.length,1);assert.equal(reader.list.firstChild,article);
  assert.equal(article.querySelector('p').textContent,'已交付');assert.equal(reader.list.scrollTop,45);
  assert.equal(reader.detail.parentNode,document.body);
});

// Hidden elements have no scroll geometry. Browsers also reset the scrolling
// box when the closed portal is detached/reinserted; preserving the DOM nodes
// alone therefore cannot preserve its reading location.
function closedPortalScroll(reader,{height=2287,client=176}={}){
  let top=0;
  reader.list.scrollHeight=height;reader.list.clientHeight=client;
  Object.defineProperty(reader.list,'scrollTop',{configurable:true,
    get:()=>reader.detail.hidden?0:top,
    set:value=>{if(!reader.detail.hidden)top=Math.max(0,Math.min(value,reader.list.scrollHeight-reader.list.clientHeight));}});
  const insert=reader.element.insertBefore.bind(reader.element);
  reader.element.insertBefore=(node,before)=>{insert(node,before);if(node===reader.detail&&reader.detail.hidden)top=0;};
}

for(const bottom of [true,false])test(`reopening the same phone reader restores ${bottom?'the latest reply':'an older reading position'} after hidden portal reset`,async t=>{
  const {make}=fixture(t);location.pathname='/mobile/';
  const reader=make('scroll-reopen');reader.setOpen(true);await turn();closedPortalScroll(reader);
  const expected=bottom?2111:320;reader.list.scrollTop=expected;
  reader.setOpen(false);assert.equal(reader.list.scrollTop,0);
  reader.setOpen(false); // background selection cleanup must not overwrite the saved visible position
  reader.setOpen(true);await turn();
  assert.equal(reader.list.scrollTop,expected);
  assert.equal(reader.detail.parentNode,document.body);
});

test('changing Agent identity does not carry the previous reader scroll position into its new conversation',async t=>{
  const {make}=fixture(t);location.pathname='/mobile/';
  const reader=make('scroll-old');reader.setOpen(true);await turn();closedPortalScroll(reader);
  reader.list.scrollTop=320;reader.setOpen(false);reader.update(bot('scroll-new'),true);reader.setOpen(true);await turn();
  assert.equal(reader.list.scrollTop,0);
});

test('opening a child closes the floating parent without cloning a sender or losing its draft',async t=>{
  const {make}=fixture(t),parent=make('parent'),child=make('child',{is_subagent:true,parent_agent_id:'fixture:parent',evidence:'native_hook'});
  parent.setOpen(true);await turn();parent.composer.input.value='Parent draft';parent.window.float();
  const composer=parent.composer;child.setOpen(true);await turn();
  assert.equal(parent.open,false);assert.equal(parent.detail.parentNode,parent.element);assert.equal(parent.timer,null);
  assert.equal(parent.composer,composer);assert.equal(parent.composer.input.value,'Parent draft');
  assert.equal(child.open,true);assert.equal(child.window.floating,true);assert.equal(child.composer.element.hidden,true);
  assert.equal(child.move.hidden,true);assert.equal(child.logout.hidden,true);
});

test('family picker owns the single panel and restores the same floating reader before showing children',async t=>{
  const {make}=fixture(t),reader=make('parent');
  reader.setChildren([bot('child',{is_subagent:true,depth:1})]);reader.setOpen(true);await turn();reader.window.float();
  const detail=reader.detail;reader.setTeamOpen(true);
  assert.equal(reader.open,false);assert.equal(reader.teamOpen,true);assert.equal(reader.headExpanded,true);
  assert.equal(reader.window.floating,false);assert.equal(detail.parentNode,reader.element);assert.equal(detail.hidden,true);
  assert.equal(reader.family.hidden,false);assert.equal(reader.timer,null);
});

test('floating semantic observation wakes only for changed public text and disconnects on docking',async t=>{
  const {make,observers}=fixture(t),reader=make('A');let wakes=0;
  window.addEventListener('office:dialogue-activity',()=>wakes++);
  reader.setOpen(true);await turn();reader.window.float();
  const observer=observers.at(-1);assert.equal(observer.root,reader.detail);assert.equal(observer.options.attributes,undefined);
  observer.callback([{target:reader.notice,addedNodes:[],removedNodes:[]}]);assert.equal(wakes,0);
  reader.notice.textContent='新的公開狀態';observer.callback([{target:reader.notice,addedNodes:[],removedNodes:[]}]);assert.equal(wakes,0,'notice is not a public-text selector');
  reader.composer.status.textContent='已交付';observer.callback([{target:reader.composer.status,addedNodes:[],removedNodes:[]}]);assert.equal(wakes,1);
  observer.callback([{target:reader.composer.status,addedNodes:[],removedNodes:[]}]);assert.equal(wakes,1);
  reader.window.dock();assert.equal(observer.disconnected,true);assert.equal(reader.floatingObserver,null);
});

test('retirement closes a floating portal, cancels its read and removes global gesture listeners',async t=>{
  const {make,observers}=fixture(t),reader=make('A');let resolve;
  t.mock.method(globalThis,'fetch',()=>new Promise(done=>{resolve=done;}));
  reader.setOpen(true);const ticket=reader.pendingRequest;reader.window.float();const observer=observers.at(-1);
  reader.window.start({button:0,isPrimary:true,pointerId:1,clientX:20,clientY:20,target:reader.windowHandle,preventDefault(){},stopPropagation(){}},'move',reader.windowHandle);
  assert.ok(reader.window.gesture);reader.disable();
  assert.equal(reader.window.gesture,null);assert.equal(reader.window.opened,false);assert.equal(reader.windowHandle.captures.size,0);
  assert.equal(reader.detail.parentNode,reader.element);assert.equal(reader.element.inert,true);assert.equal(reader.detail.hidden,true);
  assert.equal(reader.timer,null);assert.equal(reader.acceptsRequest(ticket),false);assert.equal(observer.disconnected,true);
  assert.equal(document.count(),0);assert.equal(window.count(),0);assert.equal(reader.composer.agent,null);
  resolve({ok:true,json:async()=>({messages:[{seq:9,event_id:'late',text:'must not render'}],cursor:9})});await turn();
  assert.equal(reader.rows.length,0);assert.equal(reader.cursor,0);
});
