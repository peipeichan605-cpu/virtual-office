"""Optional OFFICE-owned Codex threads over a private stdio app-server.

These threads are distinct from existing Desktop conversations. The durable
native thread is created only for the first instruction. Uncertain native
mutations are fenced and never automatically retried or replaced with a new
thread. Only typed public assistant messages become OFFICE conversation text.
"""
import hashlib
from contextlib import contextmanager
import json
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from command_adapters import DeliveryError
from codex_rpc import (PROBE_TIMEOUT, RpcError, StdioRpc, child_environment,
    deadline_lock, process_creation_flags, remaining_timeout, resolve_binary, runtime_info)
from history import redact, timestamp
from security_keys import security_secrets

SESSION = re.compile(r'bg-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z')
MARKER = re.compile(r'^\[OFFICE:([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\]\n')
MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')
SAFE_PHASES = {'reserved', 'ready'}
DISABLED_FEATURES = ('apps', 'plugins', 'remote_plugin', 'multi_agent', 'hooks',
    'browser_use', 'browser_use_external', 'computer_use', 'in_app_browser',
    'image_generation', 'goals')
BASE_OVERRIDES = ('sandbox_mode="read-only"', 'approval_policy="on-request"',
    'approvals_reviewer="user"', 'web_search="disabled"') + tuple(
        'features.' + feature + '=false' for feature in DISABLED_FEATURES)
_PROBE_LOCK = threading.Lock()
_PROBES = {}
FAILURE_TEXT = {
    'codex_model_unavailable': '目前 Codex 執行版本或帳戶未能使用所選模型。可檢查 Codex Desktop 版本，或在這隻 Agent 明確更改模型；OFFICE 未有自動更換模型或重送指示。',
    'codex_authentication_required': 'Codex 登入或授權未獲接受。請檢查本機 Codex Desktop 或所選執行版本的登入狀態；這次指示未完成，OFFICE 未有自動重送。',
    'codex_quota_exceeded': 'Codex 使用額度或工作預算已達上限。這次指示未完成；OFFICE 未有更改額度設定或自動重送。',
    'codex_rate_limited': 'Codex 暫時限制請求頻率。這次指示未完成；OFFICE 未有自動重送。',
    'codex_context_limit': '這個 Codex 對話已超出可處理的內容長度。這次指示未完成；OFFICE 未有自動建立新對話或重送。',
    'codex_provider_unavailable': 'Codex 服務或連線暫時未能完成這次指示。OFFICE 已保留失敗紀錄，未有自動重送。',
    'codex_turn_failed': 'Codex 已回報這次指示失敗。OFFICE 已保留紀錄，未有自動重送。',
}


def classify_turn_error(error):
    """Classify only whitelisted structured codes; never publish raw errors.

    Some app-server versions put a provider JSON error inside `message` while
    codexErrorInfo merely says `other`. Parsing that bounded JSON preserves the
    useful category without leaking the provider's message, IDs or credentials.
    """
    codes, statuses = set(), set()

    def inspect(value, depth=0):
        if depth > 4 or not isinstance(value, dict):
            return
        for key in ('code', 'type', 'codexErrorInfo', 'codex_error_info'):
            code = value.get(key)
            if isinstance(code, str) and len(code) < 100:
                codes.add(code)
            elif isinstance(code, dict):
                for name, inner in code.items():
                    if isinstance(name, str):
                        codes.add(name)
                    inspect(inner, depth + 1)
        for key in ('status', 'httpStatusCode'):
            status = value.get(key)
            if isinstance(status, int) and not isinstance(status, bool):
                statuses.add(status)
        inspect(value.get('error'), depth + 1)
        message = value.get('message')
        if isinstance(message, str) and len(message) <= 32768 and message.lstrip().startswith('{'):
            try:
                inspect(json.loads(message), depth + 1)
            except ValueError:
                pass

    inspect(error)
    if codes & {'model_not_found', 'model_not_supported', 'unsupported_model', 'model_unavailable', 'not_supported'}:
        return 'codex_model_unavailable'
    if codes & {'unauthorized', 'authentication_error', 'invalid_api_key', 'invalid_token', 'token_expired'} or statuses & {401, 403}:
        return 'codex_authentication_required'
    if codes & {'insufficient_quota', 'quota_exceeded', 'usageLimitExceeded', 'sessionBudgetExceeded'}:
        return 'codex_quota_exceeded'
    if codes & {'rate_limit_exceeded', 'rate_limit_error', 'too_many_requests'} or 429 in statuses:
        return 'codex_rate_limited'
    if codes & {'contextWindowExceeded', 'context_length_exceeded', 'context_window_exceeded'}:
        return 'codex_context_limit'
    if codes & {'serverOverloaded', 'internalServerError', 'server_error', 'httpConnectionFailed',
                'responseStreamConnectionFailed', 'responseStreamDisconnected', 'responseTooManyFailedAttempts'} or statuses & {500, 502, 503, 504}:
        return 'codex_provider_unavailable'
    return 'codex_turn_failed'


