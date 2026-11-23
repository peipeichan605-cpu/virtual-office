import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {isMobileAccess,mobileRoute,mobileRequest,ensureMobileSession,clearMobileSession} from './public/office/mobile-access.js';
import {officeGet,officePost,agentPageUrl} from './public/office/office-api.js';
import {canManageAgent} from './public/office/subagent-view.js';
import {connectFeed} from './public/office/feed.js';
import {takePairingToken} from './public/office/mobile-pair.js';
import {CommandComposer} from './public/office/commands.js';

function globals(t,values){
  const prior=new Map();
  for(const [key,value] of Object.entries(values)){prior.set(key,Object.getOwnPropertyDescriptor(globalThis,key));Object.defineProperty(globalThis,key,{configurable:true,writable:true,value});}
  t.after(()=>{clearMobileSession();for(const [key,value] of prior){if(value)Object.defineProperty(globalThis,key,value);else delete globalThis[key];}});
}
const reply=(body,status=200)=>({ok:status>=200&&status<300,status,json:async()=>body});

test('mobile mode never puts an owner key in Agent links and cannot manage membership or seating',t=>{
  globals(t,{location:{pathname:'/mobile/',hash:'#owner-secret'},document:undefined});
  assert.equal(isMobileAccess(),true);
  assert.equal(agentPageUrl('codex:a','owner-secret','run-a'),'/mobile/agent?agent=codex%3Aa&run=run-a');
  assert.equal(canManageAgent({agent_id:'codex:a'}),false);
});

test('mobile API client refuses every owner management route before contacting the server',async t=>{
  let calls=0;globals(t,{location:{pathname:'/mobile/'},document:undefined,fetch:async()=>{calls++;return reply({});}});
  for(const path of ['/api/office/mobile','/api/office/invitations','/api/office/grok/sdk/login/status','/api/office/seating'])await assert.rejects(officeGet(path,'owner-key'),/mobile_endpoint_forbidden/);
  for(const path of ['/api/office/permissions','/api/office/agents/logout','/api/office/seating','/api/office/grok/sdk/login','/api/office/mobile/pairings'])await assert.rejects(officePost(path,'owner-key',{}),/mobile_endpoint_forbidden/);
  for(const path of ['https://example.com/api/office/work','//evil.test/api/office/work','/api/office/work#secret','/api/office/commands/other'])assert.throws(()=>mobileRoute(path),/mobile_endpoint_forbidden/);
  assert.equal(calls,0);
});

test('mobile reads use cookie authentication without owner header; writes use only session CSRF',async t=>{
  const calls=[];globals(t,{location:{pathname:'/mobile/'},document:undefined,fetch:async(url,options)=>{
    calls.push({url,options});
    return reply(url==='/api/mobile/session'?{authenticated:true,csrf_token:'fixture-csrf',device:{device_id:'device-a'}}:{ok:true});
  }});
  clearMobileSession();
  await officeGet('/api/office/messages?agent=codex%3Aa','SHOULD_NEVER_BE_SENT');
  await officePost('/api/office/commands','SHOULD_NEVER_BE_SENT',{agent_id:'codex:a',request_id:'id',text:'hello'});
  assert.deepEqual(calls.map(c=>c.url),['/api/mobile/messages?agent=codex%3Aa','/api/mobile/session','/api/mobile/commands']);
  for(const {options} of calls){assert.equal(options.credentials,'same-origin');assert.equal(options.cache,'no-store');assert.equal(options.headers?.['X-Office-Key'],undefined);}
  assert.equal(calls[2].options.headers['X-Mobile-CSRF'],'fixture-csrf');
  assert.equal(JSON.stringify(calls).includes('SHOULD_NEVER_BE_SENT'),false);
});

test('phone scope denials remain denials and do not silently retry or switch endpoints',async t=>{
  const paths=[];globals(t,{location:{pathname:'/mobile/'},document:undefined,fetch:async(path)=>{paths.push(path);return reply({error:'mobile_forbidden'},403);}});
  await assert.rejects(officeGet('/api/office/messages?agent=denied',''),error=>error.status===403&&error.code==='mobile_forbidden');
  assert.deepEqual(paths,['/api/mobile/messages?agent=denied']);
});

