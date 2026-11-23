"""OFFICE-owned local Cursor SDK sessions; never an existing IDE sidebar chat.

Adapted from dominicliu69/virtual-office commit
0753acc8591e48cfaf3199a6f1b2cabbdc1bbb07. The integration uses the public
SDK surface, preserves uncertain delivery, and does not cancel/retry busy runs.
"""
import importlib.util
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager

from command_adapters import DeliveryError
from cursor_sdk_bridge import launch_client
from platform_adapters import MARKER, secret_file, validate_key

LOCAL_AGENT = re.compile(r'agent-[A-Za-z0-9_.-]{1,140}\Z')
LOCAL_RUN = re.compile(r'[A-Za-z0-9_.-]{1,160}\Z')
MODEL = re.compile(r'[A-Za-z0-9._-]{1,64}\Z')
DEFAULT_MODEL = 'composer-2.5'
SUPPORTED_SDK_VERSION = '1.3.1'


class LocalStartError(Exception):
    def __init__(self, code, uncertain=False, native_attempted=None, diagnostic=None):
        self.code, self.uncertain = code, uncertain
        self.native_attempted, self.diagnostic = native_attempted, diagnostic
        super().__init__(code)


def local_agent_id(value):
    if not isinstance(value, str) or not LOCAL_AGENT.fullmatch(value):
        raise ValueError('cursor_local_agent_invalid')
    return value


def tool_restrictions(value):
    """This first SDK route is explicitly chat-only on every create/resume.

    It does not inherit Desktop approval rules. SDK project/user settings and
    inline MCP are not loaded, and no local tools are silently enabled.
    """
    result = {}
    for key in ('tools', 'disallowed_tools', 'setting_sources'):
        items = value.get(key, [])
        if not isinstance(items, list) or items:
            raise ValueError('cursor_chat_only_policy_required')
        result[key] = []
    return result


def field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def public_event(message):
    """Only typed SDK assistant text is public. Tool arguments never cross here."""
    kind = field(message, 'type')
    if kind == 'tool_call':
        return {'kind': 'tool'}
    if kind != 'assistant':
        return None
    body = field(message, 'message')
    content = field(body, 'content', ()) or ()
    text = ''.join(field(block, 'text') for block in content
                   if field(block, 'type') == 'text' and isinstance(field(block, 'text'), str))
    # Pinned SDK1.3.1 LOCAL assistant messages are ordered text deltas.
    # Its typed message content has no native assistant-message identity.
    return {'kind': 'text', 'text': text[:256000]}


def close_client(client):
    try:
        client.close()
    except Exception:
        pass  # Cleanup cannot erase a verified receipt or strand worker state.


def lifecycle_path(settings):
    credential = Path(settings['credential_file'])
    return credential.with_name(credential.stem + '.lifecycle.json')


