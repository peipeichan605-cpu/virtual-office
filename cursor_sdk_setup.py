"""Optional per-install Cursor SDK environment; never changes system Python."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

SDK_VERSION = '1.3.1'
MANAGER = 'virtual-office-cursor-sdk'
REQUIREMENTS = Path(__file__).with_name('requirements-cursor-sdk.txt')
PROBE = '''import json,sys,ssl,sqlite3,venv,plistlib,platform
assert sys.version_info >= (3,10)
assert sys.platform != 'darwin' or platform.mac_ver()[0]
print(json.dumps({'version':list(sys.version_info[:3]),'executable':sys.executable}))
'''
SDK_PROBE = PROBE + '''import importlib.metadata,cursor_sdk
assert importlib.metadata.version('cursor-sdk') == '1.3.1'
'''


def runtime_root(config):
    return Path(config['data_dir']).expanduser().absolute() / 'cursor-sdk-runtime'


def venv_python(environment):
    return environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def probe_interpreter(binary, sdk=False):
    """Import-only health check: no bridge, account, model or credential access."""
    try:
        result = subprocess.run([str(binary), '-I', '-c', SDK_PROBE if sdk else PROBE],
                                stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=15)
        if result.returncode:
            return None
        data = json.loads(result.stdout.splitlines()[0])
        return data if data.get('version', [0]) >= [3, 10] else None
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def _owned_python(config):
    root = runtime_root(config)
    try:
        marker = json.loads((root / 'runtime.json').read_text())
        if marker.get('managed_by') != MANAGER or marker.get('sdk_version') != SDK_VERSION:
            return None
        relative = marker.get('environment')
        if not isinstance(relative, str) or not re.fullmatch(r'environments/env-[a-f0-9]{32}', relative):
            return None
        environment = root / relative
        if environment.is_symlink() or environment.parent.is_symlink():
            return None
        if not (environment / 'pyvenv.cfg').is_file():
            return None
        return venv_python(environment)
    except (OSError, ValueError, AttributeError):
        return None


def service_python(config, fallback=None):
    """Use an import-verified SDK env only; absent/broken optional env keeps core usable."""
    candidate = _owned_python(config)
    if candidate and probe_interpreter(candidate, sdk=True):
        # Do not resolve this symlink to its base Python: that loses the venv.
        return str(candidate)
    return str(fallback or sys.executable)


def _atomic_json(path, data):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(data, output, indent=2)
            output.write('\n')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def setup_environment(config, python=None, download_python=False, on_started=None):
    """Explicit setup creates a new private env; existing environments are never overwritten."""
    root = runtime_root(config)
    candidate = _owned_python(config)
    if candidate and probe_interpreter(candidate, sdk=True):
        return {'state': 'ready', 'sdk_version': SDK_VERSION, 'python': str(candidate),
                'reused': True, 'restart_required': True}
    marker_path = root / 'runtime.json'
    if marker_path.exists():
        try:
            owned = json.loads(marker_path.read_text()).get('managed_by') == MANAGER
        except (OSError, ValueError, AttributeError):
            owned = False
        if not owned:
            raise ValueError('Cursor SDK runtime metadata is not owned by this installer; existing files preserved.')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    runs = root / 'setup-runs'
    runs.mkdir(exist_ok=True, mode=0o700)
    run_id = 'setup-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:8]
    run = runs / run_id
    run.mkdir(mode=0o700)
    log_path, status_path = run / 'stdout.log', run / 'status.json'
    record = {'run_id': run_id, 'state': 'starting', 'pid': os.getpid(),
              'stop_target': '/bin/kill -TERM ' + str(os.getpid()), 'log': str(log_path)}
    _atomic_json(run / 'request.json', {'action': 'setup-cursor-sdk', 'sdk_version': SDK_VERSION,
                                      'download_python': bool(download_python)})
    _atomic_json(status_path, record)
    if on_started:
        on_started(dict(record))

    def execute(command, timeout=180):
        with log_path.open('ab') as log:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                     stderr=subprocess.STDOUT)
            record.update(state='running', child_pid=child.pid, command=command,
                          child_stop_target='/bin/kill -TERM ' + str(child.pid))
            _atomic_json(status_path, record)
            try:
                code = child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                raise RuntimeError('Cursor SDK setup timed out; inspect ' + str(log_path))
        if code:
            raise RuntimeError('Cursor SDK setup failed; inspect ' + str(log_path))

    try:
        candidates = [str(Path(python).expanduser().absolute())] if python else [sys.executable]
        if not python:
            for name in ('python3.14', 'python3.13', 'python3.12', 'python3.11', 'python3.10', 'python3'):
                binary = shutil.which(name)
                if binary and binary not in candidates:
                    candidates.append(binary)
        selected = next((binary for binary in candidates if probe_interpreter(binary)), None)
        if not selected and download_python:
            uv = shutil.which('uv')
            if not uv:
                raise ValueError('Install Python 3.10+ or uv, then run setup-cursor-sdk again.')
            managed = root / 'managed-python'
            execute([uv, '--no-config', 'python', 'install', '--no-bin', '--no-registry',
                     '--install-dir', str(managed), '3.12'])
            binaries = list(managed.glob('*/bin/python3.12')) + list(managed.glob('*/python.exe'))
            selected = next((str(binary) for binary in binaries if probe_interpreter(binary)), None)
        if not selected:
            raise ValueError('A healthy Python 3.10+ is needed. Use --python /path/to/python or --download-python with uv.')
        environments = root / 'environments'
        environments.mkdir(exist_ok=True, mode=0o700)
        environment = environments / ('env-' + uuid.uuid4().hex)
        # New immutable path: no --clear, no existing venv or system installation is changed.
        execute([selected, '-m', 'venv', str(environment)])
        installed_python = venv_python(environment)
        execute([str(installed_python), '-m', 'pip', '--isolated', 'install', '--no-input',
                 '--disable-pip-version-check', '--index-url', 'https://pypi.org/simple',
                 '-r', str(REQUIREMENTS)])
        if not probe_interpreter(installed_python, sdk=True):
            raise RuntimeError('Cursor SDK environment failed its import check; existing runtime preserved.')
        marker = {'schema': 1, 'managed_by': MANAGER, 'sdk_version': SDK_VERSION,
                  'environment': environment.relative_to(root).as_posix()}
        _atomic_json(root / 'runtime.json', marker)
        result = {'state': 'ready', 'run_id': run_id, 'sdk_version': SDK_VERSION,
                  'python': str(installed_python), 'reused': False,
                  'restart_required': True, 'log': str(log_path)}
        record.update(result)
        _atomic_json(status_path, record)
        return result
    except (OSError, ValueError, RuntimeError) as error:
        record.update(state='failed', error=str(error))
        _atomic_json(status_path, record)
        raise
