import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import * as pairing from './public/office/mobile-pair.js';

const ORIGIN='https://office.fixture.test';
const TOKEN='Pairing_Fixture-0123456789abcdefghijk';
const pairUrl=(token=TOKEN)=>`${ORIGIN}/mobile/pair#pair=${token}`;
const reply=(body,status=200)=>({ok:status>=200&&status<300,status,json:async()=>body});

class Element {
  constructor(){this.value='';this.textContent='';this.checked=false;this.disabled=false;this.listeners=new Map();}
  addEventListener(type,handler){const list=this.listeners.get(type)||[];list.push(handler);this.listeners.set(type,list);}
  async emit(type,event={}){await Promise.all((this.listeners.get(type)||[]).map(handler=>handler(event)));}
}

function fixture(t,{hash='',remember=false,checkbox=true,fetcher=async()=>reply({paired:true})}={}){
  const form=new Element(),input=new Element(),notice=new Element(),button=new Element(),rememberDevice=new Element();
  rememberDevice.checked=remember;
  const nodes=new Map([['mobile-pair-form',form],['pair-token',input],['pair-notice',notice]]);
  if(checkbox)nodes.set('remember-device',rememberDevice);
  form.querySelector=selector=>selector.includes('button')?button:null;
  const doc={getElementById:id=>nodes.get(id)||null},calls=[],replacements=[],navigations=[],events=[];
  const values={
    document:doc,
    location:{origin:ORIGIN,protocol:'https:',pathname:'/mobile/pair',search:'',hash,href:`${ORIGIN}/mobile/pair${hash}`,replace:path=>navigations.push(path)},
    history:{replaceState(_state,_title,path){events.push('fragment-cleared');replacements.push(path);values.location.hash='';}},
    sessionStorage:{length:0,key(){return null;},removeItem(){}},
    localStorage:{setItem(){assert.fail('pairing must not persist a pairing token in localStorage');}},
  };
  const prior=new Map();
  for(const [name,value] of Object.entries(values)){
    prior.set(name,Object.getOwnPropertyDescriptor(globalThis,name));
    Object.defineProperty(globalThis,name,{configurable:true,writable:true,value});
  }
  t.after(()=>{for(const [name,value] of prior){if(value)Object.defineProperty(globalThis,name,value);else delete globalThis[name];}});
  const fetch=async(url,options)=>{events.push('request');calls.push({url,options,body:JSON.parse(options.body)});return fetcher(url,options);};
  pairing.initMobilePair(doc,fetch);
  return {form,input,notice,button,rememberDevice,calls,replacements,navigations,events,
    async submit(value=input.value){input.value=value;let prevented=false;await form.emit('submit',{preventDefault(){prevented=true;}});assert.equal(prevented,true);},
  };
}

test('pairing input accepts a raw token at both bounds and a same-origin HTTPS pairing URL',()=>{
  for(const value of ['A'.repeat(20),'z'.repeat(256),TOKEN,pairUrl()]){
    assert.equal(pairing.parsePairingInput(value,ORIGIN),value.startsWith('https:')?TOKEN:value);
  }
  assert.equal(pairing.parsePairingInput(`  ${TOKEN}\n`,ORIGIN),TOKEN);
  assert.equal(pairing.parsePairingInput(`  ${pairUrl()}\n`,ORIGIN),TOKEN);
});

test('invalid raw tokens and values cannot become pairing credentials',()=>{
  for(const value of ['', 'A'.repeat(19), 'A'.repeat(257), `${TOKEN}=`, `a b${TOKEN}`, `${TOKEN}/`, null, undefined, {}, ['A'.repeat(20)]]){
    assert.equal(pairing.parsePairingInput(value,ORIGIN),'');
  }
});