def durable_state(path, value):
    """Flush the pre-mutation fence before any SDK create/send call."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == 'posix':
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try: os.fsync(directory)
            finally: os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def state_lock(path):
    """An abandoned lock fails closed; it is never guessed safe to replay."""
    lock = Path(str(path) + '.lock')
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise LocalStartError('cursor_sdk_recovery_required') from error
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(str(os.getpid())); stream.flush(); os.fsync(stream.fileno())
        yield
    finally:
        lock.unlink(missing_ok=True)


class SessionLifecycle:
    def __init__(self, settings, sid):
        self.path, self.sid, self.cwd = lifecycle_path(settings), sid, settings['cwd']

    def read(self):
        try:
            value = secret_file(str(self.path))
        except (ValueError,OSError) as error:
            code='cursor_sdk_recovery_required' if self.path.exists() else 'cursor_legacy_session_review_required'
            raise LocalStartError(code) from error
        if (not isinstance(value, dict) or value.get('schema') != 1 or
                value.get('agent_id') != self.sid or value.get('cwd') != self.cwd or
                value.get('phase') not in {'reserved','initializing','native_created','sending','accepted','ready','unknown'}):
            raise LocalStartError('cursor_sdk_recovery_required')
        return value

    def begin(self, command):
        with state_lock(self.path):
            value = self.read()
            if value['phase'] not in {'reserved','ready'}:
                raise LocalStartError('cursor_sdk_recovery_required')
            initialize = value['phase'] == 'reserved'
            value.update(phase='initializing' if initialize else 'sending', command_id=command,
                         native_run_id=None, updated_at=time.time())
            durable_state(self.path, value)
            return initialize

    def record(self, command, phase, **fields):
        with state_lock(self.path):
            value = self.read()
            if value.get('command_id') != command:
                raise LocalStartError('cursor_sdk_lifecycle_mismatch', True)
            value.update(phase=phase, updated_at=time.time(), **fields)
            durable_state(self.path, value)

    def fail(self, command, error, previous):
        # Only a proof that no mutating native call was attempted can restore
        # the prior state. Auth/busy after native creation is still uncertain.
        safe = isinstance(error, LocalStartError) and error.native_attempted is False
        self.record(command, previous if safe else 'unknown',
                    reason=getattr(error, 'code', 'cursor_local_delivery_unknown'),
                    diagnostic=getattr(error, 'diagnostic', None))


class SdkRuntime:
    """Optional official SDK, imported only for explicitly selected SDK sessions."""
    reason = 'cursor_sdk_required'

    def available(self):
        self.version = None
        if sys.version_info < (3, 10):
            self.reason = 'cursor_python_required'
            return False
        from cursor_sdk_setup import sdk_platform_supported
        if not sdk_platform_supported():
            self.reason = 'cursor_sdk_platform_unsupported'
            return False
        try:
            available = importlib.util.find_spec('cursor_sdk') is not None
            self.version = importlib.metadata.version('cursor-sdk') if available else None
        except (ValueError, ImportError):
            available = False
            self.version = None
        if available and self.version != SUPPORTED_SDK_VERSION:
            self.reason = 'cursor_sdk_version_unsupported'
            return False
        self.reason = None if available else 'cursor_sdk_required'
        return available

    @staticmethod
    def translate(error, uncertain=False):
        code, status = getattr(error, 'code', None), getattr(error, 'status', None)
        if code in {'unauthenticated', 'permission_denied'} or status in {401, 403}:
            return LocalStartError('platform_auth_required')
        if code in {'already_exists', 'failed_precondition', 'resource_exhausted'}:
            return LocalStartError('platform_agent_busy')
        return LocalStartError('cursor_local_delivery_unknown' if uncertain else 'cursor_local_start_failed', uncertain)

    def reserve(self, api_key, model, cwd, restrictions=None, request_id=None, name=None):
        """Registration is local metadata only, not SDK native creation."""
        validate_key(api_key); tool_restrictions(restrictions or {})
        return 'agent-' + str(uuid.uuid4())

    @staticmethod
    def diagnostic(error, stage, native_attempted):
        codes = {'internal','invalid_argument','failed_precondition','unauthenticated','permission_denied',
                 'not_found','unavailable','resource_exhausted','unknown','deadline_exceeded','already_exists','cancelled'}
        code = getattr(error, 'code', None)
        status = getattr(error, 'status', None)
        return {'error_type': type(error).__name__[:80], 'code': code if code in codes else 'unclassified',
                'status': status if isinstance(status,int) and 100<=status<=599 else None,
                'stage':stage,'native_mutation_attempted':native_attempted}

    def send(self, api_key, model, cwd, agent_id, text, restrictions=None, command_id=None,
             initialize=False, on_created=None, name=None):
        from cursor_sdk import Agent, AgentOptions, CursorClient, LocalAgentOptions
        resources, native_attempted, stage = ExitStack(), False, 'launch'
        try:
            client, release = launch_client(cwd, CursorClient)
            resources.callback(release)
            policy = tool_restrictions(restrictions or {})
            options = AgentOptions(api_key=api_key, model=model, tools=policy['tools'],
                disallowed_tools=policy['disallowed_tools'], local=LocalAgentOptions(cwd=cwd, setting_sources=policy['setting_sources']),
                **({'agent_id':agent_id,'name':name} if initialize else {}))
            if initialize:
                # SDK1.3.1 creates an empty queued bootstrap. Only THIS handle
                # remembers its initial run; close+resume cannot adopt it.
                stage, native_attempted = 'create', True
                agent = Agent.create(options, client=client)
                if local_agent_id(agent.agent_id) != agent_id:
                    raise LocalStartError('cursor_native_identity_mismatch', True, True)
                stage = 'journal_created'
                if on_created is None:
                    raise LocalStartError('cursor_sdk_lifecycle_unavailable', True, True)
                on_created(agent_id)
            else:
                stage = 'resume'
                agent = Agent.resume(agent_id, options, client=client)
                if local_agent_id(agent.agent_id) != agent_id:
                    raise LocalStartError('cursor_native_identity_mismatch', False, False)
            stage, native_attempted = 'send', True
            run = agent.send(text)
            return run, resources
        except Exception as error:
            close_client(resources)
            translated = error if isinstance(error,LocalStartError) else self.translate(error, native_attempted)
            translated.native_attempted = native_attempted
            translated.uncertain = native_attempted
            translated.diagnostic = self.diagnostic(error, stage, native_attempted)
            if translated is error:
                raise
            raise translated from error


class CursorLocalAdapter:
    mode, serial, same_session = 'cursor_local', True, True

    def __init__(self, config, emit, runtime=None):
        self.config, self.emit = config, emit
        self.sessions = config.get('command_adapters', {}).get('cursor_local', {}).get('sessions', {})
        self.runtime = runtime or SdkRuntime()
        self.reason = 'cursor_api_key_required'
        self.lock, self.workers = threading.RLock(), {}
        self.live = {}
        self.stop = threading.Event()

    def session(self, sid):
        local_agent_id(sid)
        item = self.sessions[sid]
        key = validate_key(secret_file(item['credential_file']).get('api_key'))
        cwd, model = item.get('cwd'), item.get('model') or DEFAULT_MODEL
        if not isinstance(cwd, str) or not Path(cwd).is_dir():
            raise ValueError('cursor_workspace_unavailable')
        if not isinstance(model, str) or not MODEL.fullmatch(model):
            raise ValueError('cursor_model_invalid')
        return key, model, str(Path(cwd)), tool_restrictions(item)

    def available(self, sid):
        try:
            self.session(sid)
            state = SessionLifecycle(self.sessions[sid], sid).read()
            if Path(str(lifecycle_path(self.sessions[sid])) + '.lock').exists():
                raise LocalStartError('cursor_sdk_recovery_required')
            if state['phase'] not in {'reserved','ready'} and sid not in self.live:
                raise LocalStartError('cursor_sdk_recovery_required')
        except (ValueError, OSError, KeyError, TypeError, LocalStartError) as error:
            code = error.args[0] if error.args else ''
            self.reason = code if isinstance(code, str) and code.startswith(('cursor_', 'platform_', 'invalid_')) else 'cursor_api_key_required'
            return False
        if not self.runtime.available():
            self.reason = getattr(self.runtime, 'reason', None) or 'cursor_sdk_required'
            return False
        self.reason = None
        return not self.stop.is_set()

    def native_verified(self, sid):
        try:
            return SessionLifecycle(self.sessions[sid], sid).read().get('native_target_verified') is True
        except (ValueError, OSError, KeyError, TypeError, LocalStartError):
            return False

    def can_dispatch(self, sid):
        with self.lock:
            return not self.stop.is_set() and sid not in self.live

    def event(self, sid, command, native, kind, **fields):
        self.emit('cursor:' + sid, {'type': kind, 'adapter': self.mode, 'session_id': sid,
            'run_id': command, 'native_run_id': native, **fields})

    def submit(self, sid, text):
        match = MARKER.match(text)
        if not match or not self.available(sid):
            raise DeliveryError(self.reason or 'invalid_command')
        if not self.can_dispatch(sid):
            raise DeliveryError('platform_agent_busy')
        body = text[match.end():]
        if not body.strip():
            raise DeliveryError('invalid_command')
        command = match[1]
        lifecycle = SessionLifecycle(self.sessions[sid], sid)
        initialize = None
        try:
            key, model, cwd, restrictions = self.session(sid)
            initialize = lifecycle.begin(command)
            def created(native_agent):
                if native_agent != sid:
                    raise LocalStartError('cursor_native_identity_mismatch', True, True)
                lifecycle.record(command, 'native_created', native_agent_id=native_agent)
            run, client = self.runtime.send(key, model, cwd, sid, body, restrictions, command_id=command,
                                           initialize=initialize, on_created=created, name=lifecycle.read().get('name'))
            if initialize and lifecycle.read()['phase'] != 'native_created':
                close_client(client)
                raise LocalStartError('cursor_sdk_lifecycle_unavailable', True, True)
        except LocalStartError as error:
            if initialize is not None:
                try: lifecycle.fail(command, error, 'reserved' if initialize else 'ready')
                except (ValueError, OSError, LocalStartError): pass
            raise DeliveryError(error.code, error.uncertain or (initialize is not None and error.native_attempted is not False))
        except Exception as error:
            if initialize is not None:
                try: lifecycle.fail(command, error, 'reserved' if initialize else 'ready')
                except (ValueError, OSError, LocalStartError): pass
            raise DeliveryError('cursor_local_delivery_unknown', initialize is not None) from error
        native = getattr(run, 'id', None)
        if not isinstance(native, str) or not LOCAL_RUN.fullmatch(native):
            close_client(client)
            lifecycle.record(command, 'unknown', reason='cursor_invalid_run_receipt')
            raise DeliveryError('cursor_invalid_run_receipt', True)
        try:
            lifecycle.record(command, 'accepted', native_run_id=native, native_target_verified=True)
            self.event(sid, command, native, 'accepted')
            worker = threading.Thread(target=self.observe, args=(sid, command, native, text, run, client), daemon=True)
            with self.lock:
                self.live[sid] = (native, command, run)
                self.workers[sid] = worker
            worker.start()
        except Exception as error:
            with self.lock:
                self.live.pop(sid, None); self.workers.pop(sid, None)
            try:
                lifecycle.record(command, 'unknown', reason='cursor_observation_interrupted')
                self.event(sid, command, native, 'interrupted', state='delivery_unknown',
                    detail_code='cursor_observation_interrupted')
            except Exception:
                pass
            close_client(client)
            raise DeliveryError('cursor_observation_interrupted', True) from error
        return {'native_message_id': native, 'delivery_mode': self.mode}

    def observe(self, sid, command, native, prompt, run, client):
        texts, current, started = [], None, False

        def begin():
            nonlocal started
            if not started:
                self.event(sid, command, native, 'started', text=prompt)
                started = True

        try:
            for message in run.messages():
                if self.stop.is_set():
                    raise LocalStartError('cursor_observation_interrupted', True)
                item = public_event(message)
                if not item:
                    if field(message,'type') in {'thinking','user'}:
                        current = None
                    continue
                begin()
                if item['kind'] == 'tool':
                    self.event(sid, command, native, 'status', step='Cursor 正在執行工具')
                    current = None
                    continue
                if not item['text']:
                    continue
                # Native local text-delta becomes a typed public assistant
                # message. Accumulate in order without prefix/substring
                # guesses, emitting replacement snapshots for Office polling.
                if current is None:
                    if len(texts) >= 1000:
                        raise LocalStartError('cursor_public_message_limit', True)
                    current = len(texts)
                    texts.append('')
                texts[current] += item['text']
                if len(texts[current]) > 256000:
                    raise LocalStartError('cursor_public_message_limit', True)
                self.event(sid, command, native, 'text', text=texts[current], segment=current, final=False)
            if self.stop.is_set():
                raise LocalStartError('cursor_observation_interrupted', True)
            result = run.wait()
            state = {'finished':'completed', 'error':'failed', 'cancelled':'cancelled', 'expired':'failed'}.get(getattr(result, 'status', None))
            if not state:
                raise LocalStartError('cursor_observation_interrupted', True)
            final = result.result if isinstance(getattr(result, 'result', None), str) else ''
            begin()
            if final:
                if current is None or texts[current] != final:
                    current = len(texts)
                self.event(sid, command, native, 'text', text=final, segment=current, final=True)
            SessionLifecycle(self.sessions[sid],sid).record(command,'ready', native_run_id=native,
                native_target_verified=True, terminal_state=state)
            self.event(sid, command, native, 'finished', state=state, text=final,
                detail_code=None if final or state != 'completed' else 'completed_without_reply')
        except Exception as error:
            try: SessionLifecycle(self.sessions[sid],sid).record(command,'unknown',reason='cursor_observation_interrupted')
            except (ValueError,OSError,LocalStartError): pass
            self.event(sid, command, native, 'interrupted', state='delivery_unknown',
                detail_code=error.code if isinstance(error, LocalStartError) else 'cursor_observation_interrupted')
        finally:
            close_client(client)
            with self.lock:
                if self.workers.get(sid) is threading.current_thread():
                    self.workers.pop(sid, None); self.live.pop(sid, None)

    def close(self):
        self.stop.set()
        deadline = time.monotonic() + 2
        for worker in list(self.workers.values()):
            if worker.is_alive():
                worker.join(timeout=max(0,deadline-time.monotonic()))
