import test from 'node:test';
import assert from 'node:assert/strict';
import {DEFAULT_GRAPHICS_SETTINGS,graphicsStorageKey,sanitizeGraphicsSettings,createGraphicsSettingsStore} from './public/office/graphics-settings.js';

const memory = () => {const values=new Map();return {values,getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,value)};};
test('defaults preserve existing quality, frame cap and automatic idle mode',()=>{
  assert.deepEqual(sanitizeGraphicsSettings(null),{antialias:4,fps:30,scale:1,idle:true});
  assert.deepEqual(sanitizeGraphicsSettings({antialias:2,fps:90,scale:.75,idle:false}),{antialias:2,fps:90,scale:.75,idle:false});
  assert.deepEqual(sanitizeGraphicsSettings({antialias:8,fps:120,scale:0,idle:'false'}),DEFAULT_GRAPHICS_SETTINGS);
  assert.deepEqual(sanitizeGraphicsSettings({antialias:true,fps:Infinity,scale:[],idle:0}),DEFAULT_GRAPHICS_SETTINGS);
});
test('installation path scopes storage without private channel, query or Agent identity',()=>{
  const base=graphicsStorageKey('https://example.test/office/#private-channel');
  assert.equal(base,graphicsStorageKey('https://example.test/office/index.html?quality=low#another-private-channel'));
  assert.notEqual(base,graphicsStorageKey('https://example.test/another-office/'));
  assert.ok(!base.includes('private-channel'));assert.ok(!base.includes('quality'));
});
test('settings persist, reload, validate invalid values and preserve valid prior selections',()=>{
  const storage=memory(),first=createGraphicsSettingsStore({storage,href:'https://example.test/office/'});
  first.update({antialias:1,fps:60,scale:1.25,idle:false});
  const second=createGraphicsSettingsStore({storage,href:'https://example.test/office/'});
  assert.deepEqual(second.getSettings(),{antialias:1,fps:60,scale:1.25,idle:false});
  second.update({fps:17,scale:2});assert.equal(second.getSettings().fps,60);assert.equal(second.getSettings().scale,1.25);
  const returned=second.getSettings();returned.fps=90;assert.equal(second.getSettings().fps,60);
  storage.setItem(first.key,'{corrupt');assert.deepEqual(createGraphicsSettingsStore({storage,href:'https://example.test/office/'}).getSettings(),DEFAULT_GRAPHICS_SETTINGS);
  storage.setItem(first.key,JSON.stringify({version:99,settings:{fps:90}}));assert.equal(createGraphicsSettingsStore({storage,href:'https://example.test/office/'}).getSettings().fps,30);
});
test('only meaningful edits notify; unsubscribe and status copies isolate callers',()=>{
  const store=createGraphicsSettingsStore({storage:memory()});let calls=0;
  const off=store.subscribe(()=>calls++);store.update({fps:30});assert.equal(calls,0);store.update({fps:60});assert.equal(calls,1);off();store.update({fps:90});assert.equal(calls,1);
  store.publishStatus({availableAntialias:[1,4],effectiveAntialias:4});const copy=store.getStatus();copy.availableAntialias.push(2);assert.deepEqual(store.getStatus().availableAntialias,[1,4]);
});
test('blocked storage still applies controls for the current visit and explains persistence',()=>{
  const store=createGraphicsSettingsStore({storage:{getItem(){throw new Error('blocked');},setItem(){throw new Error('blocked');}}});
  store.update({fps:60});assert.equal(store.getSettings().fps,60);assert.match(store.getStatus().persistenceMessage,/未能儲存/);
});
test('other tabs synchronize the same installation, ignore other keys and do not write back',()=>{
  let handler,removed=false,writes=0;const backing=memory();const storage={getItem:backing.getItem,setItem(){writes++;}};
  const target={addEventListener(type,fn){assert.equal(type,'storage');handler=fn;},removeEventListener(type,fn){removed=type==='storage'&&fn===handler;}};
  const store=createGraphicsSettingsStore({storage,eventTarget:target,href:'https://example.test/office/'});
  handler({key:'other',newValue:JSON.stringify({version:1,settings:{fps:90}})});assert.equal(store.getSettings().fps,30);
  handler({key:store.key,newValue:JSON.stringify({version:1,settings:{fps:60,antialias:2}}),storageArea:storage});assert.equal(store.getSettings().fps,60);assert.equal(writes,0);
  handler({key:store.key,newValue:null});assert.deepEqual(store.getSettings(),DEFAULT_GRAPHICS_SETTINGS);store.dispose();assert.ok(removed);
});
test('a failing listener cannot prevent the renderer and other controls from applying',()=>{
  const store=createGraphicsSettingsStore({storage:memory()});let applied=0;store.subscribe(()=>{throw new Error('removed UI');});store.subscribe(()=>applied++);store.update({fps:60});assert.equal(applied,1);
});

test('sequential individual controls survive a same-path query navigation',()=>{
 const storage=memory(),store=createGraphicsSettingsStore({storage,href:'https://example.test/office/#private'});
 store.update({antialias:1});store.update({fps:60});store.update({scale:.75});store.update({idle:false});
 const second=createGraphicsSettingsStore({storage,href:'https://example.test/office/?profile=1#private'});
 assert.deepEqual(second.getSettings(),{antialias:1,fps:60,scale:.75,idle:false});
});
test('unavailable initial storage is reported before any edit',()=>{
 const store=createGraphicsSettingsStore({storage:null});assert.equal(store.getStatus().storageAvailable,false);assert.match(store.getStatus().persistenceMessage,/儲存/);
});
