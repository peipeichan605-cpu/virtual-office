"""Bounded, private stdio transport for an OFFICE-owned Codex app-server.

No socket, shell, auth-file access, or raw protocol logging. Only the subprocess
created here is terminated. Notifications are queued so synchronous receipts
can be committed before any public observation is delivered.
"""
import json
from contextlib import contextmanager
import os
import queue
import signal
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

PROBE_TIMEOUT = 45


class RpcError(Exception):
    def __init__(self, code, sent=False):
        self.code, self.sent = code, sent
        super().__init__(code)


def remaining_timeout(deadline, maximum=None):
    """Share one monotonic budget across a metadata probe's nested operations."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RpcError('codex_rpc_timeout')
    return min(remaining, maximum) if maximum is not None else remaining


@contextmanager
def deadline_lock(lock, deadline=None):
    if deadline is None:
        lock.acquire()
    elif not lock.acquire(timeout=remaining_timeout(deadline)):
        raise RpcError('codex_rpc_timeout')
    try:
        yield
    finally:
        lock.release()


def process_creation_flags():
    """Native Windows helpers stay in the background without a console window."""
    return getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0


def child_environment():
    env = dict(os.environ)
    # These describe the caller's currently executing turn, not this new thread.
    for key in list(env):
        if key.startswith(('CODEX_THREAD_', 'CODEX_TURN_', 'CODEX_INTERNAL_',
                           'CODEX_PARENT_', 'CODEX_AGENT_')) or key in {
                'CODEX_SESSION_ID', 'CODEX_RUN_ID', 'CODEX_SANDBOX',
                'CODEX_SANDBOX_NETWORK_DISABLED'}:
            env.pop(key, None)
    if not shutil.which('node', path=env.get('PATH', '')):
        for folder in ('/opt/homebrew/opt/node@22/bin', '/opt/homebrew/bin', '/usr/local/bin'):
            if os.access(str(Path(folder) / 'node'), os.X_OK):
                env['PATH'] = folder + os.pathsep + env.get('PATH', os.defpath)
                break
    return env


_RUNTIME_CHECKS = {}
_RUNTIME_CHECK_LOCK = threading.Lock()


def _desktop_candidates():
    if sys.platform != 'darwin':
        return []
    suffixes = ('ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex',
                'Codex.app/Contents/Resources/codex')
    return [str(root / suffix) for root in (Path('/Applications'), Path.home() / 'Applications')
            for suffix in suffixes]


def runtime_info(binary):
    """Non-secret runtime provenance and file identity for cache invalidation."""
    path = Path(binary)
    source = 'desktop_app' if '.app/Contents/' in path.as_posix() else 'cli'
    try:
        stat = path.stat()
        identity = (str(path.resolve()), stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)
    except OSError:
        identity = (str(path), None)
    return {'source': source, 'identity': identity}


def _desktop_server_available(binary, deadline=None):
    identity = runtime_info(binary)['identity']
    with deadline_lock(_RUNTIME_CHECK_LOCK, deadline):
        cached = _RUNTIME_CHECKS.get(identity)
        if cached and time.monotonic() - cached[0] < 30:
            return cached[1]
        try:
            result = subprocess.run([binary, 'app-server', '--help'], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=child_environment(),
                text=True, timeout=remaining_timeout(deadline, 5) if deadline is not None else 5,
                creationflags=process_creation_flags())
            available = result.returncode == 0 and '--listen' in result.stdout and 'stdio' in result.stdout
        except (OSError, subprocess.TimeoutExpired):
            available = False
        if len(_RUNTIME_CHECKS) >= 32:
            _RUNTIME_CHECKS.clear()
        _RUNTIME_CHECKS[identity] = (time.monotonic(), available)
        return available


def resolve_binary(settings=None, *, deadline=None):
    if deadline is not None:
        remaining_timeout(deadline)
    selected = (settings or {}).get('binary')
    if os.name == 'nt':
        # Windows may run a batch file through cmd.exe even with shell=False.
        # Only native executables receive app-server arguments and stdio input.
        if selected:
            if str(selected).lower().endswith(('.cmd', '.bat')):
                raise RpcError('codex_native_executable_required')
            candidates = [selected]
        else:
            candidates = list(dict.fromkeys(filter(None, [shutil.which('codex.exe'), shutil.which('codex')])))
        batch_found, native_found = False, False
        for candidate in candidates:
            if str(candidate).lower().endswith(('.cmd', '.bat')):
                batch_found = True
                continue
            if not isinstance(candidate, str) or not Path(candidate).is_absolute() or not os.access(candidate, os.X_OK):
                continue
            native_found = True
            if deadline is not None:
                remaining_timeout(deadline)
            if selected or _desktop_server_available(candidate, deadline):
                return candidate
        if native_found:
            raise RpcError('codex_process_unavailable')
        raise RpcError('codex_native_executable_required' if batch_found else 'codex_binary_unavailable')
    if not selected:
        for candidate in _desktop_candidates():
            if os.access(candidate, os.X_OK) and _desktop_server_available(candidate, deadline):
                return candidate
    if deadline is not None:
        remaining_timeout(deadline)
    candidates = [selected] if selected else [shutil.which('codex'),
        '/opt/homebrew/bin/codex', '/usr/local/bin/codex']
    for candidate in candidates:
        if isinstance(candidate, str) and Path(candidate).is_absolute() and os.access(candidate, os.X_OK):
            return candidate
    raise RpcError('codex_binary_unavailable')


class StdioRpc:
    MAX_LINE = 8 * 1024 * 1024
    WRITE_TIMEOUT = 10

    def __init__(self, binary, cwd, overrides=(), *, deadline=None):
        if os.name == 'nt' and str(binary).lower().endswith(('.cmd', '.bat')):
            raise RpcError('codex_native_executable_required')
        self.deadline = deadline
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.pending = {}
        self.next_id = 0
        self.closed = threading.Event()
        self.events = queue.Queue(maxsize=4096)
        self.writes = queue.Queue(maxsize=32)
        argv = [binary]
        for override in overrides:
            argv.extend(['-c', override])
        argv.extend(['app-server', '--listen', 'stdio://'])
        try:
            if deadline is not None:
                remaining_timeout(deadline)
            self.child = subprocess.Popen(argv, cwd=cwd, env=child_environment(),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0, close_fds=True, start_new_session=(os.name == 'posix'),
                creationflags=process_creation_flags())
        except OSError:
            raise RpcError('codex_process_unavailable')
        self.reader = threading.Thread(target=self._read, daemon=True, name='office-codex-rpc')
        self.stderr_reader = threading.Thread(target=self._discard_stderr, daemon=True,
                                               name='office-codex-stderr')
        self.writer = threading.Thread(target=self._write_loop, daemon=True, name='office-codex-write')
        self.reader.start()
        self.stderr_reader.start()
        self.writer.start()

    def _signal_owned(self, *, force=False):
        try:
            if os.name == 'posix':
                os.killpg(self.child.pid, signal.SIGKILL if force else signal.SIGTERM)
            elif force:
                self.child.kill()
            else:
                self.child.terminate()
        except ProcessLookupError:
            pass
        except PermissionError:
            # macOS can report EPERM for an already reaped process group.
            # Re-check our owned launcher before any PID-only fallback.
            if self.child.poll() is None:
                if force:
                    self.child.kill()
                else:
                    self.child.terminate()

    def _write_loop(self):
        while not self.closed.is_set():
            item = self.writes.get()
            if item is None:
                return
            try:
                view = memoryview(item['raw'])
                while view:
                    count = self.child.stdin.write(view)
                    if not count:
                        raise OSError('closed pipe')
                    view = view[count:]
                self.child.stdin.flush()
                item['ok'] = True
            except (OSError, ValueError):
                self._fail()
            finally:
                item['event'].set()

    def _discard_stderr(self):
        try:
            while self.child.stderr.read(16384):
                pass
        except (OSError, ValueError):
            pass

    def _fail(self):
        self.closed.set()
        try:
            self.writes.put_nowait(None)
        except queue.Full:
            pass
        with self.lock:
            for waiting in self.pending.values():
                waiting['event'].set()

    def _read(self):
        try:
            while not self.closed.is_set():
                raw = self.child.stdout.readline(self.MAX_LINE + 1)
                if not raw:
                    break
                if len(raw) > self.MAX_LINE:
                    break
                message = json.loads(raw)
                if not isinstance(message, dict):
                    break
                if 'id' in message and 'method' not in message:
                    with self.lock:
                        waiting = self.pending.get(message['id'])
                        if waiting is not None:
                            waiting['message'] = message
                            waiting['event'].set()
                elif isinstance(message.get('method'), str):
                    self.events.put_nowait(message)
        except (OSError, ValueError, TypeError, queue.Full):
            pass
        finally:
            self._fail()

    def _write(self, value):
        raw = (json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
        with deadline_lock(self.write_lock, self.deadline):
            if self.closed.is_set():
                raise RpcError('codex_process_closed')
            item = {'raw': raw, 'event': threading.Event(), 'ok': False}
            try:
                self.writes.put_nowait(item)
            except queue.Full:
                raise RpcError('codex_write_queue_full')
            # Once enqueued, expiry is an uncertain write, never an unsent RPC.
            budget = max(0, min(self.WRITE_TIMEOUT, self.deadline - time.monotonic())) if self.deadline is not None else self.WRITE_TIMEOUT
            if not item['event'].wait(budget) or not item['ok']:
                self._fail()
                if self.child.poll() is None:
                    self._signal_owned()
                # A partial write cannot be distinguished from accepted input.
                raise RpcError('codex_write_unknown', sent=True)

    def call(self, method, params, timeout=20):
        with self.lock:
            self.next_id += 1
            ident = self.next_id
            waiting = {'event': threading.Event(), 'message': None}
            self.pending[ident] = waiting
        sent = False
        try:
            self._write({'id': ident, 'method': method, 'params': params})
            sent = True
            budget = remaining_timeout(self.deadline, timeout) if self.deadline is not None else timeout
            if not waiting['event'].wait(budget):
                raise RpcError('codex_rpc_timeout', sent=True)
            response = waiting['message']
            if response is None:
                raise RpcError('codex_process_closed', sent=True)
            if 'error' in response:
                # Do not expose provider diagnostics; they may contain secrets.
                raise RpcError('codex_rpc_rejected', sent=True)
            if 'result' not in response:
                raise RpcError('codex_invalid_receipt', sent=True)
            return response['result']
        except RpcError as error:
            error.sent = error.sent or sent
            raise
        finally:
            with self.lock:
                self.pending.pop(ident, None)

    def notify(self, method, params=None):
        value = {'method': method}
        if params is not None:
            value['params'] = params
        self._write(value)

    def reply(self, ident, result=None, error=None):
        value = {'id': ident}
        value['error' if error is not None else 'result'] = error if error is not None else result
        self._write(value)

    def initialize(self):
        self.call('initialize', {'clientInfo': {'name': 'virtual_office',
            'title': 'Virtual Office', 'version': '1.0'},
            'capabilities': {'experimentalApi': False}}, timeout=15)
        self.notify('initialized')

    def close(self):
        self._fail()
        try:
            if self.child.poll() is None:
                self._signal_owned()
                try:
                    self.child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._signal_owned(force=True)
                    self.child.wait(timeout=2)
                # A launcher can exit before a child that ignored SIGTERM. This
                # group was allocated exclusively for our still-live launcher.
                if os.name == 'posix':
                    self._signal_owned(force=True)
        finally:
            for stream in (self.child.stdin, self.child.stdout, self.child.stderr):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