test('mobile status and delivery use local-authoritative API only and never read/write feed cache',async t=>{
  const timers=new Map(),states=[],paths=[];let phase='working';
  globals(t,{location:{pathname:'/mobile/'},document:undefined,
    localStorage:{getItem(){assert.fail('mobile read cached status');},setItem(){assert.fail('mobile wrote cached status');},removeItem(){assert.fail('mobile touched feed cache');}},
    EventSource:class{constructor(){assert.fail('mobile subscribed to legacy feed');}},
    setInterval:(fn,ms)=>{timers.set(ms,fn);return ms;},clearInterval:id=>timers.delete(id),setTimeout:()=>1,clearTimeout:()=>{},
    fetch:async(path,options)=>{
      paths.push(path);assert.equal(options.headers?.['X-Office-Key'],undefined);
      if(path==='/api/mobile/health')return reply({ok:true,local_ok:true,sync_ok:true});
      assert.equal(path,'/api/mobile/work');
      return reply({local_ok:true,feed_enabled:true,history_ok:true,agents:[{id:'codex:a',name:'Agent A',provider:'codex',state:phase,step:phase,state_changed_at:new Date().toISOString()}],
        finished:phase==='done'?[{id:'run-a',agent_id:'codex:a',name:'Agent A',status:'done',started_at:new Date().toISOString(),delivery_preview:'Completed fixture'}]:[]});
    }});
  const channel=await connectFeed({topic:'IGNORED_OWNER',servers:['https://ntfy.sh']},state=>states.push(state));
  assert.equal(states.at(-1).connection.status,'live');
  assert.equal(Object.values(states.at(-1).bots)[0].chat_url,'/mobile/agent?agent=codex%3Aa');
  phase='done';await timers.get(2000)();
  assert.equal(states.at(-1).finished[0].step,'Completed fixture');
  assert.equal(states.at(-1).finished[0].url,'/mobile/agent?agent=codex%3Aa&run=run-a');
  assert.equal(paths.some(path=>path.includes('IGNORED_OWNER')||path.includes('ntfy')),false);
  channel.close();assert.equal(timers.size,0);
});

test('mobile composer discards approval controls even if a response incorrectly contains them',t=>{
  globals(t,{location:{pathname:'/mobile/'},document:undefined});
  let replaced=0;
  const fake={approvalSignature:null,approvalBox:{replaceChildren(){replaced++;}},scope(){return {};}};
  CommandComposer.prototype.renderApprovals.call(fake,[{id:'approval',can_allow:true,preview:'private'}]);
  assert.equal(fake.approvalSignature,'[]');assert.equal(replaced,1);
});

test('mobile pending commands expose cancel only for explicitly cancellable commands and re-render on permission changes',t=>{
  const element=tag=>({tag,children:[],append(...children){this.children.push(...children);},replaceChildren(...children){this.children=children;},setAttribute(){},addEventListener(){}});
  globals(t,{location:{pathname:'/mobile/'},document:{querySelector:()=>null,createElement:element}});
  const fake={element:element('root'),scope:()=>({}),pendingSignature:null};
  const row={id:'command-a',state:'pending',preview:'A visible pending message',can_cancel:false};
  CommandComposer.prototype.renderPending.call(fake,[row]);
  assert.deepEqual(fake.pendingBox.children[1].children.map(child=>child.tag),['span']);
  CommandComposer.prototype.renderPending.call(fake,[{...row,can_cancel:true}]);
  assert.deepEqual(fake.pendingBox.children[1].children.map(child=>child.tag),['span','button']);
  CommandComposer.prototype.renderPending.call(fake,[{...row,can_cancel:undefined}]);
  assert.deepEqual(fake.pendingBox.children[1].children.map(child=>child.tag),['span']);
  location.pathname='/office/';
  CommandComposer.prototype.renderPending.call(fake,[{...row,can_cancel:undefined}]);
  assert.deepEqual(fake.pendingBox.children[1].children.map(child=>child.tag),['span','button']);
});

test('mobile expiration and revocation clearly distinguish cancelled unexecuted commands',()=>{
  const fake={status:{dataset:{}}};
  CommandComposer.prototype.show.call(fake,{state:'cancelled',preview:'demo',detail_code:'mobile_command_expired'});
  assert.match(fake.status.textContent,/已過期.*沒有送交 Agent 執行/);
  CommandComposer.prototype.show.call(fake,{state:'cancelled',preview:'demo',detail_code:'mobile_authorization_revoked'});
  assert.match(fake.status.textContent,/授權已撤銷.*沒有送交 Agent 執行/);
});

test('one-time pairing fragment is removed immediately and malformed fragments never become tokens',()=>{
  const replacements=[],history={replaceState:(...args)=>replacements.push(args[2])};
  const token='A'.repeat(43);
  assert.equal(takePairingToken({pathname:'/mobile/pair',search:'',hash:'#pair='+token},history),token);
  assert.equal(replacements[0],'/mobile/pair');
  assert.equal(takePairingToken({pathname:'/mobile/pair',search:'',hash:'#pair=bad%20token'},history),'');
  assert.equal(replacements.length,2);
});

