// Browser-local display preferences. The installation path scopes the key;
// private channel hashes, Agent identities and command credentials never enter it.
export const DEFAULT_GRAPHICS_SETTINGS = Object.freeze({antialias:4,fps:30,scale:1,idle:true});
const CHOICES = {antialias:[1,2,4],fps:[30,60,90],scale:[0.75,1,1.25]};

export function graphicsStorageKey(href) {
  let scope = '/';
  try { scope = new URL('./',href).pathname; } catch {}
  return `virtual-office:graphics:v1:${scope}`;
}

export function sanitizeGraphicsSettings(raw, base = DEFAULT_GRAPHICS_SETTINGS) {
  const next = {...base};
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return next;
  for (const [key,choices] of Object.entries(CHOICES)) {
    if (!['number','string'].includes(typeof raw[key])) continue;
    const value = Number(raw[key]);
    if (choices.includes(value)) next[key] = value;
  }
  if (typeof raw.idle === 'boolean') next.idle = raw.idle;
  return next;
}
const same = (a,b) => Object.keys(DEFAULT_GRAPHICS_SETTINGS).every(key => a[key] === b[key]);
const parse = value => {
  try { const record=JSON.parse(value); return record?.version === 1 ? sanitizeGraphicsSettings(record.settings) : {...DEFAULT_GRAPHICS_SETTINGS}; }
  catch { return {...DEFAULT_GRAPHICS_SETTINGS}; }
};
const copyStatus = status => ({...status,...(status.availableAntialias ? {availableAntialias:[...status.availableAntialias]} : {})});

export function createGraphicsSettingsStore({storage = null,href = 'http://localhost/',eventTarget = null,onListenerError} = {}) {
  const key=graphicsStorageKey(href),listeners=new Set(),statusListeners=new Set();
  let settings={...DEFAULT_GRAPHICS_SETTINGS};
  let status={requestedAntialias:settings.antialias,requestedFps:settings.fps,requestedScale:settings.scale,persistenceMessage:storage?'':'瀏覽器無法使用本機儲存，本次仍可調整。',storageAvailable:!!storage};
  try { if (storage) settings=parse(storage.getItem(key)); } catch { status.persistenceMessage='瀏覽器未能讀取已儲存設定，本次仍可調整。'; }
  status={...status,requestedAntialias:settings.antialias,requestedFps:settings.fps,requestedScale:settings.scale};
  const notify = (set,value) => {
    for (const listener of [...set]) { try { listener(value()); } catch(error) { onListenerError?.(error); } }
  };
  const getSettings = () => ({...settings});
  const getStatus = () => copyStatus(status);
  const subscribeTo = (set,getter,listener,{emitInitial = false} = {}) => {
    if (typeof listener !== 'function') throw new TypeError('listener must be a function');
    set.add(listener); if (emitInitial) listener(getter()); return () => set.delete(listener);
  };
  const publishStatus = patch => {
    if (!patch || typeof patch !== 'object') return getStatus();
    const next=copyStatus({...status,...patch});
    if (JSON.stringify(next) !== JSON.stringify(status)) { status=next;notify(statusListeners,getStatus); }
    return getStatus();
  };
  const apply = (raw,persist) => {
    const next=sanitizeGraphicsSettings(raw,settings);
    if (same(settings,next)) return getSettings();
    settings=next;
    const persistence={persistenceMessage:status.persistenceMessage};
    if (persist) {
      try {
        if (!storage) throw new Error('storage unavailable');
        storage.setItem(key,JSON.stringify({version:1,settings}));persistence.persistenceMessage='';
      } catch { persistence.persistenceMessage='瀏覽器未能儲存設定，本次仍然生效。'; }
    }
    publishStatus({...persistence,requestedAntialias:settings.antialias,requestedFps:settings.fps,requestedScale:settings.scale});
    notify(listeners,getSettings);
    return getSettings();
  };
  const onStorage = event => {
    if (event.key !== key || (event.storageArea && storage && event.storageArea !== storage)) return;
    apply(parse(event.newValue),false);
  };
  eventTarget?.addEventListener('storage',onStorage);
  return {
    key,getSettings,getStatus,update:patch=>apply(patch,true),publishStatus,
    subscribe:(listener,options)=>subscribeTo(listeners,getSettings,listener,options),
    subscribeStatus:(listener,options)=>subscribeTo(statusListeners,getStatus,listener,options),
    dispose(){eventTarget?.removeEventListener('storage',onStorage);listeners.clear();statusListeners.clear();},
  };
}

let browserStorage=null;
try { if (typeof localStorage !== 'undefined') browserStorage=localStorage; } catch {}
const store=createGraphicsSettingsStore({storage:browserStorage,href:typeof location !== 'undefined'?location.href:'http://localhost/',eventTarget:typeof window !== 'undefined'?window:null});
export const getGraphicsSettings = () => store.getSettings();
export const updateGraphicsSettings = patch => store.update(patch);
export const subscribeGraphicsSettings = (listener,options) => store.subscribe(listener,options);
export const getGraphicsStatus = () => store.getStatus();
export const publishGraphicsStatus = patch => store.publishStatus(patch);
export const subscribeGraphicsStatus = (listener,options) => store.subscribeStatus(listener,options);