const unsafeUrls=[
  ['unencrypted URL',`http://office.fixture.test/mobile/pair#pair=${TOKEN}`],
  ['different origin',`https://elsewhere.fixture.test/mobile/pair#pair=${TOKEN}`],
  ['lookalike suffix',`https://office.fixture.test.evil.test/mobile/pair#pair=${TOKEN}`],
  ['lookalike prefix',`https://eviloffice.fixture.test/mobile/pair#pair=${TOKEN}`],
  ['different port',`https://office.fixture.test:444/mobile/pair#pair=${TOKEN}`],
  ['username',`https://user@office.fixture.test/mobile/pair#pair=${TOKEN}`],
  ['password',`https://user:password@office.fixture.test/mobile/pair#pair=${TOKEN}`],
  ['origin in username',`https://office.fixture.test@evil.test/mobile/pair#pair=${TOKEN}`],
  ['relative URL',`/mobile/pair#pair=${TOKEN}`],
  ['protocol-relative URL',`//office.fixture.test/mobile/pair#pair=${TOKEN}`],
  ['non-HTTPS scheme',`javascript:${pairUrl()}`],
  ['wrong path',`${ORIGIN}/mobile/#pair=${TOKEN}`],
  ['trailing slash',`${ORIGIN}/mobile/pair/#pair=${TOKEN}`],
  ['extra path',`${ORIGIN}/mobile/pair/extra#pair=${TOKEN}`],
  ['case-changed path',`${ORIGIN}/Mobile/pair#pair=${TOKEN}`],
  ['encoded path',`${ORIGIN}/mobile/%70air#pair=${TOKEN}`],
  ['encoded slash',`${ORIGIN}/mobile%2fpair#pair=${TOKEN}`],
  ['encoded dot normalization',`${ORIGIN}/mobile/%2e/pair#pair=${TOKEN}`],
  ['dot normalization',`${ORIGIN}/mobile/other/../pair#pair=${TOKEN}`],
  ['repeated slash',`${ORIGIN}//mobile/pair#pair=${TOKEN}`],
  ['query string',`${ORIGIN}/mobile/pair?from=fixture#pair=${TOKEN}`],
  ['empty query delimiter',`${ORIGIN}/mobile/pair?#pair=${TOKEN}`],
  ['missing fragment',`${ORIGIN}/mobile/pair`],
  ['wrong fragment key',`${ORIGIN}/mobile/pair#token=${TOKEN}`],
  ['duplicate token',`${pairUrl()}&pair=${TOKEN}`],
  ['extra fragment field',`${pairUrl()}&remember_device=true`],
  ['invalid fragment token',`${ORIGIN}/mobile/pair#pair=short`],
  ['fragment delimiter inside token',`${pairUrl()}#extra`],
  ['backslash in path',`${ORIGIN}/mobile\\pair#pair=${TOKEN}`],
  ['backslash in authority',`https://office.fixture.test\\@evil.test/mobile/pair#pair=${TOKEN}`],
  ['URL parser strips embedded newline',`https://office.fixture.test/mo\nbile/pair#pair=${TOKEN}`],
  ['URL parser strips embedded tab',`https://office.fixture.\ttest/mobile/pair#pair=${TOKEN}`],
];

test('pasted URLs must identify exactly the HTTPS origin and pairing route without hidden fields',()=>{
  for(const [label,value] of unsafeUrls)assert.equal(pairing.parsePairingInput(value,ORIGIN),'',label);
});

test('pairing fragments are consumed synchronously and only one pair field is accepted',()=>{
  const hashes=[`#pair=${TOKEN}`,`#pair=${TOKEN}&pair=${TOKEN}`,`#pair=${TOKEN}&extra=1`,'#pair=short',`#other=${TOKEN}`];
  for(const hash of hashes){
    const calls=[],location={origin:ORIGIN,pathname:'/mobile/pair',search:'?fixture=1',hash};
    const history={replaceState(_state,_title,path){calls.push(path);location.hash='';}};
    assert.equal(pairing.takePairingToken(location,history),hash===hashes[0]?TOKEN:'');
    assert.equal(location.hash,'');
    assert.deepEqual(calls,['/mobile/pair?fixture=1']);
  }
  let calls=0;
  assert.equal(pairing.takePairingToken({origin:ORIGIN,pathname:'/mobile/pair',search:'',hash:''},{replaceState(){calls++;}}),'');
  assert.equal(calls,0);
});

test('opening a pairing link only prefills the token, clears its fragment and waits for submit',async t=>{
  const f=fixture(t,{hash:`#pair=${TOKEN}`});
  assert.equal(f.input.value,TOKEN);
  assert.deepEqual(f.replacements,['/mobile/pair']);
  assert.deepEqual(f.events,['fragment-cleared']);
  assert.equal(f.calls.length,0);
  f.rememberDevice.checked=true;
  await f.rememberDevice.emit('change');
  assert.equal(f.calls.length,0);
  assert.equal(f.navigations.length,0);
});

