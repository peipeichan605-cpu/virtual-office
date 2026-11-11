import test from 'node:test';
import assert from 'node:assert/strict';

globalThis.location={hash:''};
const {workspaceChooser}=await import('./public/office/workspaces.js');
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
class Element{
  constructor(tag='div'){this.tag=tag;this.children=[];this.handlers=new Map();this.parts={};this.value='';this.hidden=false;this.disabled=false;this.open=false;}
  set innerHTML(text){
    if(text.includes('workspace-new'))this.parts={'select':new Element('select'),'.workspace-new':new Element(),'.workspace-path':new Element('input'),'.workspace-name':new Element('input')};
    if(text.includes('workspace-heading')){
      const form=new Element('form'),button=new Element('button');form.parts.button=button;form.append(button);
      this.parts={'form':form,'.invite-close':new Element('button'),'.invite-intro':new Element('p'),'.invite-notice':new Element('p')};
    }
  }
  querySelector(selector){return this.parts[selector];}
  setAttribute(name,value){this[name]=value;}
  append(...items){this.children.push(...items);}
  prepend(...items){this.children.unshift(...items);}
  replaceChildren(...items){this.children=[...items];}
  addEventListener(type,callback){const callbacks=this.handlers.get(type)||[];callbacks.push(callback);this.handlers.set(type,callbacks);}
  async emit(type,event={}){return Promise.all((this.handlers.get(type)||[]).map(callback=>callback(event)));}
  showModal(){this.open=true;}
  close(){this.open=false;this.emit('close');}
}
function environment(t,hash=''){
  const saved={document:globalThis.document,window:globalThis.window,location:globalThis.location};
  const body=new Element('body');globalThis.document={body,createElement:tag=>new Element(tag)};globalThis.window=new Element('window');globalThis.location={hash};
  t.after(()=>{for(const [name,value] of Object.entries(saved))if(value===undefined)delete globalThis[name];else globalThis[name]=value;});
  const calls=[];t.mock.method(globalThis,'fetch',(url,options)=>{const pending=deferred();calls.push({url,options,...pending});return pending.promise;});
  return {body,window:globalThis.window,calls,respond(index,data){calls[index].resolve({ok:true,json:async()=>data});}};
}
const catalog=id=>({workspaces:[{id,name:id}],current:{mode:'manual',id}});
let mounted=0;
async function dialogFixture(t){
  const env=environment(t,'#fixture-owner');await import('./public/office/workspaces.js?scope-fixture='+ ++mounted);
  const dialog=env.body.children[0],form=dialog.querySelector('form'),button=form.querySelector('button'),root=form.children[0];
  return {...env,dialog,form,button,root,select:root.querySelector('select'),notice:dialog.querySelector('.invite-notice')};
}

test('the newest Workspace target wins when an older request resolves last',async t=>{
  const f=environment(t),chooser=workspaceChooser({allowUnassigned:true}),select=chooser.root.querySelector('select');
  const old=chooser.load('fixture-a'),latest=chooser.load('fixture-b');
  f.respond(1,catalog('workspace-b'));assert.equal(await latest,true);assert.equal(select.value,'workspace-b');
  f.respond(0,catalog('workspace-a'));assert.equal(await old,false);assert.equal(select.value,'workspace-b');
});

test('a user Workspace draft is preserved when the current catalog response arrives',async t=>{
  const f=environment(t),chooser=workspaceChooser(),select=chooser.root.querySelector('select');
  const active=chooser.load();select.value='new';await select.emit('change');chooser.root.querySelector('.workspace-path').value='/fixture/user-project';
  f.respond(0,catalog('workspace-old'));assert.equal(await active,true);assert.equal(select.value,'new');assert.equal(chooser.root.querySelector('.workspace-path').value,'/fixture/user-project');
});

test('explicit invalidation or a rejected outer view guard prevents a hidden catalog mutation',async t=>{
  const f=environment(t),chooser=workspaceChooser(),select=chooser.root.querySelector('select');
  const first=chooser.load();chooser.invalidate();f.respond(0,catalog('workspace-a'));assert.equal(await first,false);assert.equal(select.value,'auto');
  const second=chooser.load(undefined,{accept:()=>false});f.respond(1,catalog('workspace-b'));assert.equal(await second,false);assert.equal(select.value,'auto');
});

