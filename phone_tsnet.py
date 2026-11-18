#!/usr/bin/env python3
"""Explicit lifecycle for the optional, isolated userspace phone frontend.

Importing this module does nothing. It never invokes a system Tailscale client,
changes VPN/DNS/routes/firewalls, copies credentials, or signals process groups.
Authentication details are retained only in a private worker status file.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
MIN_GO = (1, 27, 4)
TS_VERSION = 'v1.104.7'
MAX_JSON = 1024 * 1024
MAX_STATUS_AGE_SECONDS = 30
RUN_ID = re.compile(r'phone-tsnet-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}\Z')
PHASES = {'starting', 'login_required', 'device_approval_required', 'authenticated',
          'ready', 'stopped', 'error'}
_LOCAL_CHILDREN = {}


class FrontendError(Exception):
    """Public error codes only; raw child output must not escape this boundary."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _path(value):
    if (not isinstance(value, (str, Path)) or not str(value) or
            any(ord(c) < 32 or ord(c) == 127 for c in str(value))):
        raise FrontendError('invalid_private_path')
    p = Path(value).expanduser().absolute()
    if any(part == '..' for part in p.parts):
        raise FrontendError('invalid_private_path')
    for item in (p, *p.parents):
        if item.is_symlink():
            raise FrontendError('private_path_symlink')
    return p


def _owned(info):
    return not hasattr(os, 'getuid') or info.st_uid == os.getuid()


def _directory(path, *, create=False):
    path = _path(path)
    if create and not path.exists():
        path.mkdir(mode=0o700)
    try:
        info = path.lstat()
    except OSError:
        raise FrontendError('private_directory_missing') from None
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or not _owned(info):
        raise FrontendError('private_directory_required')
    return path


def _read_json(path):
    path = _path(path)
    _directory(path.parent)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or not _owned(info):
                raise FrontendError('private_file_required')
            data = file.read(MAX_JSON + 1)
        if len(data) > MAX_JSON:
            raise FrontendError('private_file_too_large')
        value = json.loads(data)
        if not isinstance(value, dict):
            raise FrontendError('invalid_private_record')
        return value
    except FrontendError:
        raise
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise FrontendError('private_record_unavailable') from None


def _write_json(path, data):
    path = _path(path)
    _directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            os.fchmod(file.fileno(), 0o600)
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write('\n')
            file.flush()
            os.fsync(file.fileno())
        # Never follow a caller-controlled destination symlink.
        if path.is_symlink():
            raise FrontendError('private_path_symlink')
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _private_file(path, mode=0o600):
    path = _path(path)
    _directory(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), mode)
    return os.fdopen(fd, 'ab', buffering=0)


def _root(config):
    data = _directory(config.get('data_dir', ''))
    return _directory(data / 'phone-tsnet', create=True)


@contextmanager
def _lock(root):
    # Windows needs handle-based process identity/locking; do not guess with a
    # PID there. The Go frontend itself is portable, this launcher is POSIX.
    if os.name != 'posix':
        raise FrontendError('lifecycle_platform_not_supported')
    import fcntl
    path = _path(root / 'lifecycle.lock')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or not _owned(info):
            raise FrontendError('private_file_required')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FrontendError('frontend_lifecycle_busy') from None
        yield
    finally:
        os.close(fd)


def child_environment(*, build=False, cache_root=None):
    """Preserve the host's routing/proxies, not unrelated provider credentials."""
    names = {'HOME', 'USER', 'LOGNAME', 'PATH', 'LANG', 'TMPDIR', 'TEMP', 'TMP',
             'SYSTEMROOT', 'WINDIR', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
             'NO_PROXY', 'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'}
    result = {k: v for k, v in os.environ.items() if k in names or k.startswith('LC_')}
    # Do not inherit TS_AUTHKEY/trust/control/debug configuration. In particular,
    # userspace operation must not try UPnP/NAT-PMP router configuration.
    result['TS_DISABLE_PORTMAPPER'] = 'true'
    result['TS_NO_LOGS_NO_SUPPORT'] = 'true'
    result['GOMAXPROCS'] = '2'
    if build:
        result.update(GOTOOLCHAIN='local', GOENV='off', GOWORK='off', CGO_ENABLED='0',
                      GOPROXY='https://proxy.golang.org', GOSUMDB='sum.golang.org')
        if cache_root is not None:
            result.update(GOCACHE=str(cache_root / 'build'), GOMODCACHE=str(cache_root / 'modules'))
    return result