def durable_state(path, value):
    """Atomic private JSON plus fsync; never write prompts or auth material."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('codex_lifecycle_symlink')
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(value, output, ensure_ascii=False, separators=(',', ':'))
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(str(temporary), str(path))
        if os.name == 'posix':
            directory = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _metadata(client, cwd=None):
    account = client.call('account/read', {'refreshToken': False})
    config = client.call('config/read', {'includeLayers': False, 'cwd': cwd}).get('config', {})
    return account.get('account') is not None, config


def _model_catalog(client):
    """A catalog is a picker aid, not proof that inference is authorized."""
    result, seen, cursor, cursors = [], set(), None, set()
    for _ in range(3):
        params = {'limit': 100, 'includeHidden': False}
        if cursor:
            params['cursor'] = cursor
        response = client.call('model/list', params, timeout=10)
        if not isinstance(response, dict) or not isinstance(response.get('data'), list):
            raise RpcError('codex_model_catalog_unavailable')
        for item in response['data'][:100]:
            if not isinstance(item, dict) or item.get('hidden') is True:
                continue
            ident = item.get('model') or item.get('id')
            if not isinstance(ident, str) or not MODEL.fullmatch(ident) or ident in seen:
                continue
            raw_name = item.get('displayName')
            name = ' '.join(raw_name.split())[:80] if isinstance(raw_name, str) else ident
            result.append({'id': ident, 'name': name or ident,
                'is_default': item.get('isDefault') is True, 'support': 'catalog_listed'})
            seen.add(ident)
            if len(result) >= 100:
                return result
        cursor = response.get('nextCursor')
        if not isinstance(cursor, str) or not cursor or len(cursor) > 4096 or cursor in cursors:
            break
        cursors.add(cursor)
    return result


def _copy_probe(value):
    copied = dict(value)
    copied['models'] = [dict(model) for model in value.get('models', [])]
    return copied


def probe(settings=None, *, deadline=None):
    """Read-only CLI/login check, cached; never creates or resumes a thread."""
    settings = settings or {}
    deadline = time.monotonic() + PROBE_TIMEOUT if deadline is None else deadline
    binary, metadata = None, {}
    result = {'available': False, 'reason': 'codex_binary_unavailable',
              'version': None, 'logged_in': False, 'model': None,
              'models': [], 'model_catalog_status': 'unavailable',
              'configured_model_listed': None, 'runtime_source': None}
    try:
        binary = resolve_binary(settings, deadline=deadline)
        metadata = runtime_info(binary)
        key = metadata['identity']
        result['runtime_source'] = metadata.get('source')
        with deadline_lock(_PROBE_LOCK, deadline):
            cached = _PROBES.get(key)
            if cached and time.monotonic() - cached[0] < 30:
                return _copy_probe(cached[1])
            client = None
            try:
                version = subprocess.run([binary, '--version'], stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=child_environment(),
                    text=True, timeout=remaining_timeout(deadline, 5),
                    creationflags=process_creation_flags())
                match = re.fullmatch(r'codex-cli\s+(\d+\.\d+\.\d+[^\s]*)\s*', version.stdout)
                if version.returncode or not match:
                    raise RpcError('codex_version_unavailable')
                result['version'] = match.group(1)
                client = StdioRpc(binary, os.getcwd(), BASE_OVERRIDES, deadline=deadline)
                client.initialize()
                logged_in, config = _metadata(client)
                result.update(available=logged_in, logged_in=logged_in,
                    reason=None if logged_in else 'codex_login_required',
                    model=config.get('model'))
                if logged_in:
                    try:
                        result['models'] = _model_catalog(client)
                        result['model_catalog_status'] = 'available'
                        result['configured_model_listed'] = any(
                            model['id'] == result['model'] for model in result['models']) if result['model'] else None
                    except RpcError:
                        # An optional catalog cannot consume a fresh timeout
                        # per page or invalidate an already verified login.
                        pass
            except (RpcError, OSError, subprocess.TimeoutExpired) as error:
                result['reason'] = getattr(error, 'code', 'codex_probe_unavailable')
            finally:
                if client:
                    client.close()
            _PROBES[key] = (time.monotonic(), result)
    except (RpcError, OSError, subprocess.TimeoutExpired) as error:
        result.update(available=False, reason=getattr(error, 'code', 'codex_probe_unavailable'))
    return _copy_probe(result)


def _disabled_integrations(config):
    """Explicit names matter: an empty table does not clear merged TOML."""
    result = []
    for key in ('mcp_servers', 'plugins'):
        names = config.get(key) or {}
        if names:
            # The CLI override parser treats dotted quoted keys literally.
            # A nested TOML map safely handles names containing dots/@/spaces.
            result.append(key + '={' + ','.join(json.dumps(name) + '={enabled=false}' for name in names) + '}')
    return tuple(result)


def _integrations_disabled(config):
    return (config.get('web_search') == 'disabled'
        and all((config.get('features') or {}).get(name) is False for name in DISABLED_FEATURES)
        and all(isinstance(value, dict) and value.get('enabled') is False
               for key in ('mcp_servers', 'plugins')
               for value in (config.get(key) or {}).values()))


class _Lease:
    """An OS lease prevents two OFFICE processes mutating one native thread."""
    def __init__(self, path):
        path = Path(str(path) + '.lock')
        if path.is_symlink():
            raise DeliveryError('codex_lifecycle_symlink')
        self.file = os.fdopen(os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600), 'a+b')
        try:
            if os.name == 'posix':
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt
                self.file.seek(0)
                self.file.write(b'0')
                self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        except (OSError, ImportError):
            self.file.close()
            raise DeliveryError('codex_session_locked')

    def close(self):
        if not self.file.closed:
            self.file.close()


class CodexBackgroundAdapter:
    mode = 'codex_background'
    serial = True
    same_session = True
    MAX_PROCESSES = 4

    def __init__(self, config, emit=None, rpc_factory=None, probe_fn=None):
        self.config, self.emit = config, emit or (lambda *_: None)
        self.settings = config.get('command_adapters', {}).get(self.mode, {})
        self.sessions = dict(self.settings.get('sessions', {}))
        self.rpc_factory = rpc_factory or StdioRpc
        self.probe_fn = probe_fn or probe
        self.lock = threading.RLock()
        self.workers = {}
        self.closed = False
        self.reason = None
        self.event_queue = queue.Queue(maxsize=4096)
        self.event_thread = None

    def _deliver_events(self):
        # Never call Commands while an adapter/session lock is held: snapshots
        # take those locks in the other order. FIFO preserves receipt ordering.
        while not self.closed:
            packet = self.event_queue.get()
            if packet is None:
                return
            worker, event = packet
            with self.lock:
                current = not self.closed and self.workers.get(worker.sid) is worker and not worker.closed
            try:
                if current and not worker.projection_failed:
                    self.emit('codex:' + worker.sid, event)
            except Exception:
                worker.projection_failed = True
                with worker.lock:
                    try:
                        state = json.loads(worker.path.read_text(encoding='utf-8'))
                        worker._save(state, phase='unknown', detail_code='codex_public_projection_failed')
                    except (OSError, ValueError, TypeError):
                        pass
            finally:
                with worker.event_lock:
                    worker.pending_events -= 1

    def _read(self, sid):
        if not isinstance(sid, str) or not SESSION.fullmatch(sid) or sid not in self.sessions:
            raise DeliveryError('codex_background_session_missing')
        settings = self.sessions[sid]
        cwd, location = settings.get('cwd'), settings.get('lifecycle_file')
        if (not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir()
                or not isinstance(location, str) or not Path(location).is_absolute()
                or Path(location).is_symlink() or Path(location).parent.is_symlink()):
            raise DeliveryError('codex_background_config_invalid')
        if settings.get('sandbox') != 'read-only' or settings.get('approval_policy') != 'on-request':
            raise DeliveryError('codex_background_policy_invalid')
        model = settings.get('model')
        if model is not None and (not isinstance(model, str) or not MODEL.fullmatch(model)):
            raise DeliveryError('codex_background_model_invalid')
        try:
            state = json.loads(Path(location).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise DeliveryError('codex_background_lifecycle_missing')
        if (not isinstance(state, dict) or state.get('schema') != 1
                or state.get('session_id') != sid or state.get('cwd') != cwd):
            raise DeliveryError('codex_background_lifecycle_invalid')
        return settings, state

    def available(self, sid):
        with self.lock:
            try:
                _, state = self._read(sid)
                if self.closed:
                    raise DeliveryError('codex_background_closed')
                worker = self.workers.get(sid)
                if worker and worker.projection_failed:
                    raise DeliveryError('codex_background_recovery_required')
                if state.get('phase') not in SAFE_PHASES and not (worker and (worker.active or worker.dispatch_reserved)):
                    raise DeliveryError('codex_background_recovery_required')
                if worker and worker.client and not worker.client.closed.is_set():
                    self.reason = None
                    return True
                status = self.probe_fn(self.settings)
                if not status.get('available'):
                    raise DeliveryError(status.get('reason') or 'codex_background_unavailable')
                self.reason = None
                return True
            except DeliveryError as error:
                self.reason = error.code
                return False

    def native_verified(self, sid):
        try:
            _, state = self._read(sid)
            return bool(state.get('native_target_verified') and state.get('native_thread_id'))
        except DeliveryError:
            return False

    def model_change_reason(self, sid):
        with self.lock:
            try:
                _, state = self._read(sid)
            except DeliveryError as error:
                return error.code
            worker = self.workers.get(sid)
            if state.get('phase') not in SAFE_PHASES or (worker and worker.projection_failed):
                return 'codex_background_recovery_required'
            if worker and (worker.active or worker.dispatch_reserved or worker.pending_events):
                return 'codex_model_change_busy'
            return None

    @contextmanager
    def model_change_guard(self, sid):
        """Reserve an idle native thread while an owner changes its model.

        Closing this adapter's idle process releases its OS lease; acquiring
        it again rejects a second Office which already owns that thread. No
        native conversation is mutated and no pending command is replayed.
        """
        with self.lock:
            reason = self.model_change_reason(sid)
            if reason:
                raise DeliveryError(reason)
            settings, _ = self._read(sid)
            worker = self.workers.pop(sid, None)
        if worker:
            worker.close()
        lease = _Lease(settings['lifecycle_file'])
        try:
            yield
        finally:
            lease.close()

    def can_dispatch(self, sid):
        with self.lock:
            worker = self.workers.get(sid)
            return self.available(sid) and not (worker and (worker.active or worker.dispatch_reserved or worker.pending_events))

    def submit(self, sid, marked_text):
        match = MARKER.match(marked_text) if isinstance(marked_text, str) else None
        if not match or not marked_text[match.end():].strip():
            raise DeliveryError('codex_command_marker_invalid')
        with self.lock:
            if not self.can_dispatch(sid):
                raise DeliveryError(self.reason or 'codex_background_busy')
            if self.event_thread is None:
                self.event_thread = threading.Thread(target=self._deliver_events, daemon=True,
                                                     name='office-codex-public-events')
                self.event_thread.start()
            settings, _ = self._read(sid)
            worker = self.workers.get(sid)
            if worker is None:
                if len(self.workers) >= self.MAX_PROCESSES:
                    idle = next((key for key, value in self.workers.items()
                        if not value.active and not value.dispatch_reserved and not value.pending_events), None)
                    if idle is None:
                        raise DeliveryError('codex_background_capacity')
                    self.workers.pop(idle).close()
                worker = _Session(self, sid, settings)
                self.workers[sid] = worker
            worker.dispatch_reserved = True
        try:
            # Re-read AFTER the OS lease is acquired. A different OFFICE may
            # have updated this lifecycle between availability and lease.
            _, state = self._read(sid)
            return worker.submit(match.group(1), marked_text, marked_text[match.end():], state)
        except Exception:
            with self.lock:
                if self.workers.get(sid) is worker:
                    self.workers.pop(sid, None)
            worker.close()
            raise
        finally:
            worker.dispatch_reserved = False

    def approvals(self, sid):
        with self.lock:
            worker = self.workers.get(sid)
            return worker.approvals() if worker and sid in self.sessions else []

    def decide(self, sid, ident, decision):
        if decision not in {'allow_once', 'reject_once'}:
            return False
        with self.lock:
            worker = self.workers.get(sid)
            return bool(worker and sid in self.sessions and worker.decide(ident, decision))

    def sync_config(self, config):
        with self.lock:
            settings = config.get('command_adapters', {}).get(self.mode, {})
            sessions = dict(settings.get('sessions', {}))
            removed = [sid for sid, worker in self.workers.items()
                       if sessions.get(sid) != worker.settings or settings.get('binary') != self.settings.get('binary')]
            self.config, self.settings, self.sessions = config, settings, sessions
            workers = [self.workers.pop(sid) for sid in removed]
        for worker in workers:
            worker.close()

    def close(self):
        with self.lock:
            self.closed = True
            workers, self.workers = list(self.workers.values()), {}
            try:
                self.event_queue.put_nowait(None)
            except queue.Full:
                pass
        for worker in workers:
            worker.close()


class _Session:
    APPROVALS = {'item/commandExecution/requestApproval', 'item/fileChange/requestApproval'}
    MAX_TURN_SECONDS = 3600

    def __init__(self, adapter, sid, settings):
        self.adapter, self.sid, self.settings = adapter, sid, dict(settings)
        self.path = Path(settings['lifecycle_file'])
        self.lease = _Lease(self.path)
        self.lock = threading.RLock()
        self.client = None
        self.closed = False
        self.active = None
        self.dispatch_reserved = False
        self.projection_failed = False
        self.event_lock = threading.Lock()
        self.pending_events = 0
        self.pending = {}
        self.thread = None
        self.runtime_version = None
        self.runtime_identity = None

    def _save(self, state, **changes):
        state.update(changes, updated_at=timestamp())
        durable_state(self.path, state)

    def _journal(self, state, phase):
        active = self.active
        if active:
            durable_state(self.path.parent / (self.sid + '-turns') / (active['command_id'] + '.json'), {
                'schema': 1, 'session_id': self.sid, 'command_id': active['command_id'],
                'native_thread_id': state.get('native_thread_id'),
                'native_turn_id': active.get('turn_id'), 'phase': phase,
                'prompt_sha256': hashlib.sha256(active['prompt'].encode()).hexdigest(),
                'updated_at': timestamp()})

    def _event(self, kind, **fields):
        active = self.active
        if self.closed or not active or not active.get('accepted'):
            return
        with self.event_lock:
            self.pending_events += 1
            try:
                self.adapter.event_queue.put_nowait((self, dict(adapter=self.adapter.mode,
                    session_id=self.sid, run_id=active['command_id'], native_run_id=active['turn_id'],
                    type=kind, **fields)))
            except queue.Full:
                self.pending_events -= 1
                raise

    def _start_client(self):
        binary = resolve_binary(self.adapter.settings)
        identity = runtime_info(binary)['identity']
        if self.client and not self.client.closed.is_set():
            if self.runtime_identity == identity:
                return
            # submit() only enters here from a durable idle phase, before
            # thread/resume or turn/start. Never roll an active transport or
            # discard public events which still await Office projection.
            if (self.pending or self.pending_events or (self.active and
                    (self.active.get('accepted') or self.active.get('thread_id')))):
                raise DeliveryError('codex_background_busy')
        if self.client:
            self.client.close()
        self.runtime_identity = None
        client = self.adapter.rpc_factory(binary, self.settings['cwd'], BASE_OVERRIDES)
        try:
            client.initialize()
            logged_in, config = _metadata(client, self.settings['cwd'])
            if not logged_in:
                raise DeliveryError('codex_login_required')
            disables = _disabled_integrations(config)
            if disables:
                client.close()
                client = self.adapter.rpc_factory(binary, self.settings['cwd'], BASE_OVERRIDES + disables)
                client.initialize()
                logged_in, config = _metadata(client, self.settings['cwd'])
            if not logged_in or not _integrations_disabled(config):
                raise DeliveryError('codex_background_tool_policy_unverified')
            if runtime_info(binary)['identity'] != identity:
                raise DeliveryError('codex_runtime_changed')
            # Capability metadata only: no auth file/account contents are
            # persisted. This scopes later model-rejection warnings to the
            # executable version which observed that native failure.
            version = self.adapter.probe_fn({**self.adapter.settings, 'binary':binary}).get('version')
            self.runtime_version = version if isinstance(version, str) and re.fullmatch(r'[A-Za-z0-9 .+_-]{1,80}', version) else None
            self.runtime_identity = identity
            self.client = client
        except Exception:
            client.close()
            raise

    def _ensure_thread(self, state):
        native = state.get('native_thread_id')
        params = {'cwd': self.settings['cwd'], 'sandbox': 'read-only',
                  'approvalPolicy': 'on-request', 'approvalsReviewer': 'user'}
        # settings.model is an explicitly selected desired model. state.model
        # is the last verified native model and stays unchanged until resume
        # confirms the new choice on the same native thread.
        model = self.settings.get('model') or state.get('model')
        if model:
            params['model'] = model
        if state.get('model_provider'):
            params['modelProvider'] = state['model_provider']
        if native:
            params['threadId'] = native
            method = 'thread/resume'
        else:
            params['ephemeral'] = False
            params['threadSource'] = 'virtual-office'
            method = 'thread/start'
        # Thread create is also a native mutation; a lost reply must not cause
        # another thread to be silently created on the next instruction.
        self._save(state, phase='thread_starting')
        response = self.client.call(method, params)
        thread_id = response.get('thread', {}).get('id')
        if not isinstance(thread_id, str) or not thread_id or (native and thread_id != native):
            raise DeliveryError('codex_thread_identity_mismatch', uncertain=True)
        self._save(state, native_thread_id=thread_id)
        if model and response.get('model') != model:
            raise DeliveryError('codex_background_model_mismatch', uncertain=True)
        policy = response.get('sandbox', {})
        if (policy.get('type') != 'readOnly' or policy.get('networkAccess', False) is not False
                or response.get('approvalPolicy') != 'on-request'
                or response.get('approvalsReviewer') != 'user'
                or response.get('cwd') != self.settings['cwd']
                or not isinstance(response.get('model'), str) or not response['model']):
            raise DeliveryError('codex_background_policy_mismatch', uncertain=True)
        self._save(state, phase='ready', native_target_verified=True,
                   model=response.get('model'), model_provider=response.get('modelProvider'),
                   runtime_version=self.runtime_version)
        return thread_id

    def submit(self, command_id, marked_text, prompt, state):
        with self.lock:
            if self.closed or self.active or state.get('phase') not in SAFE_PHASES:
                raise DeliveryError('codex_background_busy')
            journal = self.path.parent / (self.sid + '-turns') / (command_id + '.json')
            if journal.exists():
                raise DeliveryError('codex_command_already_attempted', uncertain=True)
            self.active = {'command_id': command_id, 'prompt': prompt, 'marked_text': marked_text,
                'state': state, 'thread_id': None, 'turn_id': None, 'accepted': False,
                'segments': {}, 'finished_items': set(), 'item_phases': {}, 'last_text': '',
                'deadline': time.monotonic() + self.MAX_TURN_SECONDS}
            mutating = False
            try:
                self._start_client()
                mutating = True
                native = self._ensure_thread(state)
                self.active['thread_id'] = native
                self._save(state, phase='dispatching', command_id=command_id, native_turn_id=None)
                self._journal(state, 'dispatching')
                result = self.client.call('turn/start', {'threadId': native,
                    'input': [{'type': 'text', 'text': prompt}], 'clientUserMessageId': command_id,
                    'approvalPolicy': 'on-request', 'approvalsReviewer': 'user',
                    'sandboxPolicy': {'type': 'readOnly', 'networkAccess': False}})
                turn = result.get('turn', {}).get('id')
                if not isinstance(turn, str) or not turn:
                    raise DeliveryError('codex_invalid_turn_receipt', uncertain=True)
                self.active['turn_id'] = turn
                # Receipt journal precedes OFFICE publication and all text.
                self._save(state, phase='running', native_turn_id=turn)
                self._journal(state, 'accepted')
                self.active['accepted'] = True
                self._event('accepted')
                self.thread = threading.Thread(target=self._observe, args=(command_id,), daemon=True,
                                                name='office-codex-turn')
                self.thread.start()
                return {'native_message_id': turn, 'delivery_mode': self.adapter.mode}
            except Exception as error:
                uncertain = mutating or getattr(error, 'uncertain', False)
                if uncertain:
                    self._save(state, phase='unknown', detail_code='codex_delivery_unknown')
                    self._journal(state, 'unknown')
                self.active = None
                code = getattr(error, 'code', 'codex_background_failed')
                raise DeliveryError(code, uncertain=uncertain)

    def _matches(self, params):
        active = self.active
        if not isinstance(params, dict) or not isinstance(params.get('turn') or {}, dict):
            return False
        turn_id = params.get('turnId') or (params.get('turn') or {}).get('id')
        return bool(active and params.get('threadId') == active['thread_id'] and turn_id == active['turn_id'])

    def _phase(self, item):
        ident, phase = item.get('id'), item.get('phase')
        if isinstance(ident, str) and phase in {'commentary', 'final_answer'}:
            self.active['item_phases'][ident] = phase
        return self.active['item_phases'].get(ident)

    def _public(self, item_id, text, final=False, seal=False, authoritative=False):
        active = self.active
        if not isinstance(item_id, str) or not isinstance(text, str):
            return
        if item_id in active['finished_items'] and not authoritative:
            return
        segments = active['segments']
        segment = segments.setdefault(item_id, {'number': len(segments), 'text': '', 'final': False})
        final = final or segment['final']
        if authoritative and segment['text'] == text[:256000] and segment['final'] == final:
            if seal:
                active['finished_items'].add(item_id)
            return
        segment['text'] = text[:256000]
        segment['final'] = final
        # An item can finish while the turn continues (commentary/preamble).
        # Seal its deltas independently from the public delivery/final marker.
        if seal:
            active['finished_items'].add(item_id)
        active['last_text'] = segment['text']
        self._event('text', text=segment['text'], segment=segment['number'], final=final)

    def _finish(self, state, detail=None, explanation=None):
        active = self.active
        if not active:
            return
        unknown = state == 'delivery_unknown'
        for permission in list(self.pending.values()):
            self._reply(permission['rpc_id'], {'decision': 'decline'})
        self.pending.clear()
        self._save(active['state'], phase='unknown' if unknown else 'ready',
            last_command_id=active['command_id'], last_turn_id=active['turn_id'],
            last_outcome=state, detail_code=detail,
            model_error_model=active['state'].get('model') if detail=='codex_model_unavailable' else None,
            model_error_runtime_version=self.runtime_version if detail=='codex_model_unavailable' else None)
        self._journal(active['state'], state)
        delivery = active['last_text']
        if explanation:
            delivery = (delivery + '\n\n' if delivery else '') + explanation
        self._event('interrupted' if unknown else 'finished', state=state,
            text=delivery, detail_code=detail,
            source_kind='codex_app_server', source_ref=active['thread_id'])
        self.active = None

    def _reply(self, ident, result=None, error=None):
        try:
            self.client.reply(ident, result=result, error=error)
        except RpcError:
            pass

    def _request(self, message):
        method, params = message['method'], message.get('params', {})
        if method not in self.APPROVALS:
            self._reply(message['id'], error={'code': -32601, 'message': 'Request unsupported by OFFICE'})
            return
        if not self._matches(params):
            self._reply(message['id'], {'decision': 'decline'})
            return
        ident = str(uuid.uuid4())
        # A grantRoot approval can widen permission beyond this single action.
        can_allow = not bool(params.get('grantRoot'))
        text = params.get('command') or params.get('reason') or 'Codex requests a one-time file change'
        self.pending[ident] = {'id': ident, 'rpc_id': message['id'],
            'run_id': self.active['command_id'], 'preview': redact(text, security_secrets(self.adapter.config), 4000),
            'can_allow': can_allow, 'deadline': time.monotonic() + 120,
            'expires_at': timestamp(time.time() + 120)}
        self._event('status', waiting=True, step='Codex 等待一次性批准')

    def approvals(self):
        with self.lock:
            return [{key: value[key] for key in ('id', 'run_id', 'preview', 'can_allow', 'expires_at')}
                    for value in self.pending.values() if value['deadline'] > time.monotonic()]

    def decide(self, ident, decision):
        with self.lock:
            permission = self.pending.get(ident)
            if (self.closed or not self.active or not permission or permission['deadline'] <= time.monotonic()
                    or (decision == 'allow_once' and not permission['can_allow'])):
                return False
            try:
                self.client.reply(permission['rpc_id'], result={
                    'decision': 'accept' if decision == 'allow_once' else 'decline'})
            except RpcError:
                return False
            self.pending.pop(ident)
            self._event('status', waiting=bool(self.pending), step='Codex 正在處理指示')
            return True

    def _notification(self, message):
        method, params = message['method'], message.get('params') or {}
        if not self._matches(params):
            return
        if method == 'item/agentMessage/delta':
            ident, delta = params.get('itemId'), params.get('delta')
            if isinstance(delta, str):
                previous = self.active['segments'].get(ident, {}).get('text', '')
                self._public(ident, previous + delta)
        elif method == 'item/completed':
            item = params.get('item') or {}
            if item.get('type') == 'agentMessage':
                self._public(item.get('id'), item.get('text'),
                             final=self._phase(item) == 'final_answer', seal=True)
        elif method == 'turn/completed':
            turn = params['turn']
            status = turn.get('status')
            final_public = None
            public_items = [item for item in turn.get('items', []) if isinstance(item, dict)
                and item.get('type') == 'agentMessage' and isinstance(item.get('id'), str)
                and isinstance(item.get('text'), str)]
            for index, item in enumerate(public_items):
                phase = self._phase(item)
                is_final = phase == 'final_answer' or (phase is None and status == 'completed'
                    and index == len(public_items) - 1)
                self._public(item['id'], item['text'], final=is_final, seal=True, authoritative=True)
                final_public = item['text'][:256000]
            if not public_items and self.active['segments']:
                # Some servers send an empty items snapshot after streaming.
                # Unknown-phase legacy text becomes delivery only now.
                ident = next(reversed(self.active['segments']))
                segment = self.active['segments'][ident]
                phase = self.active['item_phases'].get(ident)
                is_final = phase == 'final_answer' or (phase is None and status == 'completed')
                self._public(ident, segment['text'], final=is_final, seal=True, authoritative=True)
                final_public = segment['text']
            if final_public is not None:
                self.active['last_text'] = final_public
            terminal = {'completed': 'completed', 'failed': 'failed', 'interrupted': 'cancelled'}
            if status in terminal:
                detail = classify_turn_error(turn.get('error')) if status == 'failed' else None
                self._finish(terminal[status], detail, FAILURE_TEXT.get(detail))
        elif method == 'item/started':
            # Tool details/arguments and reasoning deliberately never enter chat.
            item = params.get('item') or {}
            if item.get('type') == 'agentMessage':
                self._phase(item)
            elif item.get('type') in {'commandExecution', 'fileChange'}:
                self._event('status', waiting=False, step='Codex 正在處理工作')

    def _observe(self, command_id):
        try:
            with self.lock:
                if self.closed or not self.active or self.active['command_id'] != command_id:
                    return
                self._event('started', text=self.active['marked_text'])
            while not self.closed:
                with self.lock:
                    if not self.active or self.active['command_id'] != command_id:
                        return
                    if self.projection_failed:
                        self._finish('delivery_unknown', 'codex_public_projection_failed')
                        self.client.close()
                        return
                    for ident, permission in list(self.pending.items()):
                        if permission['deadline'] <= time.monotonic():
                            self._reply(permission['rpc_id'], {'decision': 'decline'})
                            self.pending.pop(ident)
                            self._event('status', waiting=bool(self.pending), step='批准已逾時；操作未獲批准')
                    if time.monotonic() >= self.active['deadline']:
                        self._finish('delivery_unknown', 'codex_turn_observation_timeout')
                        self.client.close()
                        return
                try:
                    message = self.client.events.get(timeout=0.25)
                except queue.Empty:
                    if self.client.closed.is_set():
                        with self.lock:
                            self._finish('delivery_unknown', 'codex_process_interrupted')
                        return
                    continue
                with self.lock:
                    if self.closed or not self.active or self.active['command_id'] != command_id:
                        return
                    if 'id' in message:
                        self._request(message)
                    else:
                        self._notification(message)
        except Exception:
            with self.lock:
                if self.active and self.active['command_id'] == command_id and not self.closed:
                    try:
                        self._finish('delivery_unknown', 'codex_observation_failed')
                    except Exception:
                        # Durable running/dispatching is itself a restart fence.
                        self.active = None
            if self.client:
                self.client.close()

    def close(self):
        with self.lock:
            if self.closed:
                return
            # Fence callbacks first. Logout never projects late replies.
            self.closed = True
            if self.active:
                state = self.active['state']
                try:
                    self._save(state, phase='unknown', detail_code='codex_receiver_closed')
                    self._journal(state, 'unknown')
                except (OSError, ValueError):
                    pass
            self.active = None
            self.pending.clear()
            if self.client:
                self.client.close()
            self.lease.close()

