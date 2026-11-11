"""Hermes's native ACP transport. No model, provider, credential or UI routing.

One selected session owns one stdio client. Hermes owns its conversation database
and tools; Office stores only redacted public messages and delivery receipts.
"""
try:
    import fcntl
except ImportError:  # Keep the dashboard usable where POSIX session locks are unavailable.
    fcntl = None
import hashlib
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time
import uuid
from history import redact, timestamp


class AcpError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


class AcpClient:
    """Bounded JSON-RPC over local pipes; raw transport/logs are never saved."""
    def __init__(self, settings, folder, on_update=None, on_permission=None):
        if fcntl is None:
            raise AcpError('hermes_platform_unsupported')
        self.settings, self.folder = settings, Path(folder)
        self.on_update, self.on_permission = on_update, on_permission
        self.pending, self.sequence = {}, 0
        self.lock, self.write_lock = threading.RLock(), threading.Lock()
        self.close_lock, self.stopped = threading.Lock(), False
        self.closed = threading.Event()
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = os.environ.copy()
        env['PATH'] = os.pathsep.join(dict.fromkeys([
            str(Path(settings['command'][0]).parent), env.get('PATH', ''),
            '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin', '/usr/sbin', '/sbin']))
        if settings.get('home'):
            env['HERMES_HOME'] = settings['home']
        env['PYTHONUNBUFFERED'] = '1'
        self.child = subprocess.Popen(settings['command'], cwd=settings['cwd'], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, start_new_session=True)
        self.record('starting')
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.errors = threading.Thread(target=self._drain_errors, daemon=True)
        self.reader.start(); self.errors.start()

    def record(self, state):
        private_json(self.folder / 'process.json', {
            'pid': self.child.pid, 'parent_pid': os.getpid(), 'state': state,
            'transport': 'stdio', 'updated_at': timestamp(),
            'stop_target': '/bin/kill -TERM %s' % self.child.pid if state != 'stopped' else None})
        with (self.folder / 'stdout.log').open('a') as stream:
            stream.write(json.dumps({'state': state, 'at': timestamp()}) + '\n')
        (self.folder / 'stdout.log').chmod(0o600)

    def _send(self, data):
        with self.write_lock:
            if self.closed.is_set() or self.child.poll() is not None:
                raise AcpError('hermes_disconnected')
            try:
                self.child.stdin.write((json.dumps(data, ensure_ascii=False) + '\n').encode())
                self.child.stdin.flush()
            except (OSError, ValueError):
                raise AcpError('hermes_disconnected')

    def _server_request(self, message):
        try:
            if message.get('method') != 'session/request_permission':
                self._send({'jsonrpc': '2.0', 'id': message['id'],
                            'error': {'code': -32601, 'message': 'Client capability unavailable'}})
                return
            result = self.on_permission(message.get('params', {})) if self.on_permission else {'outcome': {'outcome': 'cancelled'}}
            self._send({'jsonrpc': '2.0', 'id': message['id'], 'result': result})
        except Exception:
            try:
                self._send({'jsonrpc': '2.0', 'id': message['id'], 'result': {'outcome': {'outcome': 'cancelled'}}})
            except AcpError:
                pass

    def _read(self):
        try:
            while not self.closed.is_set():
                line = self.child.stdout.readline(4 * 1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 4 * 1024 * 1024:
                    raise AcpError('hermes_frame_too_large')
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    raise AcpError('hermes_invalid_protocol')
                if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
                    raise AcpError('hermes_invalid_protocol')
                if 'method' in message:
                    if 'id' in message:
                        threading.Thread(target=self._server_request, args=(message,), daemon=True).start()
                    elif message['method'] == 'session/update' and self.on_update:
                        self.on_update(message.get('params', {}))
                else:
                    with self.lock:
                        target = self.pending.get(message.get('id'))
                    if target:
                        target.put(message)
        except Exception:
            pass  # Provider/protocol errors may contain prompts or credentials.
        finally:
            self.closed.set()
            with self.lock:
                for target in self.pending.values():
                    target.put({'disconnected': True})

    def _drain_errors(self):
        # Hermes's own profile owns its native logs. Office never copies stderr:
        # libraries can echo prompts, credentials, or internal reasoning there.
        while self.child.stderr.read(8192):
            pass

    def call(self, method, params, timeout=30):
        target = queue.Queue()
        with self.lock:
            self.sequence += 1
            ident = self.sequence
            self.pending[ident] = target
        try:
            self._send({'jsonrpc': '2.0', 'id': ident, 'method': method, 'params': params})
            try:
                response = target.get(timeout=timeout)
            except queue.Empty:
                raise AcpError('hermes_timeout')
            if response.get('disconnected'):
                raise AcpError('hermes_disconnected')
            if 'error' in response:
                raise AcpError('hermes_rpc_error')
            if not isinstance(response.get('result'), dict):
                raise AcpError('hermes_invalid_result')
            return response['result']
        finally:
            with self.lock:
                self.pending.pop(ident, None)

    def initialize(self):
        result = self.call('initialize', {'protocolVersion': 1, 'clientCapabilities': {},
            'clientInfo': {'name': 'virtual-office', 'version': '1.0'}})
        if result.get('protocolVersion') != 1 or not result.get('agentCapabilities', {}).get('loadSession'):
            raise AcpError('hermes_resume_unsupported')
        if 'hermes' not in result.get('agentInfo', {}).get('name', '').lower():
            raise AcpError('not_hermes')
        self.record('ready')
        return result

    def close(self):
        with self.close_lock:
            if self.stopped:return
            self._close_owned_process()
            self.stopped=True

    def _close_owned_process(self):
        if self.child.poll() is None:
            # Stop only this client and its own process group, never another
            # terminal/App session. EOF alone is not reliable across runtimes.
            try:
                os.killpg(self.child.pid, signal.SIGTERM)
                self.child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(self.child.pid, signal.SIGKILL)
                self.child.wait(timeout=3)
            except ProcessLookupError:
                pass
        self.closed.set()
        for stream in (self.child.stdin, self.child.stdout, self.child.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        self.record('stopped')


def session_lease(settings, session_id):
    if fcntl is None:
        raise AcpError('hermes_platform_unsupported')
    home = Path(settings.get('home') or os.environ.get('HERMES_HOME') or Path.home() / '.hermes')
    leases = home / 'office-session-locks'
    leases.mkdir(parents=True, exist_ok=True, mode=0o700)
    lease = (leases / (session_id + '.lock')).open('a')
    try:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lease.close()
        raise AcpError('hermes_session_in_use')
    return lease


def connect_session(settings, folder, session_id=None):
    """Create/load via the native API; never fabricate IDs or write Hermes DBs."""
    if session_id:
        try:
            if str(uuid.UUID(session_id)) != session_id:raise ValueError()
        except (ValueError, TypeError, AttributeError):raise AcpError('hermes_invalid_session')
    lease = session_lease(settings, session_id) if session_id else None
    client = None
    try:
        client = AcpClient(settings, folder)
        client.initialize()
        params = {'cwd': settings['cwd'], 'mcpServers': []}
        if session_id:
            params['sessionId'] = session_id
            client.call('session/load', params)
        else:
            result = client.call('session/new', params)
            session_id = result.get('sessionId')
        try:
            if str(uuid.UUID(session_id)) != session_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise AcpError('hermes_invalid_session')
        return session_id
    finally:
        if client:client.close()
        if lease:lease.close()


class HermesAcpAdapter:
    mode, serial = 'hermes_acp', True

    def __init__(self, config, emit):
        self.config, self.emit = config, emit
        self.sessions = config.get('command_adapters', {}).get('hermes_acp', {}).get('sessions', {})
        self.reason, self.runtimes, self.lock = 'hermes_unavailable', {}, threading.RLock()
        self.stopping = False

    def available(self, session_id=None):
        values = [self.sessions.get(session_id)] if session_id else list(self.sessions.values())
        return fcntl is not None and bool(values) and all(s and Path(s['command'][0]).is_file() and
            os.access(s['command'][0], os.X_OK) and Path(s['cwd']).is_dir() and
            (not s.get('home') or Path(s['home']).is_dir()) for s in values)

    def submit(self, session_id, text):
        from command_adapters import DeliveryError
        if self.stopping or not self.available(session_id):
            raise DeliveryError('hermes_unavailable')
        with self.lock:
            runtime = self.runtimes.get(session_id)
            if runtime is None:
                runtime = HermesSession(self, session_id, self.sessions[session_id])
                self.runtimes[session_id] = runtime
            if runtime.busy:
                raise DeliveryError('hermes_session_busy')
            runtime.busy = True
            worker = threading.Thread(target=runtime.run, args=(text,), daemon=True)
            runtime.worker = worker
            worker.start()
        # This is an Office-owned queue receipt. Actual execution is evidenced
        # by native session updates/response, not by starting this worker.
        return {'delivery_mode': self.mode}

    def can_dispatch(self, session_id):
        runtime = self.runtimes.get(session_id)
        return not self.stopping and (not runtime or not runtime.busy)

    def approvals(self, session_id):
        runtime = self.runtimes.get(session_id)
        return runtime.approvals() if runtime else []

    def decide(self, session_id, approval_id, decision):
        runtime = self.runtimes.get(session_id)
        return runtime.decide(approval_id, decision) if runtime else False

    def close(self):
        self.stopping = True
        for runtime in list(self.runtimes.values()):
            runtime.close()


class HermesSession:
    def __init__(self, adapter, session_id, settings):
        self.adapter, self.session_id, self.settings = adapter, session_id, settings
        self.folder = Path(adapter.config['data_dir']) / 'hermes' / session_id
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.client, self.active, self.worker = None, None, None
        self.busy, self.closed = False, False
        self.lock, self.permissions = threading.RLock(), {}
        self.lease = None

    def event(self, kind, **fields):
        active = self.active
        if active:
            self.adapter.emit('hermes:' + self.session_id, {'type': kind,
                'run_id': active['run_id'], 'session_id': self.session_id, **fields})

    def begin(self):
        if self.active and not self.active['started']:
            self.active['started'] = True
            self.event('started', text=self.active['prompt'], cwd=self.settings['cwd'])

    def flush(self, final=False):
        active = self.active
        if active and active['text']:
            self.event('text', text=active['text'], segment=active['segment'], final=final)
            active['last_flush'] = time.monotonic()

    def update(self, params):
        # session/load replays old history. Ignore it until prompt dispatch;
        # updates for another session are never admitted to this agent.
        with self.lock:
            if not self.active or not self.active['sent'] or params.get('sessionId') != self.session_id:
                return
            update = params.get('update', {})
            kind = update.get('sessionUpdate')
            if kind in {'agent_message_chunk', 'agent_thought_chunk', 'tool_call', 'tool_call_update', 'plan'}:
                self.begin()
            if kind == 'agent_message_chunk':
                block = update.get('content', {})
                if block.get('type') == 'text' and isinstance(block.get('text'), str):
                    self.active['text'] = (self.active['text'] + block['text'])[:256000]
                    if time.monotonic() - self.active['last_flush'] >= .3:
                        self.flush()
            elif kind == 'tool_call':
                self.flush()
                if self.active['text']:
                    self.active['segment'] += 1
                    self.active['text'] = ''
                self.active['tools'][str(update.get('toolCallId'))] = update.get('status', 'pending')
                self.event('status', step='Hermes 正在執行工具')
            elif kind == 'tool_call_update':
                ident = str(update.get('toolCallId'))
                if ident in self.active['tools'] and update.get('status'):
                    self.active['tools'][ident] = update['status']
            # Thought chunks, raw tool input/output and native plans are never
            # copied to Office messages or logs.

    def permission(self, params):
        with self.lock:
            if not self.active or not self.active['sent'] or params.get('sessionId') != self.session_id:
                return {'outcome': {'outcome': 'cancelled'}}
            self.begin()
            options = params.get('options', [])
            allow = next((o.get('optionId') for o in options if o.get('kind') == 'allow_once'), None)
            deny = next((o.get('optionId') for o in options if o.get('kind') == 'reject_once'), None)
            item = {'id': str(uuid.uuid4()), 'run_id': self.active['run_id'], 'event': threading.Event(),
                'allow': allow, 'deny': deny, 'decision': None,
                'preview': redact(params.get('toolCall', {}).get('title', 'Hermes 要求批准操作'),
                                  (self.adapter.config['topic'],), 4000),
                'expires_at': timestamp(time.time() + 45), 'deadline': time.monotonic() + 45}
            self.permissions[item['id']] = item
            self.event('status', waiting=True, step='Hermes 等待你批准操作')
        item['event'].wait(45)
        with self.lock:
            self.permissions.pop(item['id'], None)
            if item['decision'] != 'allow_once':
                if self.active:
                    self.active['permission_denied'] = True
            self.event('status', waiting=False, step='Hermes 正在處理指示')
        option = item['allow'] if item['decision'] == 'allow_once' else item['deny']
        return {'outcome': {'outcome': 'selected', 'optionId': option}} if option else {'outcome': {'outcome': 'cancelled'}}

    def approvals(self):
        with self.lock:
            return [{'id': p['id'], 'run_id': p['run_id'], 'preview': p['preview'],
                     'can_allow': bool(p['allow']), 'expires_at': p['expires_at']} for p in self.permissions.values()]

    def decide(self, ident, decision):
        with self.lock:
            item = self.permissions.get(ident)
            if not item or item['decision'] or time.monotonic()>=item['deadline'] or (decision == 'allow_once' and not item['allow']):
                return False
            item['decision'] = decision
            item['event'].set()
            return True

    def start(self):
        if self.client and not self.client.closed.is_set() and self.client.child.poll() is None:
            return
        if self.client:
            self.client.close()
        if self.lease is None:
            # Cross-Office lock is scoped to native home + ID, not data_dir.
            self.lease = session_lease(self.settings, self.session_id)
        folder = self.folder / ('runtime-' + uuid.uuid4().hex[:12])
        self.client = AcpClient(self.settings, folder, self.update, self.permission)
        self.client.initialize()
        self.client.call('session/load', {'sessionId': self.session_id, 'cwd': self.settings['cwd'], 'mcpServers': []})

    def run(self, prompt):
        # The request marker is an Office correlation ID, not a fabricated
        # native turn ID. ACP correlates the response to the original session.
        ident = str(uuid.uuid4())
        if prompt.startswith('[OFFICE:'):
            ident = prompt[8:44]
        folder = self.folder / 'turns' / ident
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.active = {'run_id': ident, 'prompt': prompt, 'started': False, 'sent': False,
            'text': '', 'segment': 0, 'last_flush': 0, 'tools': {}, 'permission_denied': False}
        private_json(folder / 'request.json', {'session_id': self.session_id, 'command_id': ident,
            'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(), 'created_at': timestamp()})
        result = {'session_id': self.session_id, 'command_id': ident, 'state': 'delivery_unknown'}
        try:
            if self.closed:
                raise AcpError('hermes_disconnected')
            self.start()
            with self.lock:
                self.active['sent'] = True
            response = self.client.call('session/prompt', {'sessionId': self.session_id,
                'prompt': [{'type': 'text', 'text': prompt}]}, self.settings.get('timeout_seconds', 600))
            with self.lock:
                self.begin()
                self.flush(final=True)
                reason = response.get('stopReason')
                final = self.active['text']
                failed = reason not in {'end_turn', 'cancelled'} or not final.strip() or self.active['permission_denied']
                # Some Hermes versions return an exception string with end_turn.
                failed = failed or final.lstrip().startswith(('Error:', 'API error:', 'Error running agent:'))
                state = 'cancelled' if reason == 'cancelled' else 'failed' if failed else 'completed'
                code = 'permission_denied' if self.active['permission_denied'] else 'hermes_turn_failed' if failed else None
                self.event('finished', state=state, text=final, detail_code=code)
                result.update(state=state, native_stop_reason=reason, tool_states=self.active['tools'])
            if state == 'failed' and self.client:
                self.client.close(); self.client = None
        except Exception as error:
            with self.lock:
                sent = bool(self.active and self.active['sent'])
                state = 'delivery_unknown' if sent else 'failed'
                code = error.code if isinstance(error, AcpError) else 'hermes_runtime_error'
                self.event('interrupted', state=state, detail_code=code)
                result.update(state=state, detail_code=code)
            if self.client:
                self.client.close(); self.client = None
        finally:
            result['finished_at'] = timestamp()
            private_json(folder / 'result.json', result)
            with self.lock:
                self.active = None
                self.busy = False

    def close(self):
        self.closed = True
        with self.lock:
            for permission in self.permissions.values():
                permission['event'].set()
        client=self.client
        if client:
            client.close()
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=5)
        if self.lease:
            self.lease.close(); self.lease = None
