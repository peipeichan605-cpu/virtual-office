// Browser-local display preferences. This panel has no Agent, command or
// receiver dependencies; the settings store owns validation and persistence.
import { DEFAULT_GRAPHICS_SETTINGS as DEFAULTS } from './graphics-settings.js';
import { mountHeaderAction } from './header-actions.js';
let mounted = null;

export function graphicsSettingsStatusText(settings, status = {}) {
  const pending = [['requestedAntialias','antialias'],['requestedFps','fps'],['requestedScale','scale']]
    .some(([requested,key]) => status[requested] != null && Number(status[requested]) !== Number(settings[key]));
  if (pending) return '正在套用設定…';
  const notes = [];
  if (status.message) notes.push(String(status.message));
  else if (status.effectiveAntialias != null && Number(status.effectiveAntialias) !== Number(settings.antialias)) {
    notes.push(`此裝置已改用 ×${Number(status.effectiveAntialias)} 抗鋸齒。`);
  }
  if (status.effectiveScale != null && Math.abs(Number(status.effectiveScale) - Number(settings.scale)) > 0.001) notes.push('畫面精細度已依裝置調整。');
  return notes.join(' ');
}

export function initGraphicsSettingsUI({ getSettings, updateSettings, subscribe, getStatus = () => ({}), subscribeStatus } = {}) {
  if (mounted) return mounted;
  if (typeof getSettings !== 'function' || typeof updateSettings !== 'function') throw new TypeError('Display settings store is required');
  const doc = document;
  const node = (tag, attrs = {}, text = '') => {
    const element = doc.createElement(tag);
    for (const [name, value] of Object.entries(attrs)) element.setAttribute(name, String(value));
    if (text) element.textContent = text;
    return element;
  };
  const launch = node('button', { type: 'button', id: 'office-graphics-settings-launch', class: 'office-graphics-settings-launch', 'aria-label': '畫面設定', 'aria-haspopup': 'dialog', 'aria-controls': 'office-graphics-settings-dialog', 'aria-expanded': 'false', title: '畫面設定' });
  const icon = node('span', { class: 'graphics-settings-icon', 'aria-hidden': 'true' }, '⚙');
  launch.append(icon, node('span', { class: 'graphics-settings-launch-text' }, '設定'));
  const dialog = node('dialog', { id: 'office-graphics-settings-dialog', class: 'office-graphics-settings-dialog', 'aria-labelledby': 'office-graphics-settings-heading' });
  const heading = node('div', { class: 'graphics-settings-heading' });
  heading.append(node('h2', { id: 'office-graphics-settings-heading' }, '畫面設定'));
  const closeButton = node('button', { type: 'button', class: 'graphics-settings-close', 'aria-label': '關閉畫面設定' }, '×');
  heading.append(closeButton);
  dialog.append(heading);
  const controls = {};
  const addSelect = (key, label, options, help) => {
    const row = node('div', { class: 'graphics-settings-row' });
    const id = `office-graphics-${key}`;
    const text = node('div', { class: 'graphics-settings-label' });
    text.append(node('label', { for: id }, label));
    if (help) text.append(node('small', { id: `${id}-help` }, help));
    const select = node('select', { id, name: key, ...(help ? { 'aria-describedby': `${id}-help` } : {}) });
    for (const [value, caption] of options) select.append(node('option', { value }, caption));
    controls[key] = select;
    row.append(text, select); dialog.append(row);
  };
  addSelect('antialias', '抗鋸齒', [[1, '×1 · 基本'], [2, '×2'], [4, '×4 · 平滑']], '讓角色及家具邊緣更平滑。');
  addSelect('fps', 'FPS 上限', [[30, '30 FPS'], [60, '60 FPS'], [90, '90 FPS']], '實際幀率受螢幕及裝置效能影響。');
  addSelect('scale', '畫面精細度', [[0.75, '節省 · 75%'], [1, '標準 · 100%'], [1.25, '清晰 · 125%']]);
  const idleRow = node('div', { class: 'graphics-settings-row' });
  const idleLabel = node('div', { class: 'graphics-settings-label' });
  idleLabel.append(node('label', { for: 'office-graphics-idle' }, '自動閒置節能'), node('small', { id: 'office-graphics-idle-help' }, '沒有工作或操作時暫停小動畫。'));
  controls.idle = node('input', { type: 'checkbox', id: 'office-graphics-idle', name: 'idle', 'aria-describedby': 'office-graphics-idle-help' });
  const idleHit = node('label', { class: 'graphics-settings-toggle-hit', for: 'office-graphics-idle' });
  idleHit.append(controls.idle);
  idleRow.append(idleLabel, idleHit); dialog.append(idleRow);
  const statusLine = node('p', { class: 'graphics-settings-status', role: 'status', 'aria-live': 'polite', hidden: '' });
  const footer = node('div', { class: 'graphics-settings-footer' });
  const preferenceNote = node('small', {}, '即時生效，並記住這個瀏覽器的選擇。');
  const reset = node('button', { type: 'button', class: 'graphics-settings-reset' }, '重設為預設');
  footer.append(preferenceNote, reset); dialog.append(statusLine, footer);
  let status = getStatus() || {};
  let settings = { ...DEFAULTS, ...getSettings() };
  let disposed = false;
  const synchronize = (value = getSettings()) => {
    settings = { ...DEFAULTS, ...value };
    for (const key of ['antialias', 'fps', 'scale']) controls[key].value = String(settings[key]);
    controls.idle.checked = !!settings.idle;
    const message = graphicsSettingsStatusText(settings, status);
    statusLine.textContent = message; statusLine.hidden = !message;
    preferenceNote.textContent = status.persistenceMessage || (status.storageAvailable === false || status.persisted === false
      ? '此瀏覽器未能儲存設定，今次開啟仍可使用。'
      : '即時生效，並記住這個瀏覽器的選擇。');
  };
  const apply = async patch => {
    try {
      await updateSettings(patch);
      if (!disposed) synchronize();
    } catch {
      if (disposed) return;
      synchronize(); statusLine.hidden = false; statusLine.textContent = '設定未能套用，請再試一次。';
    }
  };
  for (const key of ['antialias', 'fps', 'scale']) controls[key].addEventListener('change', () => { void apply({ [key]: Number(controls[key].value) }); });
  controls.idle.addEventListener('change', () => { void apply({ idle: controls.idle.checked }); });
  reset.addEventListener('click', () => { void apply({ ...DEFAULTS }); });
  const close = () => { if (dialog.open) dialog.close(); };
  const open = () => {
    if (disposed || dialog.open) return;
    synchronize(); dialog.showModal(); launch.setAttribute('aria-expanded', 'true'); controls.antialias.focus();
  };
  launch.addEventListener('click', open);
  closeButton.addEventListener('click', close);
  dialog.addEventListener('close', () => { launch.setAttribute('aria-expanded', 'false'); if (launch.isConnected) launch.focus({ preventScroll: true }); });
  dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
  dialog.addEventListener('keydown', event => {
    // Preserve any selected Agent when dismissing settings; app.js also uses
    // Escape to clear selection outside this dialog.
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); }
  });
  dialog.addEventListener('click', event => {
    if (event.target !== dialog) return;
    const bounds = dialog.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) close();
  });
  doc.body.append(dialog);
  const unmountLaunch = mountHeaderAction(launch, { simple: true, doc });
  doc.documentElement.classList.add('office-graphics-ready');
  synchronize();
  const unsubscribe = typeof subscribe === 'function' ? subscribe(synchronize) : null;
  const unsubscribeStatus = typeof subscribeStatus === 'function' ? subscribeStatus(value => { status = value || {}; synchronize(); }) : null;
  const api = {
    open, close,
    setStatus(value) { status = value || {}; synchronize(); },
    dispose() {
      disposed = true; close();
      if (typeof unsubscribe === 'function') unsubscribe();
      if (typeof unsubscribeStatus === 'function') unsubscribeStatus();
      unmountLaunch(); dialog.remove(); doc.documentElement.classList.remove('office-graphics-ready'); mounted = null;
    },
  };
  mounted = api;
  return api;
}