def _new_run(root, action):
    runs = _directory(root / 'runs', create=True)
    ident = 'phone-tsnet-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:12]
    run = _directory(runs / ident, create=True)
    _write_json(run / 'request.json', {'version': 1, 'run_id': ident, 'action': action, 'created_at': _now()})
    _write_json(run / 'result.json', {'run_id': ident, 'state': 'prepared'})
    return run


def _sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _binary(path):
    path = _path(path)
    try:
        info = path.stat()
    except OSError:
        raise FrontendError('frontend_binary_missing') from None
    if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
        raise FrontendError('invalid_frontend_binary')
    return path


def _process_identity(pid, config_path, binary):
    """Match UID, start and exact argv hash, including the unique private config.

    This is a fail-closed POSIX identity check, not an atomic macOS pid handle.
    The lifecycle lock and unique config pathname prevent ordinary PID reuse
    from becoming a stop target. No environment or other process is inspected.
    """
    if type(pid) is not int or pid <= 1 or os.name != 'posix':
        return None
    try:
        result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'uid=', '-o', 'lstart=', '-o', 'args='],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, timeout=2, env={**child_environment(), 'LC_ALL': 'C'})
        line = result.stdout.strip()
        match = re.fullmatch(r'(\d+)\s+([A-Za-z]{3}\s+[A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s+(.+)', line)
        if result.returncode or not match or int(match[1]) != os.getuid():
            return None
        command = match[3]
        if str(binary) not in command or not command.endswith('--config ' + str(config_path)):
            return None
        return {'uid': int(match[1]), 'started': match[2],
                'command_sha256': hashlib.sha256(command.encode()).hexdigest()}
    except (OSError, subprocess.SubprocessError):
        return None


def _active(root):
    if not (root / 'active.json').exists():
        return None
    record = _read_json(root / 'active.json')
    ident = record.get('run_id')
    if not isinstance(ident, str) or not RUN_ID.fullmatch(ident):
        raise FrontendError('invalid_active_record')
    run = _directory(root / 'runs' / ident)
    if (record.get('config_path') != str(run / 'config.json') or
            record.get('status_path') != str(run / 'worker-status.json') or
            record.get('mode') not in {'login', 'serve'} or
            type(record.get('pid')) is not int or record['pid'] <= 1 or
            not isinstance(record.get('process_identity'), dict)):
        raise FrontendError('invalid_active_record')
    binary = _binary(record.get('binary', ''))
    if binary.parent != root / 'bin' or not re.fullmatch(r'office-phone-[0-9a-f]{16}', binary.name):
        raise FrontendError('invalid_active_record')
    return record


def _is_running(record):
    # Reap only handles created by this Python process. This prevents zombies
    # in a long-running caller; recovered workers still require full identity.
    if record:
        child = _LOCAL_CHILDREN.get(record.get('run_id'))
        if child is not None and child.poll() is not None:
            _LOCAL_CHILDREN.pop(record['run_id'], None)
    return bool(record and record.get('process_identity') and
                _process_identity(record.get('pid'), record['config_path'], record['binary']) == record['process_identity'])


