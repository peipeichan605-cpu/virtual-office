const STORAGE_KEY = 'office.simple-mode';

/** A local view preference; it never changes membership or agent permissions. */
export function initSimpleMode(onChange) {
  let enabled = false;
  try { enabled = localStorage.getItem(STORAGE_KEY) === '1'; } catch {}
  const button = document.createElement('button');
  button.id = 'office-mode-toggle';
  button.type = 'button';
  button.innerHTML = '<span aria-hidden="true">◫</span><span class="office-mode-label"></span>';
  const label = button.querySelector('.office-mode-label');
  document.body.append(button);
  function apply(save) {
    document.documentElement.classList.toggle('office-simple-mode', enabled);
    label.textContent = enabled ? '完整模式' : '簡易模式';
    button.title = enabled ? '顯示所有 OFFICE 功能' : '只顯示 Agent 與頭頂對話';
    button.setAttribute('aria-label', enabled ? '切換完整模式' : '切換簡易模式');
    if (save) { try { localStorage.setItem(STORAGE_KEY, enabled ? '1' : '0'); } catch {} }
    onChange(enabled);
  }
  button.addEventListener('click', () => { enabled = !enabled; apply(true); });
  apply(false);
}