test('phone entries keep 3D scene but exclude owner setup and remote executable dependencies',async()=>{
  const html=await readFile(new URL('./public/office/mobile.html',import.meta.url),'utf8');
  assert.match(html,/<main id="scene"/);assert.match(html,/<meta name="office-access" content="mobile">/);
  assert.match(html,/\/assets\/vendor\/three\/build\/three.module.js/);
  assert.match(html,/\/office\/mobile-entry.js/);
  assert.doesNotMatch(html,/https:\/\/cdn|fonts.googleapis|src="[^\"]*invite.js|mobile-owner/);
  const pair=await readFile(new URL('./public/office/mobile-pair.html',import.meta.url),'utf8');
  assert.match(pair,/type="password" autocomplete="off"/);assert.doesNotMatch(pair,/\son[a-z]+=/);
});

test('the phone shell offers a local retry if the first module cannot load',async t=>{
  const html=await readFile(new URL('./public/office/mobile.html',import.meta.url),'utf8');
  const retryTag=html.match(/<a [^>]*id="mobile-boot-retry"[^>]*>/)?.[0];
  assert.match(retryTag,/href="\/mobile\/"/);assert.match(retryTag,/\bhidden\b/);
  const module=html.match(/<script type="module">([\s\S]*?)<\/script>/)?.[1];
  assert.ok(module);
  const retry={hidden:true},label={textContent:''},track={removed:false,remove(){this.removed=true;}};
  const loading={classList:new Set(),querySelector(name){return {'.hq-loading-label':label,'.hq-loading-track':track}[name];}};
  globals(t,{document:{
    querySelector(name){assert.equal(name,'.hq-loading');return loading;},
    getElementById(id){assert.equal(id,'mobile-boot-retry');return retry;},
  }});
  // A data: module has no resolvable origin for the absolute-path entry import.
  await import('data:text/javascript,'+encodeURIComponent(module));
  assert.match(label.textContent,/啟動檔案/);assert.equal(track.removed,true);assert.equal(retry.hidden,false);
});

test('mobile polling marks an outage stale and restores authoritative delivery without sending a command',async t=>{
  const timers=new Map(),states=[],requests=[];let offline=false,finished=false;
  globals(t,{location:{pathname:'/mobile/'},document:undefined,
    localStorage:{getItem(){assert.fail('mobile read legacy cache');},setItem(){assert.fail('mobile wrote legacy cache');}},
    setInterval:(fn,ms)=>{timers.set(ms,fn);return ms;},clearInterval:id=>timers.delete(id),setTimeout:()=>1,clearTimeout:()=>{},
    fetch:async(path,options)=>{
      requests.push({path,method:options.method});
      if(offline)throw new TypeError('Simulated private-network interruption');
      if(path==='/api/mobile/health')return reply({ok:true,local_ok:true,sync_ok:true});
      assert.equal(path,'/api/mobile/work');
      return reply({local_ok:true,history_ok:true,agents:[{id:'codex:recovery',name:'Recovery',provider:'codex',state:finished?'done':'working',step:finished?'Delivered':'Working',state_changed_at:new Date().toISOString()}],
        finished:finished?[{id:'recovered-run',agent_id:'codex:recovery',name:'Recovery',status:'done',delivery_preview:'Recovered delivery'}]:[]});
    }});
  const channel=await connectFeed({topic:'UNUSED_OWNER',servers:[]},state=>states.push(state));
  assert.equal(states.at(-1).connection.status,'live');
  offline=true;await timers.get(2000)();
  assert.notEqual(states.at(-1).connection.status,'live');
  await timers.get(5000)();assert.equal(states.at(-1).connection.status,'offline');
  offline=false;finished=true;
  await timers.get(5000)();assert.notEqual(states.at(-1).connection.status,'live','Health alone is not fresh work evidence');
  await timers.get(2000)();
  assert.equal(states.at(-1).connection.status,'live');
  assert.equal(states.at(-1).finished[0].step,'Recovered delivery');
  assert.ok(requests.every(({path,method})=>method==='GET'&&['/api/mobile/work','/api/mobile/health'].includes(path)));
  channel.close();assert.equal(timers.size,0);
});

test('session revocation removes sensitive visible content and navigates back to pairing without retries',async t=>{
  clearMobileSession();let redirected=null,replacement=null,calls=0;
  globals(t,{location:{pathname:'/mobile/',replace:path=>{redirected=path;}},
    document:{querySelector:()=>({content:'mobile'}),createElement:()=>({}),body:{replaceChildren:n=>{replacement=n;}}},
    fetch:async()=>{calls++;return reply({error:'mobile_session_revoked'},401);}});
  await assert.rejects(ensureMobileSession(),error=>error.status===401);
  assert.equal(calls,1);assert.equal(redirected,'/mobile/pair');assert.match(replacement.textContent,/登入已失效/);
});
