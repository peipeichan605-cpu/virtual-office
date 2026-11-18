import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source=(await readFile(new URL('./public/office/mobile-owner.js',import.meta.url),'utf8')).replace("import {officeGet, officePost} from './office-api.js';","const officeGet=()=>{throw Error('Unexpected API call')};const officePost=officeGet;");
const {MobileOwnerController,pairingLink,MobilePairingQR,drawPairingQR,initMobileOwner,mobileEntrySummary}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const TOKEN='test_pairing_token_long_enough_123456';
const ready={enabled:true,public_origin:'https://office.example.ts.net',identity:'owner@example.com',agents:[{id:'agent:a',name:'Agent A'},{id:'agent:b',name:'Agent B'}],devices:[{device_id:'phone-a',label:'Phone',revoked:false}]};
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {resolve,promise};};

// Exercise the real owner renderer without browser/network dependencies.
const descendants=element=>element.children.flatMap(child=>[child,...descendants(child)]);
function installOwnerDOM(){
  class Element{
    constructor(tag){this.tagName=tag;this.children=[];this.className='';this.value='';this.ownText='';this.listeners=new Map();}
    set textContent(value){this.ownText=String(value);this.children=[];}
    get textContent(){return this.ownText+this.children.map(child=>child.textContent).join('');}
    append(...children){for(const child of children){child.parent=this;this.children.push(child);}}
    replaceChildren(...children){this.ownText='';this.children=[];this.append(...children);}
    setAttribute(){}
    addEventListener(type,listener){const listeners=this.listeners.get(type)||[];listeners.push(listener);this.listeners.set(type,listeners);}
    async fire(type,event={}){for(const listener of this.listeners.get(type)||[])await listener(event);}
    querySelector(selector){return descendants(this).find(child=>selector.startsWith('.')&&child.className.split(' ').includes(selector.slice(1)))||null;}
    remove(){if(this.parent)this.parent.children=this.parent.children.filter(child=>child!==this);}
  }
  const previousDocument=globalThis.document,previousLocation=globalThis.location;
  const body=new Element('body'),slot=new Element('div');body.append(slot);
  globalThis.document={body,createElement:tag=>new Element(tag),querySelector:()=>null};
  globalThis.location={search:''};
  return {body,slot,restore(){
    if(previousDocument===undefined)delete globalThis.document;else globalThis.document=previousDocument;
    if(previousLocation===undefined)delete globalThis.location;else globalThis.location=previousLocation;
  }};
}

