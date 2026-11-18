import test from 'node:test';
import assert from 'node:assert/strict';
import {graphicsSettingsStatusText} from './public/office/graphics-settings-ui.js';

test('hardware fallback reports the effective AA rather than claiming the requested level applied', () => {
  for (const [requested,effective] of [[4,1],[4,2],[2,1],[2,4]]) {
    const text=graphicsSettingsStatusText({antialias:requested,fps:30,scale:1},{requestedAntialias:requested,effectiveAntialias:effective});
    assert.match(text,new RegExp('×'+effective));
    assert.match(text,/改用/);
  }
  assert.equal(graphicsSettingsStatusText({antialias:4,fps:30,scale:1},{requestedAntialias:4,effectiveAntialias:4}),'');
});

test('stale status never labels a newly selected preference as the previous effective result', () => {
  const settings={antialias:2,fps:90,scale:1.25};
  assert.equal(graphicsSettingsStatusText(settings,{requestedAntialias:4,effectiveAntialias:4}), '正在套用設定…');
  assert.equal(graphicsSettingsStatusText(settings,{requestedAntialias:2,requestedFps:30,effectiveAntialias:2}), '正在套用設定…');
  assert.equal(graphicsSettingsStatusText(settings,{requestedAntialias:2,requestedFps:90,requestedScale:1,effectiveAntialias:2}), '正在套用設定…');
});

test('renderer explanations and effective resolution limitations are preserved for the user', () => {
  const text=graphicsSettingsStatusText({antialias:4,fps:60,scale:1.25},{requestedAntialias:4,effectiveAntialias:1,effectiveScale:1,message:'此裝置不支援所選等級，已使用基本模式。'});
  assert.match(text,/基本模式/);
  assert.match(text,/精細度已依裝置調整/);
  assert.ok(!text.includes('×4'));
});
