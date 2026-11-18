// Keep related launchers in one layout flow. Invitations can initialize before
// the 3D UI; the next launcher registration moves them into the ready top bar.
// Explicit refresh on view changes avoids a document-wide observer or polling.
const documents = new WeakMap();

export function refreshHeaderActions(doc = document) {
  const state = documents.get(doc);
  if (!state) return;
  const simple = doc.documentElement.classList.contains('office-simple-mode');
  const slot = doc.querySelector('#topbar .bhq-right');
  const focused = state.actions.has(doc.activeElement) ? doc.activeElement : null;
  let movedFocus = false;
  const tray = () => {
    if (!state.tray) {
      state.tray = doc.createElement('div');
      state.tray.id = 'office-header-actions';
      state.tray.setAttribute('role', 'group');
      state.tray.setAttribute('aria-label', '辦公室設定');
      doc.body.append(state.tray);
    }
    return state.tray;
  };
  for (const [element, options] of state.actions) {
    const target = simple && options.simple ? tray()
      : options.full === 'body' ? doc.body : slot || tray();
    if (element.parentElement !== target) {
      target.append(element);
      if (element === focused) movedFocus = true;
    }
  }
  if (state.tray && !state.tray.children.length) {
    state.tray.remove(); state.tray = null;
  }
  if (movedFocus) focused.focus({ preventScroll: true });
}

export function mountHeaderAction(element, { simple = false, full = 'header', doc = document } = {}) {
  let state = documents.get(doc);
  if (!state) { state = { actions: new Map(), tray: null }; documents.set(doc, state); }
  state.actions.set(element, { simple, full });
  refreshHeaderActions(doc);
  return () => {
    state.actions.delete(element); element.remove(); refreshHeaderActions(doc);
    if (!state.actions.size) documents.delete(doc);
  };
}
