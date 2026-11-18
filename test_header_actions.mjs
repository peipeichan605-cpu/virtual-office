import test from 'node:test';
import assert from 'node:assert/strict';
import {mountHeaderAction,refreshHeaderActions} from './public/office/header-actions.js';

// Minimal DOM relationships make reparenting observable without a browser or
// real Office data. Browser layout/hit testing is verified separately.
class Element {
  constructor(tag='div') {
    this.tagName=tag;this.children=[];this.parentElement=null;this.attrs={};this.events=new Map();
    const classes=new Set();this.classList={contains:key=>classes.has(key),toggle:(key,on)=>on?classes.add(key):classes.delete(key)};
  }
  append(...nodes){for(const node of nodes){node.remove();node.parentElement=this;this.children.push(node);}}
  remove(){if(this.parentElement)this.parentElement.children=this.parentElement.children.filter(node=>node!==this);this.parentElement=null;}
  setAttribute(key,value){this.attrs[key]=String(value);}
  focus(options){this.ownerDocument.activeElement=this;this.focusOptions=options;}
  addEventListener(type,fn){this.events.set(type,fn);}
  click(){this.events.get('click')?.();}
}
function fixture({simple=false,ready=false}={}){
  const doc={body:new Element('body'),documentElement:new Element('html'),createElement:tag=>new Element(tag),slot:null,
    querySelector:selector=>selector==='#topbar .bhq-right'?doc.slot:null};
  doc.documentElement.classList.toggle('office-simple-mode',simple);
  const makeHeader=()=>{doc.slot=new Element();doc.body.append(doc.slot);return doc.slot;};
  if(ready)makeHeader();
  const tray=()=>doc.body.children.find(node=>node.id==='office-header-actions')||null;
  return {doc,makeHeader,tray};
}

test('an early invitation joins the ready header with settings and existing phone/sound controls',()=>{
  const f=fixture(),invite=new Element('button'),settings=new Element('button');let opened=0;
  invite.addEventListener('click',()=>opened++);
  mountHeaderAction(invite,{doc:f.doc});assert.equal(invite.parentElement,f.tray());
  const slot=f.makeHeader(),phone=new Element('button'),sound=new Element('button');slot.append(phone,sound);
  mountHeaderAction(settings,{doc:f.doc,simple:true});
  assert.deepEqual(slot.children,[phone,sound,invite,settings]);assert.equal(f.tray(),null);
  invite.click();assert.equal(opened,1);
});

test('saved simple mode shares one tray for settings and the mode switch without exposing invitation',()=>{
  const f=fixture({simple:true,ready:true}),settings=new Element('button'),mode=new Element('button'),invite=new Element('button');
  invite.hidden=true;
  mountHeaderAction(settings,{doc:f.doc,simple:true});
  mountHeaderAction(mode,{doc:f.doc,simple:true,full:'body'});
  mountHeaderAction(invite,{doc:f.doc});
  assert.deepEqual(f.tray().children,[settings,mode]);
  assert.equal(invite.parentElement,f.doc.slot);assert.equal(invite.hidden,true);
});

test('repeated simple/full switches preserve button identity, handlers and full-view placement',()=>{
  const f=fixture({ready:true}),settings=new Element('button'),mode=new Element('button');let opened=0;
  settings.addEventListener('click',()=>opened++);
  mountHeaderAction(settings,{doc:f.doc,simple:true});mountHeaderAction(mode,{doc:f.doc,simple:true,full:'body'});
  for(let i=0;i<3;i++){
    f.doc.documentElement.classList.toggle('office-simple-mode',true);refreshHeaderActions(f.doc);
    assert.deepEqual(f.tray().children,[settings,mode]);settings.click();
    f.doc.documentElement.classList.toggle('office-simple-mode',false);refreshHeaderActions(f.doc);
    assert.equal(settings.parentElement,f.doc.slot);assert.equal(mode.parentElement,f.doc.body);assert.equal(f.tray(),null);
  }
  assert.equal(opened,3);
});

test('later registration recovers a rebuilt header without duplication or background observers',()=>{
  const f=fixture({ready:true}),invite=new Element('button'),settings=new Element('button');
  mountHeaderAction(invite,{doc:f.doc});const oldSlot=f.doc.slot;oldSlot.remove();
  const newSlot=f.makeHeader();mountHeaderAction(settings,{doc:f.doc,simple:true});
  assert.deepEqual(newSlot.children,[invite,settings]);assert.deepEqual(oldSlot.children,[]);
  refreshHeaderActions(f.doc);assert.deepEqual(newSlot.children,[invite,settings]);
});

test('keyboard focus remains on the mode switch when moving into and out of simple mode',()=>{
  const f=fixture({ready:true}),mode=new Element('button');mode.ownerDocument=f.doc;
  mountHeaderAction(mode,{doc:f.doc,simple:true,full:'body'});mode.focus();
  // Native DOM removal blurs an active descendant; model that behavior here.
  const remove=mode.remove.bind(mode);mode.remove=()=>{if(f.doc.activeElement===mode)f.doc.activeElement=f.doc.body;remove();};
  for(const simple of [true,false]){
    f.doc.documentElement.classList.toggle('office-simple-mode',simple);refreshHeaderActions(f.doc);
    assert.equal(f.doc.activeElement,mode);assert.deepEqual(mode.focusOptions,{preventScroll:true});
  }
});

test('disposing settings releases its fallback and leaves independently mounted actions intact',()=>{
  const f=fixture(),settings=new Element('button'),invite=new Element('button');
  const release=mountHeaderAction(settings,{doc:f.doc,simple:true});
  const releaseInvite=mountHeaderAction(invite,{doc:f.doc});
  release();assert.deepEqual(f.tray().children,[invite]);assert.equal(settings.parentElement,null);
  releaseInvite();assert.equal(f.tray(),null);refreshHeaderActions(f.doc);assert.equal(f.tray(),null);
});

test('separate documents never share launchers or mode state',()=>{
  const a=fixture({ready:true}),b=fixture({ready:true,simple:true}),one=new Element('button'),two=new Element('button');
  mountHeaderAction(one,{doc:a.doc,simple:true});mountHeaderAction(two,{doc:b.doc,simple:true});
  assert.equal(one.parentElement,a.doc.slot);assert.equal(two.parentElement,b.tray());
  assert.equal(a.tray(),null);refreshHeaderActions(a.doc);assert.equal(two.parentElement,b.tray());
});
