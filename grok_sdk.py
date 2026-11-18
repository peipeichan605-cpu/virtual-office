"""Optional, selected-session Grok Bot SDK subprocess transport.

Authentication is a fresh human browser login. Credentials stay in this Office
installation and never cross subprocess stdout, command arguments or HTTP APIs.
The adapter sends once with a stable native message ID; transcript observation
is responsible for proving receipt, public output and terminal delivery.
"""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid

from command_adapters import DeliveryError

ROOT = Path(__file__).resolve().parent
SDK_VERSION = '0.78.1'
SDK_COMMIT = '42c75eb13f24630d1b689629502d4320c4219cdb'
WORKER = ROOT / 'grok-sdk' / 'worker.mjs'
_RUNTIME_FILES = ('sdk.mjs', 'core.mjs', 'avatar.mjs', 'errors.mjs', 'connect-stream.mjs', 'sse.mjs')
_MAX_OUTPUT = 20 * 1024 * 1024
_LOCK = threading.RLock()
_ROOT_LOCKS = {}
_RUNTIME_CACHE = {}


class GrokSdkError(Exception):
    def __init__(self, code, status=503, uncertain=False):
        self.code, self.status, self.uncertain = code, status, uncertain
        super().__init__(code)


def _environment():
    # In particular, never inherit NODE_OPTIONS, proxy/auth environment values
    # or a globally installed loader into the private transport process.
    return {k: v for k, v in os.environ.items()
            if k in {'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'SYSTEMROOT', 'WINDIR', 'TMPDIR', 'TEMP', 'TMP'}}


def _uuid(value):
    if not isinstance(value, str):
        raise GrokSdkError('grok_sdk_invalid_request_id', 400)
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except ValueError:
        raise GrokSdkError('grok_sdk_invalid_request_id', 400)
    return value


def _vendor_valid():
    directory = ROOT / 'vendor' / 'grok-bot-sdk'
    try:
        manifest = json.loads((directory / 'PROVENANCE.json').read_text())
        if manifest['commit'] != SDK_COMMIT:
            return False
        for name in _RUNTIME_FILES:
            source = directory / name
            expected = manifest.get('local_patches', {}).get(name, {}).get('sha256', manifest['files'][name]['sha256'])
            if source.is_symlink() or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def runtime_binary(settings=None):
    """Find Node >= 22.13 without installing or executing package scripts."""
    settings = settings or {}
    explicit = settings.get('node_binary')
    candidates = [explicit] if explicit else [shutil.which('node'),
        '/opt/homebrew/opt/node@22/bin/node', '/opt/homebrew/bin/node', '/usr/local/bin/node',
        str(Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'nodejs' / 'node.exe')]
    for value in candidates:
        if not value or not Path(value).is_absolute() or not os.access(value, os.X_OK):
            continue
        try:
            stat = Path(value).stat()
            identity = (value, stat.st_mtime_ns, stat.st_size)
            with _LOCK:
                cached = _RUNTIME_CACHE.get(identity)
            if cached is None:
                result = subprocess.run([value, '--version'], stdin=subprocess.DEVNULL,
                    capture_output=True, timeout=3, env=_environment(), text=True)
                match = re.fullmatch(r'v(\d+)\.(\d+)\.(\d+)\s*', result.stdout)
                cached = bool(result.returncode == 0 and match and tuple(map(int, match.groups())) >= (22, 13, 0))
                with _LOCK:
                    _RUNTIME_CACHE[identity] = cached
            if cached:
                return value
        except (OSError, subprocess.SubprocessError):
            pass
    raise GrokSdkError('grok_sdk_runtime_required')


def client_version(settings=None):
    """Public app metadata only; works while the App is closed or absent."""
    settings = settings or {}
    explicit = settings.get('client_version')
    if explicit is not None:
        if not isinstance(explicit, str) or not re.fullmatch(r'\d+\.\d+\.\d+(?:[-+.][A-Za-z0-9.-]+)?', explicit):
            raise GrokSdkError('grok_sdk_client_version_invalid')
        return explicit
    for p in (Path('/Applications/Grok Bot.app/Contents/Info.plist'),
              Path.home() / 'Applications/Grok Bot.app/Contents/Info.plist'):
        try:
            if p.stat().st_size > 262144:
                continue
            value = plistlib.loads(p.read_bytes()).get('CFBundleShortVersionString')
            if isinstance(value, str) and re.fullmatch(r'\d+\.\d+\.\d+(?:[-+.][A-Za-z0-9.-]+)?', value):
                return value
        except (OSError, ValueError, TypeError):
            pass
    return SDK_VERSION


class GrokSdkClient:
    def __init__(self, config, runner=None):
        self.config = config
        self.root = Path(config['data_dir']) / 'grok-sdk'
        self.credential_file = self.root / 'account.json'
        self.settings = config.get('command_adapters', {}).get('grok_sdk', {})
        self.runner = runner
        self._auth_cache = None
        self._auth_failure = None
        with _LOCK:
            self.lock = _ROOT_LOCKS.setdefault(str(self.root), threading.RLock())

    def _prepare(self):
        if not self.root.is_absolute() or self.root.is_symlink():
            raise GrokSdkError('grok_sdk_private_root_invalid')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.root.is_dir():
            raise GrokSdkError('grok_sdk_private_root_invalid')
        if os.name != 'nt':
            self.root.chmod(0o700)

    def _runtime(self):
        if not _vendor_valid() or not WORKER.is_file():
            raise GrokSdkError('grok_sdk_runtime_integrity')
        return runtime_binary(self.settings)

    def _request(self, action, **kwargs):
        return {'root': str(self.root), 'action': action, 'client_version': client_version(self.settings), **kwargs}

    @staticmethod
    def _decode(value, action):
        if not isinstance(value, dict) or value.get('ok') is not True:
            raw = value.get('error') if isinstance(value, dict) else None
            raw = raw if isinstance(raw, str) and re.fullmatch(r'[A-Z0-9_]{1,80}', raw) else 'WORKER_FAILED'
            status = value.get('http_status') if isinstance(value, dict) else None
            uncertain = action == 'send_message' and raw not in {'SEND_REFUSED', 'MESSAGE_INVALID', 'TARGET_REQUIRED', 'AUTH_REQUIRED', 'AUTH_EXPIRED'} and status not in (401, 403)
            if status == 401 or raw in {'AUTH_REQUIRED', 'AUTH_EXPIRED', 'ACCOUNT_AUTH_FAILED'}:
                raise GrokSdkError('grok_sdk_login_required', 401, uncertain=False)
            if status == 403:
                raise GrokSdkError('grok_sdk_access_denied', 403, uncertain=False)
            raise GrokSdkError('grok_sdk_' + raw.lower(), 503, uncertain=uncertain)
        result = value.get('result')
        if not isinstance(result, dict):
            raise GrokSdkError('grok_sdk_invalid_response', uncertain=action == 'send_message')
        return result

    def _call(self, action, timeout=30, **kwargs):
        self._prepare()
        request = self._request(action, **kwargs)
        try:
            if self.runner:
                result = self.runner(request)
            else:
                process = subprocess.run([self._runtime(), str(WORKER)],
                    input=json.dumps(request), capture_output=True, text=True,
                    timeout=timeout, env=_environment())
                if len(process.stdout.encode()) > _MAX_OUTPUT:
                    raise GrokSdkError('grok_sdk_invalid_response', uncertain=action == 'send_message')
                lines = process.stdout.splitlines()
                if len(lines) != 1:
                    raise GrokSdkError('grok_sdk_invalid_response', uncertain=action == 'send_message')
                try:
                    result = json.loads(lines[0])
                except ValueError:
                    raise GrokSdkError('grok_sdk_invalid_response', uncertain=action == 'send_message')
            return self._decode(result, action)
        except (OSError, subprocess.SubprocessError):
            raise GrokSdkError('grok_sdk_delivery_unknown' if action == 'send_message' else 'grok_sdk_transport_unavailable', uncertain=action == 'send_message')
        except GrokSdkError as error:
            if error.code == 'grok_sdk_login_required':
                self._auth_failure = 'grok_sdk_login_required'
                self._auth_cache = None
            raise

    def auth_status(self, refresh=False):
        """Local metadata only. Authenticated is not task-delivery proof."""
        try:
            self._prepare()
            stamps = []
            for name in ('account.json', 'auth-state.json', 'latest-login.json'):
                file = self.root / name
                if file.is_symlink():
                    raise GrokSdkError('grok_sdk_private_file_invalid')
                try:
                    st = file.stat(); stamps.append((name, st.st_mtime_ns, st.st_size))
                except FileNotFoundError:
                    stamps.append((name, None))
            key = tuple(stamps)
            if not refresh and self._auth_cache and self._auth_cache[0] == key and time.monotonic() < self._auth_cache[1]:
                return dict(self._auth_cache[2])
            result = self._call('auth_status', timeout=5)
            result.update(available=True, experimental=True, sdk_version=SDK_VERSION,
                          client_version=client_version(self.settings))
            self._auth_cache = (key, time.monotonic() + 20, result)
            if result.get('state') == 'authenticated':
                self._auth_failure = None
            return dict(result)
        except GrokSdkError as error:
            return {'state': 'unavailable', 'available': False, 'reason': error.code,
                    'experimental': True, 'sdk_version': SDK_VERSION}

    def start_login(self, request_id):
        with self.lock:
            result = self._call('login_start', request_id=_uuid(request_id), timeout=5)
            self._auth_cache = None
            return result

    def login_status(self, login_id):
        with self.lock:
            return self._call('login_status', login_id=_uuid(login_id), timeout=5)

    def poll_login(self, login_id):
        with self.lock:
            result = self._call('login_poll', login_id=_uuid(login_id), timeout=45)
            self._auth_cache = None
            return result

    def cancel_login(self, login_id):
        with self.lock:
            result = self._call('login_cancel', login_id=_uuid(login_id), timeout=5)
            self._auth_cache = None
            return result

    def list_bots(self):
        return self._call('list_bots')

    def list_sessions(self, bot_id):
        return self._call('list_sessions', bot_id=bot_id)

    def list_transcript(self, bot_id, session_id, limit=100, before_seq=None, generation=None):
        return self._call('list_transcript', bot_id=bot_id, session_id=session_id, limit=limit,
            **({'before_seq': before_seq} if before_seq is not None else {}),
            **({'generation': generation} if generation is not None else {}))

    def agent_statuses(self, ids):
        return self._call('agent_statuses', bot_ids=ids)

    def get_send_status(self, bot_id, session_id, message_id):
        return self._call('get_send_status', bot_id=bot_id, session_id=session_id, message_id=message_id)

    def list_tasks(self, bot_id, session_id):
        return self._call('list_tasks', bot_id=bot_id, session_id=session_id)

    def list_agent_tasks(self, bot_id, include_recently_done=True):
        return self._call('list_agent_tasks', bot_id=bot_id, include_recently_done=include_recently_done)

    def list_subagents(self, bot_id):
        return self._call('list_subagents', bot_id=bot_id)

    def send_message(self, bot_id, session_id, message_id, text):
        return self._call('send_message', bot_id=bot_id, session_id=session_id, message_id=_uuid(message_id), text=text)

    def sensitive_values(self):
        """Return only this adapter's own saved secrets for local redaction."""
        result = []
        try:
            p = self.credential_file
            if p.is_symlink() or not p.is_file() or p.stat().st_size > 65536:
                return result
            value = json.loads(p.read_text())
            for key in ('accessToken', 'refreshToken'):
                if isinstance(value.get(key), str) and value[key]:
                    result.append(value[key])
        except (OSError, ValueError, TypeError):
            pass
        return result

    redaction_secrets = sensitive_values

    def watch(self, bot_id, session_id, resume_from=None, stop_event=None):
        """Yield selected native events; callers own durable cursors/reconnect."""
        stop = stop_event or threading.Event()
        self._prepare()
        request = self._request('watch', bot_id=bot_id, session_id=session_id,
            **({'resume_from': resume_from} if resume_from else {}))
        process = None
        messages = queue.Queue(maxsize=16)
        reader_stop = threading.Event()
        try:
            process = subprocess.Popen([self._runtime(), str(WORKER)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_environment())
            process.stdin.write(json.dumps(request).encode()); process.stdin.close()
            def read():
                try:
                    while not reader_stop.is_set():
                        line = process.stdout.readline(_MAX_OUTPUT + 1)
                        if not line:
                            item = None
                        elif len(line) > _MAX_OUTPUT:
                            item = {'ok': False, 'error': 'OUTPUT_TOO_LARGE'}
                        else:
                            try: item = json.loads(line)
                            except ValueError: item = {'ok': False, 'error': 'INVALID_RESPONSE'}
                        while not reader_stop.is_set():
                            try: messages.put(item, timeout=0.2); break
                            except queue.Full: continue
                        if item is None or len(line) > _MAX_OUTPUT: break
                except (OSError, ValueError):
                    pass
            reader = threading.Thread(target=read, daemon=True, name='grok-sdk-watch-reader')
            reader.start()
            while not stop.is_set():
                try: item = messages.get(timeout=0.2)
                except queue.Empty:
                    if process.poll() is not None and not reader.is_alive():
                        raise GrokSdkError('grok_sdk_stream_closed')
                    continue
                if item is None:
                    raise GrokSdkError('grok_sdk_stream_closed')
                if isinstance(item, dict) and isinstance(item.get('event'), dict):
                    yield item['event']
                else:
                    self._decode(item, 'watch')
                    raise GrokSdkError('grok_sdk_stream_closed')
        except OSError:
            raise GrokSdkError('grok_sdk_transport_unavailable')
        finally:
            reader_stop.set()
            if process:
                if process.poll() is None:
                    process.terminate()
                    try: process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait(timeout=2)
                if process.stdout: process.stdout.close()


class GrokSdkAdapter:
    mode, serial, same_session = 'grok_sdk', True, True

    def __init__(self, config, on_event=None, client=None):
        self.config, self.emit = config, on_event
        self.client = client or GrokSdkClient(config)
        self.sessions = config.get('command_adapters', {}).get('grok_sdk', {}).get('sessions', {})
        self.reason = 'grok_sdk_login_required'

    def _target(self, sid):
        item = self.sessions.get(sid)
        if (not isinstance(item, dict) or not isinstance(item.get('bot_id'), str)
                or not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}', item['bot_id'])
                or 'native_session_id' not in item or not isinstance(item['native_session_id'], str)
                or len(item['native_session_id']) > 256
                or any(ord(c) < 32 or ord(c) == 127 for c in item['native_session_id'])
                or not isinstance(item.get('account_fingerprint'), str)
                or not re.fullmatch(r'[a-f0-9]{64}', item['account_fingerprint'])):
            raise GrokSdkError('grok_sdk_target_not_selected')
        return item

    def available(self, sid):
        try:
            item = self._target(sid)
            state = self.client.auth_status()
            if state.get('state') != 'authenticated':
                self.reason = state.get('reason') or 'grok_sdk_login_required'
                return False
            if state.get('account_fingerprint') != item['account_fingerprint']:
                self.reason = 'grok_sdk_account_changed'
                return False
            self.reason = None
            return True
        except GrokSdkError as error:
            self.reason = error.code
            return False

    def native_verified(self, sid):
        return self.available(sid)

    def submit(self, sid, text):
        match = re.match(r'^\[OFFICE:([0-9a-f-]{36})\]\n', text) if isinstance(text, str) else None
        if not match or not self.available(sid):
            raise DeliveryError(self.reason or 'grok_sdk_invalid_command')
        try:
            item = self._target(sid)
            message_id = _uuid(match[1])
            receipt = self.client.send_message(item['bot_id'], item['native_session_id'], message_id, text)
            if (receipt.get('botId') != item['bot_id'] or receipt.get('messageId') != message_id
                    or receipt.get('delivery') not in {'accepted-box', 'accepted-temporal', 'duplicate'}):
                raise GrokSdkError('grok_sdk_delivery_unknown', uncertain=True)
            return {'native_message_id': message_id, 'delivery_mode': self.mode,
                    'attention_code': 'awaiting_native_confirmation'}
        except GrokSdkError as error:
            self.reason = error.code
            raise DeliveryError(error.code, uncertain=error.uncertain)