test('invalid pasted URLs and tokens never cause a pairing request or disclose their content',async t=>{
  const f=fixture(t);
  for(const value of ['short',`${TOKEN}=`,...unsafeUrls.map(([,value])=>value)]){
    await f.submit(value);
    assert.equal(f.calls.length,0);
    assert.equal(f.navigations.length,0);
    assert.ok(f.notice.textContent.length>0);
    assert.equal(f.notice.textContent.includes(TOKEN),false);
    assert.equal(f.button.disabled,false);
  }
});

for(const remember of [false,true])test(`explicit pairing submits only token and remember_device=${remember}`,async t=>{
  const f=fixture(t);
  assert.equal(f.rememberDevice.checked,false);
  f.rememberDevice.checked=remember;
  await f.submit(pairUrl());
  assert.equal(f.calls.length,1);
  const {url,options,body}=f.calls[0];
  assert.equal(url,'/api/mobile/pair');
  assert.equal(options.method,'POST');
  assert.equal(options.credentials,'same-origin');
  assert.equal(options.cache,'no-store');
  assert.deepEqual(body,{token:TOKEN,remember_device:remember});
  assert.equal(options.headers['Content-Type'],'application/json');
  assert.equal(options.headers['X-Office-Key'],undefined);
  assert.equal(f.input.value,'');
  assert.deepEqual(f.navigations,['/mobile/']);
});

test('an unavailable remember-device checkbox cannot opt the phone into persistence',async t=>{
  const f=fixture(t,{checkbox:false});
  await f.submit(TOKEN);
  assert.deepEqual(f.calls.map(call=>call.body),[{token:TOKEN,remember_device:false}]);
});

test('server and transport errors cannot put the pairing token in visible errors or navigation',async t=>{
  let status=400,networkFailure=false;
  const f=fixture(t,{fetcher:async()=>{
    if(networkFailure)throw new Error(`fixture network detail ${TOKEN}`);
    return reply({error:`fixture server detail ${TOKEN}`},status);
  }});
  for(status of [400,401,403,410,429,500]){
    await f.submit(TOKEN);
    assert.ok(f.notice.textContent.length>0);
    assert.equal(f.notice.textContent.includes(TOKEN),false);
    assert.equal(f.notice.textContent.includes('fixture server detail'),false);
    assert.equal(f.button.disabled,false);
  }
  networkFailure=true;
  await f.submit(TOKEN);
  assert.equal(f.notice.textContent.includes(TOKEN),false);
  assert.equal(f.notice.textContent.includes('fixture network detail'),false);
  assert.equal(f.navigations.length,0);
  assert.equal(f.calls.length,7,'errors never automatically retry pairing');
});

test('a pending pairing blocks a second submit until its request settles',async t=>{
  let resolve;
  const pending=new Promise(done=>{resolve=done;});
  const f=fixture(t,{fetcher:()=>pending});
  const first=f.submit(TOKEN);
  assert.equal(f.button.disabled,true);
  await f.submit(TOKEN);
  assert.equal(f.calls.length,1);
  resolve(reply({error:'pair_expired'},410));
  await first;
  assert.equal(f.button.disabled,false);
  assert.equal(f.navigations.length,0);
});

test('the remember-device choice is an unchecked checkbox in the delivered form',async()=>{
  const html=await readFile(new URL('./public/office/mobile-pair.html',import.meta.url),'utf8');
  const checkbox=(html.match(/<input\b[^>]*>/g)||[]).find(tag=>/\bid=["']remember-device["']/.test(tag));
  assert.ok(checkbox,'pairing page contains the explicit remember-device choice');
  assert.match(checkbox,/\btype=["']checkbox["']/);
  assert.doesNotMatch(checkbox,/\bchecked(?:\s|=|\/?>)/);
});


test('already-paired phones have an ordinary same-origin return link without replaying a one-use pairing',async()=>{
  const html=await readFile(new URL('./public/office/mobile-pair.html',import.meta.url),'utf8');
  assert.match(html,/<a href="\/mobile\/">已配對？返回 OFFICE<\/a>/);
  assert.match(html,/舊配對連結只能使用一次/);
  assert.match(html,/手機登入/);
  assert.doesNotMatch(html,/<meta[^>]+http-equiv=["']refresh/i);
});