def _pid_absent(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (OSError, TypeError):
        pass
    return False


def _fresh_status(worker):
    try:
        updated = datetime.fromisoformat(worker['updated_at'].replace('Z', '+00:00'))
        if updated.tzinfo is None:
            return False
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        return -5 <= age <= MAX_STATUS_AGE_SECONDS
    except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
        return False


def _cleanup_launch(process, run, root=None):
    """Reap our exact new child if persistence or a caller callback fails."""
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
    except (OSError, subprocess.SubprocessError):
        # Preserve any existing receipt when positive cleanup is unavailable.
        raise FrontendError('frontend_cleanup_incomplete') from None
    _LOCAL_CHILDREN.pop(run.name, None)
    if root is not None and (root / 'active.json').exists():
        try:
            active = _read_json(root / 'active.json')
            if active.get('run_id') == run.name and active.get('pid') == process.pid:
                (root / 'active.json').unlink()
        except (OSError, FrontendError):
            pass
    try:
        result = {'run_id': run.name, 'pid': process.pid, 'state': 'failed',
                  'code': 'frontend_initialization_failed', 'stopped': True}
        _write_json(run / 'result.json', result)
        _write_json(run / 'status.json', result)
    except (OSError, FrontendError):
        # Disk failure may be why initialization failed. Never mask the error
        # or let the absence of an audit write leave a live owned child.
        pass


def validate_settings(config, *, mode, hostname, origin=None, identity=None, node_ids=None):
    if mode not in {'login', 'serve'}:
        raise FrontendError('invalid_frontend_mode')
    if not isinstance(hostname, str) or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', hostname):
        raise FrontendError('invalid_frontend_hostname')
    core = config.get('port', 4318)
    mobile = config.get('mobile', {})
    if not isinstance(mobile, dict):
        raise FrontendError('invalid_mobile_configuration')
    port = mobile.get('port', core + 1) if type(core) is int else None
    if type(core) is not int or type(port) is not int or not 1024 <= core <= 65535 or not 1024 <= port <= 65535 or port == core:
        raise FrontendError('invalid_frontend_ports')
    result = {'mode': mode, 'hostname': hostname, 'backend_port': port, 'core_port': core}
    if mode == 'login':
        if origin is not None or identity is not None or node_ids:
            raise FrontendError('login_mode_has_no_remote_listener')
        return result
    if (not isinstance(identity, str) or not 1 <= len(identity) <= 254 or
            any(ord(c) < 33 or ord(c) == 127 for c in identity)):
        raise FrontendError('invalid_expected_identity')
    try:
        p = urlsplit(origin or '')
        if (p.scheme != 'https' or p.netloc != p.hostname or p.path or p.query or p.fragment or
                not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+ts\.net', p.hostname or '')):
            raise FrontendError('invalid_expected_origin')
    except ValueError:
        raise FrontendError('invalid_expected_origin') from None
    if (not isinstance(node_ids, (list, tuple)) or not 1 <= len(node_ids) <= 16 or
            any(not isinstance(x, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', x) for x in node_ids) or
            len(set(node_ids)) != len(node_ids)):
        raise FrontendError('invalid_allowed_nodes')
    result.update(expected_origin=origin, expected_identity=identity, allowed_node_ids=list(node_ids))
    return result


def build_frontend(config, go_binary, *, on_started=None, timeout=600):
    """Build only after an explicit call; never install or download Go itself."""
    root = _root(config)
    go = _binary(go_binary)
    module = ROOT / 'phone-tsnet'
    if not (module / 'go.mod').is_file() or not (module / 'go.sum').is_file():
        raise FrontendError('frontend_pinned_sources_missing')
    text = (module / 'go.mod').read_text()
    if not re.search(r'(?m)^\s*(?:require\s+)?tailscale\.com\s+' + re.escape(TS_VERSION) + r'\s*$', text) or re.search(r'(?m)^replace\b', text):
        raise FrontendError('frontend_dependency_pin_mismatch')
    cache = _directory(root / 'go-cache', create=True)
    env = child_environment(build=True, cache_root=cache)
    try:
        version = subprocess.run([str(go), 'version'], stdin=subprocess.DEVNULL, capture_output=True,
                                 text=True, timeout=5, env=env)
        match = re.fullmatch(r'go version go(\d+)\.(\d+)\.(\d+) [a-z0-9]+/[a-z0-9]+\s*', version.stdout)
        if version.returncode or not match or tuple(map(int, match.groups())) < MIN_GO:
            raise FrontendError('go_1_27_4_or_newer_required')
    except (OSError, subprocess.SubprocessError):
        raise FrontendError('go_runtime_unavailable') from None
    with _lock(root):
        run = _new_run(root, 'build')
        output = run / 'office-phone.build'
        argv = [str(go), 'build', '-p=2', '-tags=ts_omit_peerapiserver', '-mod=readonly', '-trimpath', '-o', str(output), '.']
        with _private_file(run / 'stdout.log') as log:
            process = subprocess.Popen(argv, cwd=module, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env)
            try:
                record = {'run_id': run.name, 'state': 'building', 'pid': process.pid,
                          'log_path': str(run / 'stdout.log'), 'exact_stop_pid': process.pid}
                _write_json(run / 'status.json', record)
                if on_started:
                    on_started(record.copy())
                try:
                    code = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        # This handle is our still-live build child, never a PID
                        # recovered from a prior invocation or a process group.
                        process.kill()
                        process.wait(timeout=3)
                    code = None
            except BaseException:
                _cleanup_launch(process, run)
                raise
        if code != 0 or not output.is_file():
            result = {'run_id': run.name, 'state': 'failed', 'code': 'build_timeout' if code is None else 'build_failed', 'log_path': str(run / 'stdout.log')}
            _write_json(run / 'result.json', result)
            _write_json(run / 'status.json', result)
            raise FrontendError(result['code'])
        digest = _sha(output)
        directory = _directory(root / 'bin', create=True)
        target = directory / ('office-phone-' + digest[:16])
        if target.exists():
            if target.is_symlink() or _sha(target) != digest:
                raise FrontendError('frontend_binary_conflict')
            output.unlink()
        else:
            os.chmod(output, 0o700)
            os.replace(output, target)
        manifest = {'version': 1, 'binary': str(target), 'sha256': digest, 'tailscale_version': TS_VERSION, 'built_at': _now()}
        _write_json(root / 'binary.json', manifest)
        result = {'run_id': run.name, 'state': 'built', 'binary_path': str(target), 'sha256': digest, 'tailscale_version': TS_VERSION}
        _write_json(run / 'result.json', result)
        _write_json(run / 'status.json', result)
        return result


def start_frontend(config, *, mode='login', hostname='virtual-office', origin=None,
                   identity=None, node_ids=None, on_started=None):
    settings = validate_settings(config, mode=mode, hostname=hostname, origin=origin, identity=identity, node_ids=node_ids)
    root = _root(config)
    with _lock(root):
        previous = _active(root)
        if previous and _is_running(previous):
            raise FrontendError('frontend_already_running')
        if previous and previous.get('pid'):
            try:
                os.kill(previous['pid'], 0)
            except ProcessLookupError:
                pass
            except (OSError, TypeError):
                raise FrontendError('previous_frontend_identity_uncertain') from None
            else:
                raise FrontendError('previous_frontend_identity_uncertain')
        manifest = _read_json(root / 'binary.json')
        binary = _binary(manifest.get('binary', ''))
        if binary.parent != root / 'bin' or _sha(binary) != manifest.get('sha256') or manifest.get('tailscale_version') != TS_VERSION:
            raise FrontendError('frontend_binary_integrity_failed')
        node = root / 'node-settings.json'
        if node.exists() and _read_json(node).get('hostname') != hostname:
            raise FrontendError('frontend_hostname_requires_explicit_new_state')
        if not node.exists():
            _write_json(node, {'hostname': hostname})
        state = _directory(root / 'state', create=True)
        run = _new_run(root, mode)
        worker_config = {**settings, 'state_dir': str(state), 'status_path': str(run / 'worker-status.json')}
        _write_json(run / 'config.json', worker_config)
        with _private_file(run / 'stdout.log') as log:
            process = subprocess.Popen([str(binary), '--config', str(run / 'config.json')],
                                       cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       env=child_environment(), start_new_session=True)
        _LOCAL_CHILDREN[run.name] = process
        try:
            record = {'version': 1, 'run_id': run.name, 'mode': mode, 'pid': process.pid,
                      'binary': str(binary), 'binary_sha256': manifest['sha256'],
                      'config_path': str(run / 'config.json'), 'status_path': str(run / 'worker-status.json'),
                      'started_at': _now(), 'process_identity': None}
            for _ in range(20):
                if process.poll() is not None:
                    break
                record['process_identity'] = _process_identity(process.pid, record['config_path'], binary)
                if record['process_identity']:
                    break
                time.sleep(0.025)
            if not record['process_identity']:
                # A live handle belongs to this exact freshly spawned child; stop
                # it before refusing to leave an unidentifiable background worker.
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
                _LOCAL_CHILDREN.pop(run.name, None)
                result = {'run_id': run.name, 'state': 'failed', 'code': 'frontend_start_failed', 'pid': process.pid}
                _write_json(run / 'result.json', result)
                _write_json(run / 'status.json', result)
                raise FrontendError('frontend_start_failed')
            _write_json(root / 'active.json', record)
            _write_json(run / 'status.json', {'run_id': run.name, 'state': 'started', 'pid': process.pid})
            result = _public_status(root, record)
            _write_json(run / 'result.json', result)
            if on_started:
                on_started(result.copy())
            return result
        except BaseException:
            _cleanup_launch(process, run, root)
            raise


def _public_status(root, record):
    if not record:
        return {'state': 'not_started', 'phase': 'not_started', 'auth_url_available': False,
                'listening': False, 'end_to_end_verified': False}
    running = _is_running(record)
    state = 'starting' if running else ('not_running' if _pid_absent(record['pid']) else 'process_identity_unknown')
    result = {'run_id': record['run_id'], 'pid': record['pid'], 'mode': record['mode'],
              'running': running, 'state': state,
              'private_status_path': record['status_path'], 'listening': False, 'auth_url_available': False,
              'end_to_end_verified': False,
              'stop_command': 'phone_tsnet.py --config <office-config> stop --run-id ' + record['run_id']}
    if running and Path(record['status_path']).exists():
        try:
            worker = _read_json(record['status_path'])
            phase = worker.get('phase')
            if (worker.get('version') != 1 or worker.get('pid') != record['pid'] or phase not in PHASES or
                    (phase == 'ready' and record['mode'] != 'serve')):
                result['state'] = 'status_unavailable'
            elif not _fresh_status(worker):
                result['state'] = 'status_stale'
            else:
                result['state'] = phase
                result['listening'] = record['mode'] == 'serve' and phase == 'ready'
                # Only a boolean is public. The one-time URL remains inside the
                # owned 0600 worker-status file for a trusted local UI to open.
                auth_url = worker.get('auth_url')
                if phase == 'login_required' and isinstance(auth_url, str):
                    try:
                        url = urlsplit(auth_url)
                        result['auth_url_available'] = (url.scheme == 'https' and
                            url.netloc == 'login.tailscale.com' and bool(url.path) and
                            not any(ord(c) < 33 or ord(c) == 127 for c in auth_url))
                    except ValueError:
                        pass
                if phase == 'error' and isinstance(worker.get('code'), str) and re.fullmatch(r'[a-z0-9_]{1,80}', worker['code']):
                    result['code'] = worker['code']
        except FrontendError:
            result['state'] = 'status_unavailable'
    result['phase'] = result['state']
    return result


def frontend_status(config):
    root = _root(config)
    with _lock(root):
        return _public_status(root, _active(root))


def stop_frontend(config, *, run_id=None):
    root = _root(config)
    with _lock(root):
        record = _active(root)
        if not record:
            return {'state': 'not_started', 'stopped': True}
        if run_id != record['run_id']:
            raise FrontendError('exact_run_id_required')
        if not _is_running(record):
            try:
                os.kill(record['pid'], 0)
            except ProcessLookupError:
                child = _LOCAL_CHILDREN.pop(run_id, None)
                if child is not None:
                    child.poll()
                (root / 'active.json').unlink()
                return {'run_id': run_id, 'state': 'not_running', 'stopped': True}
            except OSError:
                pass
            raise FrontendError('frontend_process_identity_mismatch')
        # Recheck immediately before signalling; never use a group or killall.
        if not _is_running(record):
            raise FrontendError('frontend_process_identity_mismatch')
        os.kill(record['pid'], signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _is_running(record):
            time.sleep(0.05)
        # A failed/timed-out ps read is not proof that the worker exited.
        # Reap our own child handle, then require positive PID absence before
        # discarding the active receipt. Keep uncertain identities fail closed.
        _is_running(record)
        stopped = _pid_absent(record['pid'])
        result = {'run_id': run_id, 'pid': record['pid'], 'state': 'stopped' if stopped else 'stop_requested', 'stopped': stopped}
        run = root / 'runs' / run_id
        _write_json(run / 'result.json', result)
        _write_json(run / 'status.json', result)
        if stopped:
            child = _LOCAL_CHILDREN.pop(run_id, None)
            if child is not None:
                child.poll()
            (root / 'active.json').unlink()
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='Existing private OFFICE configuration; it is never modified')
    sub = parser.add_subparsers(dest='action', required=True)
    build = sub.add_parser('build'); build.add_argument('--go', required=True)
    login = sub.add_parser('login'); login.add_argument('--hostname', default='virtual-office')
    serve = sub.add_parser('serve'); serve.add_argument('--hostname', default='virtual-office')
    serve.add_argument('--origin', required=True); serve.add_argument('--identity', required=True)
    serve.add_argument('--node', action='append', required=True, help='Exact approved phone stable node ID')
    sub.add_parser('status')
    stop = sub.add_parser('stop'); stop.add_argument('--run-id', required=True)
    args = parser.parse_args()
    from configuration import load_config
    config = load_config(_path(args.config))
    emit = lambda value: print(json.dumps(value), flush=True)
    if args.action == 'build':
        result = build_frontend(config, args.go, on_started=emit)
    elif args.action in {'login', 'serve'}:
        result = start_frontend(config, mode=args.action, hostname=args.hostname,
                                origin=getattr(args, 'origin', None), identity=getattr(args, 'identity', None),
                                node_ids=getattr(args, 'node', None))
    elif args.action == 'stop':
        result = stop_frontend(config, run_id=args.run_id)
    else:
        result = frontend_status(config)
    emit(result)


if __name__ == '__main__':
    try:
        main()
    except FrontendError as error:
        print(json.dumps({'state': 'error', 'code': str(error)}))
        raise SystemExit(1)
    except (OSError, ValueError, TypeError, KeyError):
        print(json.dumps({'state': 'error', 'code': 'frontend_operation_failed'}))
        raise SystemExit(1)