test('a fresh invitation clears a previous new-Workspace draft and stale required fields',async t=>{
  const f=environment(t),chooser=workspaceChooser(),select=chooser.root.querySelector('select');
  const pending=chooser.load();select.value='new';await select.emit('change');
  const path=chooser.root.querySelector('.workspace-path'),name=chooser.root.querySelector('.workspace-name');
  path.value='/fixture/previous-project';name.value='Previous project';assert.equal(path.required,true);
  chooser.reset();assert.equal(select.value,'auto');assert.equal(path.value,'');assert.equal(name.value,'');
  assert.equal(path.required,false);assert.equal(chooser.root.querySelector('.workspace-new').hidden,true);
  f.respond(0,catalog('workspace-old'));assert.equal(await pending,false);assert.equal(select.value,'auto');
});

test('mounted Workspace dialog never enables save from a stale Agent response',async t=>{
  const f=await dialogFixture(t);
  const a=f.window.emit('office:workspace',{detail:{id:'fixture-a',name:'A'}});
  const b=f.window.emit('office:workspace',{detail:{id:'fixture-b',name:'B'}});
  f.respond(1,catalog('workspace-b'));await b;assert.equal(f.select.value,'workspace-b');assert.equal(f.button.disabled,false);
  f.respond(0,catalog('workspace-a'));await a;assert.equal(f.select.value,'workspace-b');assert.equal(f.dialog.querySelector('.invite-intro').textContent,'B');
  const save=f.form.emit('submit',{preventDefault(){}});await Promise.resolve();
  assert.equal(f.calls[2].url,'/api/office/workspaces/assign');assert.deepEqual(JSON.parse(f.calls[2].options.body),{agent_id:'fixture-b',mode:'manual',workspace_id:'workspace-b'});
  f.respond(2,{saved:true});await save;assert.equal(f.dialog.open,false);
});

test('closing the Workspace dialog fences pending loads and blocks early keyboard submit',async t=>{
  const f=await dialogFixture(t),opening=f.window.emit('office:workspace',{detail:{id:'fixture-a',name:'A'}});
  await f.form.emit('submit',{preventDefault(){}});assert.equal(f.calls.length,1,'No assignment while target catalog is unresolved');
  f.dialog.close();f.respond(0,catalog('workspace-a'));await opening;
  assert.equal(f.dialog.open,false);assert.equal(f.button.disabled,true);assert.equal(f.select.value,'auto');
});

test('late Workspace assignment success cannot close a new Agent dialog',async t=>{
  const f=await dialogFixture(t),opening=f.window.emit('office:workspace',{detail:{id:'fixture-a',name:'A'}});
  f.respond(0,catalog('workspace-a'));await opening;
  const saving=f.form.emit('submit',{preventDefault(){}});await Promise.resolve();assert.equal(f.calls[1].url,'/api/office/workspaces/assign');
  const next=f.window.emit('office:workspace',{detail:{id:'fixture-b',name:'B'}});f.respond(2,catalog('workspace-b'));await next;
  f.respond(1,{saved:true});await saving;assert.equal(f.dialog.open,true);assert.equal(f.select.value,'workspace-b');assert.equal(f.button.disabled,false);
});

test('late Workspace assignment failure cannot replace a new Agent notice',async t=>{
  const f=await dialogFixture(t),opening=f.window.emit('office:workspace',{detail:{id:'fixture-a',name:'A'}});
  f.respond(0,catalog('workspace-a'));await opening;
  const saving=f.form.emit('submit',{preventDefault(){}});await Promise.resolve();
  f.dialog.close();const next=f.window.emit('office:workspace',{detail:{id:'fixture-b',name:'B'}});f.respond(2,catalog('workspace-b'));await next;
  f.calls[1].reject(new Error('Synthetic interruption'));await saving;
  assert.equal(f.dialog.open,true);assert.equal(f.notice.textContent,'');assert.equal(f.select.value,'workspace-b');assert.equal(f.button.disabled,false);
});

test('switching targets while a new Workspace is created prevents assignment of that stale target',async t=>{
  const f=await dialogFixture(t),opening=f.window.emit('office:workspace',{detail:{id:'fixture-a',name:'A'}});
  f.respond(0,catalog('workspace-a'));await opening;f.select.value='new';await f.select.emit('change');f.root.querySelector('.workspace-path').value='/fixture/approved-project';
  const saving=f.form.emit('submit',{preventDefault(){}});assert.equal(f.calls[1].url,'/api/office/workspaces');
  const next=f.window.emit('office:workspace',{detail:{id:'fixture-b',name:'B'}});f.respond(2,catalog('workspace-b'));await next;
  f.respond(1,{id:'workspace-created-for-a'});await saving;assert.equal(f.calls.length,3,'No assignment after the original target was replaced');assert.equal(f.dialog.open,true);assert.equal(f.select.value,'workspace-b');
});
