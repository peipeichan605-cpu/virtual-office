import test from 'node:test';
import assert from 'node:assert/strict';
import {renderHistorySyncWarning} from './public/office/history-detail.js';

test('missing source prompts and partial transcripts have distinct visible warnings',()=>{
  const element={textContent:'',hidden:true};
  renderHistorySyncWarning(element,'cursor_cloud_prompt_unavailable');
  assert.equal(element.hidden,false);
  assert.match(element.textContent,/未提供.*原始任務文字/);
  assert.doesNotMatch(element.textContent,/已完成|完整對話/);
  renderHistorySyncWarning(element,'cursor_cloud_transcript_gap');
  assert.equal(element.hidden,false);
  assert.match(element.textContent,/部分公開對話未能恢復/);
  assert.match(element.textContent,/不能視為完整對話/);
});

test('changing to a complete record clears the previous warning, and unknown server content is never rendered',()=>{
  const element={textContent:'',hidden:true};
  for(const unknown of [null,undefined,'','<script>private response</script>','toString','__proto__',{},42]){
    renderHistorySyncWarning(element,'cursor_cloud_transcript_gap');
    renderHistorySyncWarning(element,unknown);
    assert.equal(element.textContent,'');
    assert.equal(element.hidden,true);
  }
});
