const warnings={
  cursor_cloud_prompt_unavailable:'平台未提供這次工作的原始任務文字；以下僅顯示已取得的內容。',
  cursor_cloud_transcript_gap:'部分公開對話未能恢復；以下保留已取得的內容，不能視為完整對話。',
};

/** Known public warning codes only; never render a server error or source text. */
export function renderHistorySyncWarning(element,code){
  const message=typeof code==='string'&&Object.hasOwn(warnings,code)?warnings[code]:'';
  element.textContent=message;
  element.hidden=!message;
}
