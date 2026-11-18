import test from 'node:test';
import assert from 'node:assert/strict';
import {createFramePacer} from './public/office/scene/frame-pacer.js';

for (const hz of [60, 120, 144]) {
  test(`30 fps budget stays on real time on a ${hz} Hz display`, () => {
    const shouldDraw = createFramePacer(30), draws = [];
    for (let tick = 0; tick <= hz * 10; tick++) {
      const now = tick * 1000 / hz;
      if (shouldDraw(now)) draws.push(now);
    }
    assert.equal(draws.length, 301, 'one initial frame plus 30 frames per second');
    assert.equal(draws.at(-1), 10000, 'the render schedule does not drift');
    const shortestGap = Math.floor(hz / 30) * 1000 / hz;
    for (let i = 1; i < draws.length; i++) {
      assert.ok(draws[i] - draws[i - 1] >= shortestGap - 0.001);
    }
  });
}

test('the first frame draws immediately even when the renderer starts late', () => {
  const shouldDraw = createFramePacer();
  assert.equal(shouldDraw(947), true);
  assert.equal(shouldDraw(948), false);
  assert.equal(shouldDraw(947 + 1000 / 30), true);
});

test('hidden tabs do not render and resume with a single immediate frame', () => {
  const shouldDraw = createFramePacer();
  assert.equal(shouldDraw(0), true);
  for (const now of [16, 1000, 10000]) assert.equal(shouldDraw(now, true), false);
  assert.equal(shouldDraw(10001), true);
  assert.equal(shouldDraw(10002), false);
  assert.equal(shouldDraw(10017), false);
  assert.equal(shouldDraw(10001 + 1000 / 30), true);
});

test('a visible hitch drops missed frames instead of producing catch-up bursts', () => {
  const shouldDraw = createFramePacer();
  assert.equal(shouldDraw(0), true);
  assert.equal(shouldDraw(20), false);
  assert.equal(shouldDraw(5000), true);
  for (const now of [5000, 5001, 5016, 5030]) assert.equal(shouldDraw(now), false);
  assert.equal(shouldDraw(5000 + 1000 / 30), true);
});

for (const fps of [30,60,90]) {
  for (const hz of [120,144]) {
    test(`${fps} fps selectable budget remains on real time on a ${hz} Hz display`, () => {
      const draw=createFramePacer(fps);let count=0;
      for(let tick=0;tick<=hz*10;tick++)if(draw(tick*1000/hz))count++;
      assert.equal(count,fps*10+1);
    });
  }
}

test('changing FPS applies on the next vsync without catch-up or resetting an unchanged budget',()=>{
  const draw=createFramePacer(30);assert.equal(draw.getFps(),30);assert.equal(draw(0),true);
  draw.setFps(30);assert.equal(draw(16),false,'an unchanged setting does not admit an extra frame');
  draw.setFps(60);assert.equal(draw.getFps(),60);assert.equal(draw(20),true);assert.equal(draw(21),false);assert.equal(draw(20+1000/60),true);
  draw.setFps(90);assert.equal(draw(10000),true);assert.equal(draw(10001),false);assert.equal(draw(10000+1000/90),true);
  assert.equal(draw(20000,true),false);assert.equal(draw(30000),true);assert.equal(draw(30001),false);
});

test('a selected FPS remains a ceiling on a slower display and rejects invalid live updates',()=>{
  const draw=createFramePacer(90);let count=0;for(let tick=0;tick<=600;tick++)if(draw(tick*1000/60))count++;
  assert.equal(count,601);
  for(const value of [0,-1,NaN,Infinity])assert.throws(()=>draw.setFps(value),RangeError);
  assert.equal(draw.getFps(),90);assert.equal(draw(20000),true);
});
