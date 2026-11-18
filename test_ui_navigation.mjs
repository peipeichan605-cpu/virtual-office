import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const source=await readFile(process.env.OFFICE_UI_TEST_SOURCE || new URL('./public/office/ui/ui.js',import.meta.url),'utf8');
const start=source.indexOf('async function turnOffRoutine(');
const end=source.indexOf('\nfunction renderRoutines(',start);
assert.ok(start>=0&&end>start);
const action=source.slice(start,end);

// Execute the actual UI action with boundary doubles for clipboard, selection
// and timers. No DOM, Office API, private state, or real clipboard is accessed.
function fixture({copySucceeds=true,hasAgent=true}={}) {
  const copied=[],selected=[],navigated=[],cleared=[],timers=[],classes=new Set();
  const bot={id:'fixture-bot',chat_url:'/office/agent.html?agent=fixture'};
  const U={h:{onSelectBot:id=>selected.push(id)},last:{state:{bots:hasAgent?{[bot.id]:bot}:{}}},dock:{copied:new Map([['key',42]])}};
  const context=vm.createContext({U,copyText:async text=>{copied.push(text);return copySucceeds;},
    setText:(node,text)=>{node.textContent=text;},openChat:(...args)=>navigated.push(args),
    clearTimeout:id=>cleared.push(id),setTimeout:(fn,ms)=>{timers.push({fn,ms});return 43;}});
  const run=vm.runInContext(action+'\nturnOffRoutine',context);
  const ref={key:'key',noteText:{},li:{classList:{add:name=>classes.add(name),remove:name=>classes.delete(name)}}};
  return {run,ref,routine:{bot:bot.id,routine:'每日檢查'},copied,selected,navigated,cleared,timers,classes,U};
}

test('routine pause copies the instruction and selects its Agent without leaving Office',async()=>{
  const f=fixture();await f.run(f.routine,f.ref);
  assert.deepEqual(f.copied,['pause routine 每日檢查']);
  assert.deepEqual(f.selected,['fixture-bot']);
  assert.deepEqual(f.navigated,[]);
  assert.match(f.ref.noteText.textContent,/已複製/);
  assert.deepEqual(f.cleared,[42]);assert.equal(f.timers[0].ms,4200);
  f.timers[0].fn();assert.equal(f.classes.has('is-copied'),false);assert.equal(f.U.dock.copied.has('key'),false);
});

test('clipboard refusal keeps a manual instruction and in-place selection without navigation',async()=>{
  const f=fixture({copySucceeds:false});await f.run(f.routine,f.ref);
  assert.match(f.ref.noteText.textContent,/pause routine 每日檢查/);
  assert.deepEqual(f.selected,['fixture-bot']);assert.deepEqual(f.navigated,[]);
});

test('a retired routine owner never opens a stale stored chat URL',async()=>{
  const f=fixture({hasAgent:false});await f.run(f.routine,f.ref);
  assert.deepEqual(f.copied,['pause routine 每日檢查']);
  assert.deepEqual(f.selected,[]);assert.deepEqual(f.navigated,[]);
});