test('construction performs no reads or writes; opening only reads',async()=>{const calls=[];const c=new MobileOwnerController({read:async()=>{calls.push('read');return ready;},create:()=>calls.push('create')});assert.deepEqual(calls,[]);await c.open();assert.deepEqual(calls,['read']);assert.equal(c.pairing,null);});
test('pairing requires an explicitly selected valid view scope and send subset',async()=>{let writes=0;const c=new MobileOwnerController({read:async()=>ready,create:async()=>{writes++;return {pairing_token:TOKEN,expires_at:2000000000};}});await c.open();for(const scopes of [{view_agents:[],send_agents:[]},{view_agents:['agent:unknown'],send_agents:[]},{view_agents:['agent:a'],send_agents:['agent:b']}])assert.equal(await c.pair({identity:'owner@example.com',label:'My phone',...scopes}),false);assert.equal(writes,0);assert.equal(await c.pair({identity:' owner@example.com ',label:' My phone ',view_agents:['agent:a'],send_agents:['agent:a']}),true);assert.equal(writes,1);assert.equal(c.pairing.url,'https://office.example.ts.net/mobile/pair#pair='+TOKEN);});
test('legacy pairing payload remains selected without silently adding roster permissions',async()=>{
  const calls=[],c=new MobileOwnerController({read:async()=>ready,create:async data=>{calls.push(data);return {pairing_token:TOKEN,expires_at:2000000000};}});
  await c.open();await c.pair({identity:' owner@example.com ',label:' Phone ',view_agents:['agent:a','agent:a'],send_agents:[]});
  assert.deepEqual(calls,[{identity:'owner@example.com',label:'Phone',view_agents:['agent:a'],send_agents:[]}]);c.close();
});
test('following the enrolled roster does not expand send permission and permits an empty initial office',async()=>{
  const calls=[],c=new MobileOwnerController({read:async()=>ready,create:async data=>{calls.push(data);return {pairing_token:TOKEN,expires_at:2000000000};}});
  await c.open();const identity={identity:'owner@example.com',label:'Phone'};
  assert.equal(await c.pair({...identity,view_mode:'enrolled',send_mode:'selected',view_agents:[],send_agents:['agent:a']}),true);
  assert.deepEqual(calls[0],{...identity,view_mode:'enrolled',send_mode:'selected',view_agents:[],send_agents:['agent:a']});
  c.snapshot={...ready,agents:[]};
  assert.equal(await c.pair({...identity,view_mode:'enrolled',send_mode:'selected',view_agents:[],send_agents:[]}),true);
  assert.deepEqual(calls[1].send_agents,[]);assert.equal(calls[1].send_mode,'selected');c.close();
});
test('all-agent send requires explicit enrolled view and canonicalizes dynamic arrays',async()=>{
  const calls=[],c=new MobileOwnerController({read:async()=>ready,create:async data=>{calls.push(data);return {pairing_token:TOKEN,expires_at:2000000000};}});
  await c.open();const identity={identity:'owner@example.com',label:'Phone'};
  for(const scopes of [
    {view_mode:'selected',send_mode:'enrolled',view_agents:['agent:a'],send_agents:[]},
    {view_mode:'all',send_mode:'selected',view_agents:['agent:a'],send_agents:[]},
    {view_mode:'enrolled',send_mode:'selected',view_agents:[],send_agents:['agent:unknown']},
    {view_mode:'enrolled',send_mode:'selected',view_agents:[],send_agents:'agent:a'},
  ])assert.equal(await c.pair({...identity,...scopes}),false);
  assert.deepEqual(calls,[]);
  assert.equal(await c.pair({...identity,view_mode:'enrolled',send_mode:'enrolled',view_agents:['agent:a'],send_agents:['agent:a']}),true);
  assert.deepEqual(calls[0],{...identity,view_mode:'enrolled',send_mode:'enrolled',view_agents:[],send_agents:[]});c.close();
});
test('an explicit scope update retains the device session and displays the confirmed revision',async()=>{
  const device={device_id:'phone-a',state:'ready',revision:4,view_mode:'selected',send_mode:'selected',view_agents:['agent:a'],send_agents:['agent:a']};
  const updated={...device,revision:5,view_mode:'enrolled',view_agents:['agent:a','agent:b']};
  const calls=[],c=new MobileOwnerController({read:async()=>({...ready,devices:[device]}),updateScopes:async data=>{calls.push(data);return {changed:true,device:updated};}});
  await c.open();assert.deepEqual(calls,[]);
  assert.equal(await c.updateDeviceScopes('phone-a',{view_mode:'enrolled',send_mode:'selected',view_agents:[],send_agents:['agent:a']}),true);
  assert.deepEqual(calls,[{device_id:'phone-a',view_mode:'enrolled',send_mode:'selected',view_agents:[],send_agents:['agent:a']}]);
  assert.deepEqual(c.snapshot.devices,[updated]);assert.equal(c.pairing,null);assert.match(c.notice,/毋須重新配對/);assert.match(c.notice,/已開始的工作不受影響/);c.close();
});
test('scope updates reject revoked, expired, missing, closed and disabled devices before posting',async()=>{
  let writes=0;const scope={view_mode:'enrolled',send_mode:'selected',view_agents:[],send_agents:[]};
  for(const fields of [{state:'expired'},{state:'revoked'},{state:'ready',revoked_at:0},{state:'ready',revoked:true},{state:'unknown'}]){
    const c=new MobileOwnerController({read:async()=>({...ready,devices:[{device_id:'phone-a',...fields}]}),updateScopes:async()=>{writes++;}});
    await c.open();assert.equal(await c.updateDeviceScopes('phone-a',scope),false);c.close();
  }
  const c=new MobileOwnerController({read:async()=>({...ready,devices:[{device_id:'phone-a',state:'ready'}]}),updateScopes:async()=>{writes++;}});
  await c.open();assert.equal(await c.updateDeviceScopes('unknown',scope),false);c.snapshot.enabled=false;assert.equal(await c.updateDeviceScopes('phone-a',scope),false);c.close();assert.equal(await c.updateDeviceScopes('phone-a',scope),false);assert.equal(writes,0);
});
test('scope writes are not retried after closing or when a response names another device',async()=>{
  const pending=deferred(),calls=[],device={device_id:'phone-a',state:'ready',revision:1};
  const c=new MobileOwnerController({read:async()=>({...ready,devices:[device]}),updateScopes:data=>{calls.push(data);return pending.promise;}});
  await c.open();const scope={view_mode:'enrolled',send_mode:'selected',view_agents:[],send_agents:[]};
  const updating=c.updateDeviceScopes('phone-a',scope);assert.equal(await c.updateDeviceScopes('phone-a',scope),false);c.close();
  pending.resolve({changed:true,device:{...device,revision:2}});assert.equal(await updating,false);assert.equal(c.snapshot.devices[0].revision,1);assert.equal(calls.length,1);
  c.updateScopes=async()=>({device:{device_id:'other-device',state:'ready'}});await c.open();assert.equal(await c.updateDeviceScopes('phone-a',scope),false);assert.equal(c.snapshot.devices[0].device_id,'phone-a');c.close();
});
test('owner scope controls default to selected, keep send separate and only save explicitly',async()=>{
  const {body,slot,restore}=installOwnerDOM(),calls=[];
  const device={device_id:'phone-a',label:'Phone',state:'ready',revision:1,view_mode:'selected',send_mode:'selected',view_agents:['agent:a'],send_agents:['agent:a']};
  const c=new MobileOwnerController({read:async()=>({...ready,devices:[device]}),updateScopes:async data=>{calls.push(data);return {changed:true,device:{...device,...data,revision:2,view_agents:['agent:a','agent:b']}};}});
  let ui;
  try{
    ui=initMobileOwner({slot,controller:c});await c.open();
    const form=descendants(body).find(el=>el.tagName==='form'),topCheckboxes=form.children.filter(el=>el.tagName==='label').flatMap(el=>el.children.filter(child=>child.type==='checkbox'));
    assert.equal(topCheckboxes.length,2);assert.ok(topCheckboxes.every(el=>!el.checked));
    topCheckboxes[0].checked=true;await topCheckboxes[0].fire('change');assert.equal(topCheckboxes[1].checked,false);
    topCheckboxes[1].checked=true;await topCheckboxes[1].fire('change');assert.equal(topCheckboxes[0].checked,true);
    topCheckboxes[0].checked=false;await topCheckboxes[0].fire('change');assert.equal(topCheckboxes[1].checked,false);
    let row=body.querySelector('.mobile-owner-devices').children[0];let controls=descendants(row).filter(el=>el.tagName==='select');
    assert.deepEqual(controls.map(el=>el.value),['selected','selected']);
    controls[0].value='enrolled';await controls[0].fire('change');assert.equal(controls[1].value,'selected');assert.deepEqual(calls,[]);
    // A status render preserves the unsaved explicit selection without writing it.
    c.emit();row=body.querySelector('.mobile-owner-devices').children[0];controls=descendants(row).filter(el=>el.tagName==='select');assert.equal(controls[0].value,'enrolled');
    await descendants(row).find(el=>el.textContent==='儲存權限').fire('click');
    assert.deepEqual(calls,[{device_id:'phone-a',view_mode:'enrolled',send_mode:'selected',view_agents:[],send_agents:['agent:a']}]);
    row=body.querySelector('.mobile-owner-devices').children[0];controls=descendants(row).filter(el=>el.tagName==='select');controls[1].value='none';await controls[1].fire('change');assert.equal(calls.length,1);
    await descendants(row).find(el=>el.textContent==='儲存權限').fire('click');assert.deepEqual(calls[1].send_agents,[]);assert.equal(calls[1].send_mode,'selected');
  }finally{ui?.dispose();restore();}
});
test('pairing response uses only the configured origin and token, never arbitrary response URL',async()=>{const c=new MobileOwnerController({read:async()=>ready,create:async()=>({pair_url:'https://evil.example/capture',pairing_token:TOKEN,expires_at:2000000000})});await c.open();await c.pair({identity:'owner@example.com',label:'Phone',view_agents:['agent:a'],send_agents:[]});assert.match(c.pairing.url,/^https:\/\/office\.example\.ts\.net\/mobile\/pair#/);});
test('unsafe origins or malformed tokens cannot create a pairing link',()=>{for(const origin of ['http://office.example','javascript:alert(1)','https://user:pass@office.example','https://office.example/path','https://office.example?token=x','https://office.example#token'])assert.equal(pairingLink(origin,TOKEN),null);for(const token of ['',null,'tiny','<svg onload=alert(1)>'])assert.equal(pairingLink(ready.public_origin,token),null);});
test('a pairing response cannot redirect to a different HTTPS origin',async()=>{const c=new MobileOwnerController({read:async()=>ready,create:async()=>({public_origin:'https://evil.example',pairing_token:TOKEN,expires_at:2000000000})});await c.open();assert.equal(await c.pair({identity:'owner@example.com',label:'Phone',view_agents:['agent:a'],send_agents:[]}),false);assert.equal(c.pairing,null);});
test('disabled gateway does not create pairing',async()=>{let calls=0;const c=new MobileOwnerController({read:async()=>({...ready,enabled:false}),create:()=>calls++});await c.open();assert.equal(await c.pair({identity:'a',label:'b',view_agents:['agent:a'],send_agents:[]}),false);assert.equal(calls,0);});
test('closing dialog clears pairing and fences late responses without retrying writes',async()=>{const response=deferred();let writes=0;const c=new MobileOwnerController({read:async()=>ready,create:()=>{writes++;return response.promise;}});await c.open();const pending=c.pair({identity:'owner@example.com',label:'Phone',view_agents:['agent:a'],send_agents:[]});c.close();response.resolve({pairing_token:TOKEN,expires_at:2000000000});assert.equal(await pending,false);assert.equal(c.pairing,null);assert.equal(writes,1);});
test('revocation is explicit and reports only future/unreceived blocking',async()=>{const calls=[];const c=new MobileOwnerController({read:async()=>ready,revoke:async data=>{calls.push(data);return {};}});await c.open();assert.equal(await c.revokeDevice('unknown'),false);assert.equal(await c.revokeDevice('phone-a'),true);assert.deepEqual(calls,[{device_id:'phone-a'}]);assert.equal(c.snapshot.devices[0].revoked,true);assert.match(c.notice,/已開始的工作/);assert.equal(await c.revokeDevice('phone-a'),false);});
test('canonical revoked state or timestamp fences repeat revocation without a legacy boolean',async()=>{
  let writes=0;
  for(const fields of [{state:'revoked',revoked_at:null},{revoked_at:1794800000},{revoked_at:0},{revoked:true}]){
    const c=new MobileOwnerController({read:async()=>({...ready,devices:[{device_id:'phone-a',label:'Phone',...fields}]}),revoke:async()=>{writes++;}});
    await c.open();assert.equal(await c.revokeDevice('phone-a'),false);c.close();
  }
  assert.equal(writes,0);
});
test('refreshing the real revoked API shape keeps its rendered status and removes only its revoke control',async()=>{
  const {body,slot,restore}=installOwnerDOM();
  const calls=[];
  let response={...ready,devices:[
    {device_id:'phone-a',label:'Same phone label',state:'ready',revoked_at:null,expires_at:2000000000},
    {device_id:'phone-b',label:'Same phone label',state:'ready',revoked_at:null,expires_at:2000000000},
  ]};
  const c=new MobileOwnerController({read:async()=>structuredClone(response),revoke:async data=>{
    calls.push(data);response={...response,devices:response.devices.map(device=>device.device_id===data.device_id?{...device,state:'revoked',revoked_at:1794800000}:device)};
    return {device:response.devices.find(device=>device.device_id===data.device_id)};
  }});
  let ui;
  try{
    ui=initMobileOwner({slot,controller:c});await c.open();
    const rows=()=>body.querySelector('.mobile-owner-devices').children;
    const buttons=row=>descendants(row).filter(child=>child.tagName==='button'&&child.textContent==='撤銷');
    assert.equal(buttons(rows()[0]).length,1);assert.equal(buttons(rows()[1]).length,1);
    assert.equal(await c.revokeDevice('phone-a'),true);
    await c.refresh();
    assert.equal(c.snapshot.devices[0].revoked,undefined,'Server payload deliberately has no legacy revoked boolean');
    assert.match(rows()[0].textContent,/已撤銷/);assert.equal(buttons(rows()[0]).length,0);
    assert.doesNotMatch(rows()[1].textContent,/已撤銷/);assert.equal(buttons(rows()[1]).length,1);assert.equal(buttons(rows()[1])[0].disabled,false);
    assert.equal(await c.revokeDevice('phone-a'),false);assert.deepEqual(calls,[{device_id:'phone-a'}]);
    c.close();await c.open();
    assert.match(rows()[0].textContent,/已撤銷/);assert.equal(buttons(rows()[0]).length,0);
  }finally{
    ui?.dispose();restore();
  }
});
test('idle-expired API state overrides a future absolute deadline after refresh and reopen',async()=>{
  const {body,slot,restore}=installOwnerDOM(),calls=[];
  const device={device_id:'phone-expired',label:'Phone',state:'ready',revoked_at:null,revision:1,
    expires_at:2000000000,idle_expires_at:1794803600,view_agents:['agent:a'],send_agents:['agent:a']};
  let response={...ready,devices:[device]};
  const c=new MobileOwnerController({read:async()=>structuredClone(response),revoke:async data=>{calls.push(data);return {};}});
  let ui;
  try{
    ui=initMobileOwner({slot,controller:c});await c.open();
    const row=()=>body.querySelector('.mobile-owner-devices').children[0];
    const buttons=()=>descendants(row()).filter(child=>child.tagName==='button');
    assert.match(row().textContent,/登入有效期至/);
    response={...response,devices:[{...device,state:'expired'}]};await c.refresh();
    const originalDeadline=new Date(device.expires_at*1000).toLocaleString('zh-HK');
    assert.match(row().textContent,/已過期，請重新配對/);
    assert.doesNotMatch(row().textContent,/登入有效期至/);
    assert.ok(row().textContent.includes(originalDeadline),'Retain the absolute deadline without implying the login remains active');
    assert.equal(buttons().length,1);assert.equal(buttons()[0].disabled,false);
    c.close();await c.open();
    assert.match(row().textContent,/已過期，請重新配對/);assert.deepEqual(calls,[]);
    assert.equal(await c.revokeDevice('phone-expired'),true);
    assert.deepEqual(calls,[{device_id:'phone-expired'}]);
    assert.match(row().textContent,/已撤銷/);assert.doesNotMatch(row().textContent,/請重新配對/);assert.equal(buttons().length,0);
  }finally{ui?.dispose();restore();}
});
test('double pairing submission cannot issue a second token',async()=>{const response=deferred();let writes=0;const c=new MobileOwnerController({read:async()=>ready,create:()=>{writes++;return response.promise;}});await c.open();const data={identity:'owner@example.com',label:'Phone',view_agents:['agent:a'],send_agents:[]};const pending=c.pair(data);assert.equal(await c.pair(data),false);response.resolve({pairing_token:TOKEN,expires_at:2000000000});assert.equal(await pending,true);assert.equal(writes,1);});

test('QR encoder loads only for an actual pairing, receives no URL during loading, and late work is fenced',async()=>{
  const pending=deferred(),loads=[],draws=[];let clears=0;
  const qr=new MobilePairingQR({load:(...args)=>{loads.push(args);return pending.promise;},draw:(encoder,url)=>draws.push(url),clear:()=>clears++});
  await qr.show(null);assert.equal(loads.length,0);
  const showing=qr.show({url:pairingLink(ready.public_origin,TOKEN)});assert.deepEqual(loads,[[]]);
  qr.reset();pending.resolve({default:()=>{}});await showing;assert.deepEqual(draws,[]);assert.equal(qr.url,null);assert.ok(clears>0);
});
test('replacing a pairing cannot display an older asynchronously encoded secret',async()=>{
  const first=deferred(),second=deferred(),draws=[];let count=0;
  const qr=new MobilePairingQR({load:()=>++count===1?first.promise:second.promise,draw:(_,url)=>draws.push(url)});
  const old=qr.show({url:'first-private-pairing'}),latest=qr.show({url:'second-private-pairing'});
  second.resolve({default:()=>{}});await latest;first.resolve({default:()=>{}});await old;
  assert.deepEqual(draws,['second-private-pairing']);
});
test('local encoder failure preserves pairing for text fallback without retrying or logging the URL',async()=>{
  let failures=0,loads=0;const qr=new MobilePairingQR({load:async()=>{loads++;throw Error('module unavailable');},draw:()=>assert.fail('must not draw'),unavailable:()=>failures++});
  const pairing={url:pairingLink(ready.public_origin,TOKEN)};await qr.show(pairing);await qr.show(pairing);
  assert.equal(failures,1);assert.equal(loads,1);assert.equal(pairing.url,pairingLink(ready.public_origin,TOKEN));
});
test('expired pairing clears its secret and QR, and closing cancels the expiry timer',async()=>{
  let now=1000000,next=0,clears=0;const timers=new Map();
  const qr=new MobilePairingQR({load:async()=>({default:()=>{}}),draw:()=>{},clear:()=>clears++});
  const c=new MobileOwnerController({read:async()=>ready,create:async()=>({pairing_token:TOKEN,expires_at:(now+5000)/1000}),now:()=>now,schedule:fn=>{const id=++next;timers.set(id,fn);return id;},cancel:id=>timers.delete(id),changed:state=>void qr.show(state.pairing)});
  await c.open();const args={identity:'owner@example.com',label:'Phone',view_agents:['agent:a'],send_agents:[]};await c.pair(args);await Promise.resolve();assert.ok(c.pairing);assert.equal(timers.size,1);
  now+=5001;const callback=[...timers.values()][0];timers.clear();callback();assert.equal(c.pairing,null);assert.equal(qr.url,null);assert.match(c.notice,/已過期/);assert.ok(clears>0);
  await c.pair(args);assert.equal(timers.size,1);c.close();assert.equal(timers.size,0);assert.equal(c.pairing,null);assert.equal(qr.url,null);
});
test('default timers keep the browser Window receiver when pairing and closing',async()=>{
  const originalSet=globalThis.setTimeout,originalClear=globalThis.clearTimeout,calls=[];
  // Browser WebIDL timer functions reject a controller object as their this.
  // Node accepts that receiver, so simulate the browser contract explicitly.
  globalThis.setTimeout=function(callback,delay){assert.equal(this,globalThis);assert.equal(typeof callback,'function');calls.push(['set',delay]);return 42;};
  globalThis.clearTimeout=function(id){assert.equal(this,globalThis);calls.push(['clear',id]);};
  try{
    const c=new MobileOwnerController({read:async()=>ready,create:async()=>({pairing_token:TOKEN,expires_at:Date.now()/1000+300})});await c.open();
    assert.equal(await c.pair({identity:'owner@example.com',label:'Phone',view_agents:['agent:a'],send_agents:[]}),true);
    assert.ok(c.pairing);assert.match(c.notice,/只供這部手機/);assert.equal(calls[0][0],'set');assert.ok(calls[0][1]>0&&calls[0][1]<=300000);
    c.close();assert.equal(c.pairing,null);assert.deepEqual(calls.at(-1),['clear',42]);
  }finally{globalThis.setTimeout=originalSet;globalThis.clearTimeout=originalClear;}
});
test('expired or missing expiry responses cannot leave a scannable or copyable secret',async()=>{
  for(const expires_at of [undefined,null,'invalid',0,999]){
    const c=new MobileOwnerController({read:async()=>ready,now:()=>1000000,create:async()=>({pairing_token:TOKEN,expires_at})});await c.open();
    assert.equal(await c.pair({identity:'owner@example.com',label:'Phone',view_agents:['agent:a'],send_agents:[]}),false);assert.equal(c.pairing,null);
  }
});
test('vendored encoder draws the actual pairing locally with a four-module quiet zone and readable size',async()=>{
  const {default:encode}=await import('./public/office/vendor/qrcode-generator/qrcode.mjs');
  const fills=[];const ctx={fillStyle:'',fillRect(...rect){fills.push({color:this.fillStyle,rect});}};const canvas={getContext:type=>type==='2d'?ctx:null};
  const url=pairingLink(ready.public_origin,TOKEN);drawPairingQR(canvas,encode,url);
  assert.equal(canvas.width,canvas.height);assert.ok(canvas.width>=288);assert.deepEqual(fills[0],{color:'#fff',rect:[0,0,canvas.width,canvas.height]});
  const black=fills.slice(1);assert.ok(black.length>100);const cell=black[0].rect[2];assert.ok(black.every(x=>x.color==='#000'&&x.rect[0]>=4*cell&&x.rect[1]>=4*cell&&x.rect[0]+cell<=canvas.width-4*cell&&x.rect[1]+cell<=canvas.height-4*cell));
});


test('setup metadata cannot label an untested phone connected or allow a known unavailable entry',async()=>{
  assert.match(mobileEntrySummary({...ready,setup:{configuration_ready:true,pairing_available:true,transport:{kind:'unverified'}}}),/尚未確認手機連線/);
  assert.match(mobileEntrySummary({...ready,enabled:true,configured:false,setup:{configuration_ready:false,pairing_available:false}}),/尚未設定/);
  assert.doesNotMatch(mobileEntrySummary({...ready,enabled:true,configured:false,setup:{configuration_ready:false,pairing_available:false}}),/資料已設定/);
  assert.match(mobileEntrySummary({...ready,enabled:true,configured:false}),/尚未設定/);
  assert.match(mobileEntrySummary({...ready,configured:true}),/資料已設定/);
  assert.match(mobileEntrySummary({...ready,setup:{configuration_ready:true,pairing_available:false}}),/目前未能使用/);
  assert.match(mobileEntrySummary({...ready,setup:{configuration_ready:true,local_listener_ready:true,transport:{kind:'tsnet',running:true,listening:true,configuration_matches:true}}}),/手機是否能連入，仍要掃碼配對後確認/);
  let writes=0;const c=new MobileOwnerController({read:async()=>({...ready,setup:{pairing_available:false}}),create:()=>{writes++;}});
  await c.open();assert.equal(await c.pair({identity:'owner@example.com',label:'Phone',view_agents:['agent:a'],send_agents:[]}),false);assert.equal(writes,0);c.close();
});
test('new-phone assistant includes install and VPN guidance, changes steps without creating grants',async()=>{
  const {body,slot,restore}=installOwnerDOM(),calls=[];
  const c=new MobileOwnerController({read:async()=>({...ready,devices:[],setup:{configuration_ready:true,pairing_available:true,setup_prompt:'A generic local setup prompt'}}),create:()=>calls.push('pair')});
  let ui;try{
    ui=initMobileOwner({slot,controller:c});await c.open();
    const wizard=body.querySelector('.mobile-owner-wizard'),daily=body.querySelector('.mobile-owner-daily');
    assert.equal(wizard.hidden,false);assert.equal(daily.hidden,true);
    const panels=descendants(wizard).filter(el=>el.className==='mobile-owner-step');assert.deepEqual(panels.map(el=>el.hidden),[false,true,true,true]);
    assert.match(panels[0].textContent,/下載官方 Tailscale/);assert.match(panels[0].textContent,/允許加入 VPN/);assert.match(panels[0].textContent,/Connected/);assert.match(panels[0].textContent,/勿直接關掉/);
    const links=descendants(panels[0]).filter(el=>el.tagName==='a');assert.ok(links.some(el=>el.href==='https://play.google.com/store/apps/details?id=com.tailscale.ipn'));assert.ok(links.some(el=>el.href==='https://apps.apple.com/app/tailscale/id1470499037'));assert.ok(links.every(el=>el.rel==='noopener noreferrer'));
    await descendants(wizard).find(el=>el.tagName==='button'&&el.textContent==='手機已顯示 Connected，下一步').fire('click');
    assert.deepEqual(panels.map(el=>el.hidden),[true,false,true,true]);assert.deepEqual(calls,[]);
    await descendants(wizard).find(el=>el.tagName==='button'&&el.textContent==='下一步：選擇手機權限').fire('click');
    assert.deepEqual(panels.map(el=>el.hidden),[true,true,false,true]);assert.deepEqual(calls,[]);
    assert.match(panels[3].textContent,/Agent 的回覆仍要另行實測/);
    assert.match(daily.textContent,/手機登入/);assert.match(daily.textContent,/不需重掃 QR 碼/);
    assert.match(daily.textContent,/7 天/);assert.match(daily.textContent,/不等於已完成/);
  }finally{ui?.dispose();restore();}
});
test('paired and renewable phones default to everyday guidance without silently enrolling more phones',async()=>{
  for(const state of ['ready','renewal_required']){
    const {body,slot,restore}=installOwnerDOM(),calls=[];
    const c=new MobileOwnerController({read:async()=>({...ready,devices:[{device_id:'phone-a',label:'Phone',state,remembered:true,renewal_expires_at:2000000000}]}),create:()=>calls.push('pair')});
    let ui;try{
      ui=initMobileOwner({slot,controller:c});await c.open();assert.equal(body.querySelector('.mobile-owner-daily').hidden,false);assert.equal(body.querySelector('.mobile-owner-wizard').hidden,true);assert.deepEqual(calls,[]);
      await descendants(body).find(el=>el.tagName==='button'&&el.textContent==='首次連接 / 加入手機').fire('click');assert.equal(body.querySelector('.mobile-owner-wizard').hidden,false);assert.deepEqual(calls,[]);
      if(state==='renewal_required')assert.match(body.querySelector('.mobile-owner-devices').textContent,/會嘗試續期/);
      assert.ok(descendants(body.querySelector('.mobile-owner-devices')).some(el=>el.tagName==='button'&&el.textContent==='儲存權限')); 
    }finally{ui?.dispose();restore();}
  }
});

test('owner may restrict a remembered phone while its short session awaits renewal',async()=>{
  const device={device_id:'phone-a',state:'renewal_required',revision:2,remembered:true,view_mode:'enrolled',send_mode:'enrolled',view_agents:['agent:a'],send_agents:['agent:a']};
  const calls=[],updated={...device,revision:3,send_mode:'selected',send_agents:[]};
  const c=new MobileOwnerController({read:async()=>({...ready,devices:[device]}),updateScopes:async data=>{calls.push(data);return {changed:true,device:updated};}});
  await c.open();assert.equal(await c.updateDeviceScopes('phone-a',{view_mode:'enrolled',send_mode:'selected',view_agents:[],send_agents:[]}),true);
  assert.equal(calls.length,1);assert.deepEqual(c.snapshot.devices[0].send_agents,[]);c.close();
});
