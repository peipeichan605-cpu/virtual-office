// Mobile pages use their own revocable cookie. They never receive the owner key.
export const isMobileAccess = () => globalThis.document?.querySelector?.('meta[name="office-access"]')?.content === 'mobile'
  || /^\/mobile(?:\/|$)/.test(globalThis.location?.pathname || '');

const reads = new Set(['work','messages','history','run','commands']);
const writes = new Set(['commands','commands/cancel']);
let session = null, pendingSession = null, pendingRenewal = null, locked = false, sessionGeneration = 0;
const sessionEpochs = new WeakMap();

function sessionChanged() {
  return Object.assign(new Error('mobile_session_changed'), {code:'mobile_session_changed'});
}

export function mobileRoute(path, method = 'GET') {
  if (typeof path !== 'string' || !path.startsWith('/api/office/') || path.includes('#')) throw new Error('mobile_endpoint_forbidden');
  const [route] = path.slice('/api/office/'.length).split('?');
  if (!(method === 'GET' ? reads : method === 'POST' ? writes : new Set()).has(route)) throw new Error('mobile_endpoint_forbidden');
  return '/api/mobile/' + path.slice('/api/office/'.length);
}

export function clearMobileSession() {
  sessionGeneration++;
  session = null; pendingSession = null; pendingRenewal = null;
}

export function isCurrentMobileSession(value) {
  return Boolean(value && value === session && !locked);
}

// A short-session renewal may replace the cached object while the 3D module
// loads. A genuine logout/reset changes the epoch and must still cancel load.
export function currentMobileSessionFor(previous) {
  return !locked && session && previous && sessionEpochs.get(previous) === sessionGeneration ? session : null;
}

export function lockMobileAccess() {
  clearMobileSession();
  if (locked) return;
  locked = true;
  if (typeof globalThis.dispatchEvent === 'function' && typeof globalThis.CustomEvent === 'function') {
    globalThis.dispatchEvent(new CustomEvent('office:mobile-locked'));
  }
  const doc = globalThis.document;
  if (doc?.body && typeof doc.createElement === 'function') {
    const message = doc.createElement('p');message.className='mobile-lock-notice';message.textContent='手機登入已失效，請重新配對。';
    doc.body.replaceChildren(message);doc.title='重新配對 · Virtual Office';
  }
  if (globalThis.location?.pathname !== '/mobile/pair') globalThis.location?.replace?.('/mobile/pair');
}

async function decode(response, current = () => true) {
  let data;try { data = await response.json(); } catch { data = {}; }
  // A response from before logout/cache reset must not restore old data or
  // invalidate a newer login, even if JSON parsing finished out of order.
  if (!current()) throw sessionChanged();
  if (!response.ok) {
    const code = typeof data.error === 'string' && /^[a-z_]{1,100}$/.test(data.error) ? data.error : 'mobile_unavailable';
    if ((response.status === 401 && code !== 'mobile_session_renewal_required') || (response.status === 403 && /(?:revoked|expired|session_required|identity_mismatch)/.test(code))) lockMobileAccess();
    const error = new Error(code);error.code=code;error.status=response.status;throw error;
  }
  return data;
}

function acceptSession(data, generation) {
  if (generation !== sessionGeneration) throw sessionChanged();
  if (!data || data.authenticated !== true || typeof data.csrf_token !== 'string' || !data.csrf_token) {
    lockMobileAccess();throw Object.assign(new Error('mobile_session_required'), {code:'mobile_session_required',status:401});
  }
  session = data;sessionEpochs.set(data,generation);locked=false;return data;
}

