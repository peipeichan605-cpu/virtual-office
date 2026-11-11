import test from 'node:test';
import assert from 'node:assert/strict';
import {SingleHeadPanel,layoutHeadLabels,headPanelPlacement,headOpenAction} from './public/office/scene/dialogue-layout.js';
import {DialogueBubble} from './public/office/scene/dialogue.js';

const viewport={width:1100,height:780};
const overlaps=(a,b)=>a.left<b.right&&a.right>b.left&&a.top<b.bottom&&a.bottom>b.top;
const label=(id,extra={})=>({id,x:500,y:360,width:132,height:34,child:true,...extra});

test('only one reader or family picker owns the head panel, including reentrant close callbacks',()=>{
  const manager=new SingleHeadPanel(),closed=[];
  const owner=id=>({id,closePanels(){closed.push(id);manager.release(this);}});
  const first=owner('reader'),second=owner('picker'),third=owner('other-reader');
  manager.claim(first);manager.claim(second);
  assert.deepEqual(closed,['reader']);assert.equal(manager.current,second);
  manager.claim(second);assert.deepEqual(closed,['reader']);
  manager.claim(third);assert.deepEqual(closed,['reader','picker']);assert.equal(manager.current,third);
  manager.release(first);assert.equal(manager.current,third);
  third.closePanels();assert.equal(manager.current,null);
});

test('retired head controls become inert immediately and ignore queued reader or picker opens',()=>{
  const element={style:{},setAttribute(name,value){this[name]=value;}},reader={element,open:true,teamOpen:true,
    closePanels(){this.open=false;this.teamOpen=false;}};
  DialogueBubble.prototype.disable.call(reader);
  assert.equal(element.inert,true);assert.equal(element.style.display,'none');assert.equal(element['aria-hidden'],'true');
  DialogueBubble.prototype.setOpen.call(reader,true);DialogueBubble.prototype.setTeamOpen.call(reader,true);
  assert.equal(reader.open,false);assert.equal(reader.teamOpen,false);
});

test('a dense sixteen-child family keeps one compact root label instead of overlapping text',()=>{
  const labels=[label('parent',{child:false,width:220}),...Array.from({length:16},(_,i)=>label('child-'+i))];
  const result=layoutHeadLabels(labels,viewport);
  assert.equal(result.size,17);assert.equal(result.get('parent').visible,true);
  assert.equal([...result.values()].filter(x=>x.visible).length,1);
  assert.equal(result.get('parent').height,34);
});

test('selected and keyboard-focused children take precedence over an unselected parent',()=>{
  const entries=[label('root',{child:false}),label('child',{selected:true})];
  const selected=layoutHeadLabels(entries,viewport);
  assert.equal(selected.get('child').visible,true);assert.equal(selected.get('root').visible,false);
  const focused=layoutHeadLabels([label('root',{child:false}),label('child',{focused:true})],viewport);
  assert.equal(focused.get('child').visible,true);assert.equal(focused.get('root').visible,false);
});

test('decluttering is deterministic when state delivery reorders siblings',()=>{
  const entries=Array.from({length:16},(_,i)=>label('child-'+i,{x:330+i%4*125,y:240+Math.floor(i/4)*34}));
  const a=layoutHeadLabels(entries,viewport),b=layoutHeadLabels([...entries].reverse(),viewport);
  for(const item of entries)assert.deepEqual(a.get(item.id),b.get(item.id));
  const visible=[...a.values()].filter(x=>x.visible);
  for(let i=0;i<visible.length;i++)for(let j=0;j<i;j++)assert.equal(overlaps(visible[i],visible[j]),false);
});

test('an expanded reader reserves its complete rectangle and quiets nearby sibling pills',()=>{
  const entries=[label('open',{expanded:true,selected:true,width:350,height:380}),
    label('sibling-a',{x:450,y:500}),label('sibling-b',{x:590,y:650}),label('far',{x:940,y:600})];
  const placed=layoutHeadLabels(entries,viewport);
  assert.equal(placed.get('open').visible,true);assert.equal(placed.get('sibling-a').visible,false);
  assert.equal(placed.get('sibling-b').visible,false);assert.equal(placed.get('far').visible,true);
});

test('expanded readers fit mobile edges and switch below a head near the top',()=>{
  const screen={width:320,height:480};
  for(const x of [2,318]){
    const at=headPanelPlacement({x,y:30},{width:350,height:360},screen);
    assert.ok(at.left>=12);assert.ok(at.right<=308);assert.ok(at.top>=0);assert.ok(at.bottom<=468);
    assert.equal(at.below,true);
  }
});

test('full-mode expanded reader respects the gap between side panels and compact labels avoid panels',()=>{
  const screen={width:1100,height:780,left:350,right:750,obstacles:[{left:0,right:335,top:90,bottom:680}]};
  const entries=[label('open',{x:200,expanded:true,width:350,height:320}),label('behind-panel',{x:180,y:600})];
  const placed=layoutHeadLabels(entries,screen);
  assert.ok(placed.get('open').left>=350);assert.ok(placed.get('open').right<=750);
  assert.equal(placed.get('behind-panel').visible,false);
});

test('off-screen child labels remain hidden while a selected actor moves into view',()=>{
  const placed=layoutHeadLabels([label('outside',{x:-100,selected:true}),label('inside',{x:500,selected:true})],viewport);
  assert.equal(placed.get('outside').visible,false);assert.equal(placed.get('inside').visible,true);
});

test('a focused reader whose head moves off-screen still reserves its clamped visible panel',()=>{
  const screen={width:800,height:600};
  const placed=layoutHeadLabels([label('reader',{x:-40,y:200,width:350,height:300,expanded:true,focused:true}),
    label('sibling',{x:200,y:350,width:100,height:30})],screen);
  assert.equal(placed.get('reader').visible,true);assert.equal(placed.get('sibling').visible,false);
  assert.ok(placed.get('reader').left>=12);assert.ok(placed.get('reader').right<=788);
});

test('a child without a standing slot can still open its local history, without accepting arbitrary links',()=>{
  const bot={chat_url:'/office/agent.html?agent=fixture-child#fixture-office'};
  assert.deepEqual(headOpenAction(bot,true),{mode:'head'});
  assert.deepEqual(headOpenAction(bot,false),{mode:'history',url:bot.chat_url});
  assert.deepEqual(headOpenAction({chat_url:'javascript:alert(1)'},false),{mode:'unavailable'});
  assert.deepEqual(headOpenAction({chat_url:'https://other.invalid/'},false),{mode:'unavailable'});
});
