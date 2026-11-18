import test from 'node:test';
import assert from 'node:assert/strict';
import {mountMobileSessionControls,mobileLoginSummary} from './public/office/mobile-session-controls.js';
const settle=async()=>{for(let i=0;i<15;i++)await Promise.resolve();};
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.listeners=new Map();this.textContent='';this.open=false;this.disabled=false;}
  append(...nodes){for(const node of nodes){node.parent=this;this.children.push(node);}}
  setAttribute(key,value){this[key]=value;}
  addEventListener(name,fn){this.listeners.set(name,fn);}
  removeEventListener(name,fn){if(this.listeners.get(name)===fn)this.listeners.delete(name);}
  fire(name){return this.listeners.get(name)?.({type:name});}
  showModal(){this.open=true;}
  close(){this.open=false;this.fire('close');}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(x=>x!==this);}
}
function setup(options={}){
  const body=new Element('body'),events=new Element('events');
  const all=()=>{const items=[];const visit=x=>{items.push(x);x.children.forEach(visit);};visit(body);return items;};
  const doc={body,createElement:tag=>new Element(tag),getElementById:id=>all().find(x=>x.id===id)};
  const initial={device:{remembered:false}},calls=[];
  const dispose=mountMobileSessionControls({doc,initial,events,mount:node=>{body.append(node);return()=>node.remove();},
    read:async()=>{calls.push('read');return initial;},remember:async enabled=>{calls.push(enabled);return{device:{remembered:enabled,renewal_expires_at:1800000000}};},logout:async()=>{calls.push('logout');},...options});
  const button=text=>all().find(x=>x.tag==='button'&&x.textContent===text);
  return {doc,events,calls,dispose,button,all};
}
test('existing phone controls do not extend access until explicit remember action',async()=>{
  const f=setup();assert.deepEqual(f.calls,[]);
  await f.button('手機登入').fire('click');assert.deepEqual(f.calls,['read']);
  await f.button('在此手機保留登入').fire('click');assert.deepEqual(f.calls,['read',true]);
  assert.ok(f.button('關閉自動續期'));assert.ok(f.all().some(x=>x.textContent.includes('最長 30 日')));
  f.button('關閉').fire('click');assert.equal(f.all().find(x=>x.tag==='dialog').open,false);f.dispose();
});
test('turning off remembered login is explicit and updates status without logout',async()=>{
  const f=setup({initial:{device:{remembered:true,renewal_expires_at:1800000000}}});
  await f.button('關閉自動續期').fire('click');assert.deepEqual(f.calls,[false]);assert.ok(f.button('在此手機保留登入'));assert.equal(f.calls.includes('logout'),false);f.dispose();
});
test('closed dialog ignores delayed read and does not overwrite a newly opened view',async()=>{
  let resolve,reads=0;const wait=new Promise(r=>{resolve=r;});
  const f=setup({read:()=>++reads===1?wait:Promise.resolve({device:{remembered:true,renewal_expires_at:1800000000}})});
  const pending=f.button('手機登入').fire('click');f.button('關閉').fire('click');await f.button('手機登入').fire('click');
  resolve({device:{remembered:false}});await pending;assert.ok(f.button('關閉自動續期'));f.dispose();
});
test('lock event removes the dialog and launcher immediately',async()=>{
  const f=setup();await f.button('手機登入').fire('click');f.events.fire('office:mobile-locked');assert.equal(f.doc.body.children.length,0);assert.equal(f.events.listeners.size,0);
});
test('summary makes short login versus bounded remembered grant explicit',()=>{
  assert.match(mobileLoginSummary({remembered:false}),/短期登入/);assert.match(mobileLoginSummary({remembered:true,renewal_expires_at:1800000000}),/連續 7 日/);
});