async function renewMobileSession(generation) {
  if (generation !== sessionGeneration) throw sessionChanged();
  if (pendingRenewal) return pendingRenewal;
  const request = (async () => {
    let response;
    try { response = await fetch('/api/mobile/renew', {method:'POST',cache:'no-store',credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-Mobile-Renew':'1'},body:'{}',signal:AbortSignal.timeout(10000)}); }
    catch(error) { if(generation!==sessionGeneration)throw sessionChanged();throw error; }
    const data = await decode(response, () => generation === sessionGeneration);
    return acceptSession(data,generation);
  })();
  pendingRenewal=request;
  try { return await request; } finally { if(pendingRenewal===request)pendingRenewal=null; }
}

/** User-initiated only. The server requires current access and CSRF. */
export async function rememberMobileDevice(enabled) {
  if(typeof enabled!=='boolean')throw new Error('invalid_mobile_remember_choice');
  const current=await ensureMobileSession({refresh:true}),generation=sessionGeneration;
  const response=await fetch('/api/mobile/remember',{method:'POST',cache:'no-store',credentials:'same-origin',
    headers:{'Content-Type':'application/json','X-Mobile-CSRF':current.csrf_token},
    body:JSON.stringify({remember_device:enabled}),signal:AbortSignal.timeout(10000)});
  const data=await decode(response,()=>generation===sessionGeneration);
  if(generation!==sessionGeneration)throw sessionChanged();
  clearMobileSession();
  return acceptSession(data,sessionGeneration);
}

export async function logoutMobileDevice() {
  const current=await ensureMobileSession({refresh:true}),generation=sessionGeneration;
  const headers={'Content-Type':'application/json','X-Mobile-CSRF':current.csrf_token};
  const response=await fetch('/api/mobile/logout',{method:'POST',cache:'no-store',credentials:'same-origin',
    headers,body:'{}',signal:AbortSignal.timeout(10000)});
  const data=await decode(response,()=>generation===sessionGeneration);
  if(generation!==sessionGeneration)throw sessionChanged();
  lockMobileAccess();return data;
}

export async function ensureMobileSession({refresh = false} = {}) {
  if (session && !refresh) return session;
  if (pendingSession) return pendingSession;
  const generation = sessionGeneration;
  const request = (async () => {
    let response;
    try { response = await fetch('/api/mobile/session', {cache:'no-store',credentials:'same-origin',signal:AbortSignal.timeout(5000)}); }
    catch (error) { if (generation !== sessionGeneration) throw sessionChanged(); throw error; }
    try {
      return acceptSession(await decode(response, () => generation === sessionGeneration),generation);
    } catch(error) {
      if(error.code==='mobile_session_renewal_required')return renewMobileSession(generation);
      throw error;
    }
  })();
  pendingSession = request;
  try { return await request; } finally { if (pendingSession === request) pendingSession = null; }
}

/** Retry only session reads while starting a phone page. Mutations are never
 * retried here. After the initial backoff, probe at most once per 30 seconds:
 * a private route can recover without a browser online event. Hidden pages
 * have no retry timer and terminal auth failures stop entirely.
 */
export function waitForMobileSession({onStatus = () => {}, signal,
  document: doc = globalThis.document, events = globalThis,
  setTimer = (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimer = id => globalThis.clearTimeout(id)} = {}) {
  return new Promise((resolve, reject) => {
    let stopped = false, running = false, timer = null, attempts = 0, resumePending = false, suspended = false;
    const hidden = () => suspended || doc?.visibilityState === 'hidden';
    const notify = state => { try { onStatus(state); } catch {} };
    const clear = () => { if (timer !== null) clearTimer(timer); timer = null; };
    const finish = (error, data) => {
      if (stopped) return;
      stopped = true; clear();
      events?.removeEventListener?.('online', resume);
      events?.removeEventListener?.('pagehide', pagehide);
      events?.removeEventListener?.('pageshow', pageshow);
      events?.removeEventListener?.('office:mobile-locked', authFailed);
      doc?.removeEventListener?.('visibilitychange', visibility);
      signal?.removeEventListener?.('abort', cancel);
      if (error) reject(error); else resolve(data);
    };
    const cancel = () => finish(sessionChanged());
    const authFailed = () => finish(Object.assign(new Error('mobile_auth_required'), {code:'mobile_auth_required',status:401}));
    const transient = error => (!error?.status && ['TypeError','AbortError','TimeoutError'].includes(error?.name))
      || error?.status === 408 || error?.status === 429 || error?.status >= 500;
    const attempt = async () => {
      if (stopped || running) return;
      clear();
      if (hidden()) { notify('paused'); return; }
      running = true; attempts = Math.min(attempts + 1, 5); notify('connecting');
      const generation = sessionGeneration;
      try {
        const data = await ensureMobileSession();
        if (generation !== sessionGeneration || !isCurrentMobileSession(data)) throw sessionChanged();
        if (!stopped) {
          if (hidden()) notify('paused');
          else {
            notify('ready');
            if (generation !== sessionGeneration || !isCurrentMobileSession(data)) throw sessionChanged();
            finish(null, data);
          }
        }
      } catch (error) {
        if (stopped) return;
        if (!transient(error)) { notify('denied'); finish(error); return; }
        if (hidden()) notify('paused');
        else {
          notify('retrying');
          const delay = error.status === 429 || attempts >= 5 ? 30000 : 1000 * 2 ** (attempts - 1);
          timer = setTimer(attempt, delay);
        }
      } finally {
        running = false;
        if (resumePending && !stopped) { resumePending = false; resume(); }
      }
    };
    function resume() {
      if (stopped || hidden()) return;
      clear(); attempts = 0;
      if (running) resumePending = true; else attempt();
    }
    function visibility() { if (hidden()) { clear(); notify('paused'); } else resume(); }
    function pagehide(event) {
      if(event?.persisted){suspended=true;clear();notify('paused');}
      else cancel();
    }
    function pageshow() { if(suspended){suspended=false;resume();} }
    events?.addEventListener?.('online', resume);
    events?.addEventListener?.('pagehide', pagehide);
    events?.addEventListener?.('pageshow', pageshow);
    events?.addEventListener?.('office:mobile-locked', authFailed);
    doc?.addEventListener?.('visibilitychange', visibility);
    signal?.addEventListener?.('abort', cancel, {once:true});
    if (signal?.aborted) cancel(); else attempt();
  });
}

export async function mobileRequest(path, {method = 'GET',body} = {}) {
  const allowed = method === 'GET' ? /^\/api\/mobile\/(?:work|messages|history|run|commands|health)(?:\?|$)/
    : /^\/api\/mobile\/(?:commands|commands\/cancel)$/;
  if (!allowed.test(path) || (method !== 'GET' && method !== 'POST')) throw new Error('mobile_endpoint_forbidden');
  const headers = {}, generation = sessionGeneration;
  if (method === 'POST') {
    const current = await ensureMobileSession({refresh:true});
    headers['X-Mobile-CSRF'] = current.csrf_token;headers['Content-Type']='application/json';
  }
  if (generation !== sessionGeneration) throw sessionChanged();
  const send=()=>fetch(path,{method,cache:'no-store',credentials:'same-origin',headers,
    ...(method === 'POST' ? {body:JSON.stringify(body)} : {}),signal:AbortSignal.timeout(method==='POST'?10000:5000)});
  let response=await send(),data;
  try { data=await decode(response,()=>generation===sessionGeneration); }
  catch(error) {
    if(error.code!=='mobile_session_renewal_required')throw error;
    await renewMobileSession(generation);
    if(generation!==sessionGeneration)throw sessionChanged();
    // A command request is never replayed, even when its session was stale.
    if(method!=='GET')throw Object.assign(new Error('登入已續期，請再按一次傳送。'),{code:'mobile_session_renewed',status:409});
    data=await decode(await send(),()=>generation===sessionGeneration);
  }
  if (generation !== sessionGeneration) throw sessionChanged();
  return data;
}
