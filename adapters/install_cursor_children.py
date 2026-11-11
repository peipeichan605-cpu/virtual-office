#!/usr/bin/env python3
"""Install one scoped Office observer command per Cursor hook, preserving others."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import sys
import uuid

EVENTS = ('beforeSubmitPrompt', 'afterAgentResponse', 'stop', 'subagentStart',
          'subagentStop', 'preToolUse', 'postToolUse', 'postToolUseFailure')


def merged(existing, observer_dir, python=None, client=None, legacy_credential_path=None):
    if not isinstance(existing, dict) or existing.get('version', 1) != 1:
        raise ValueError('unsupported_cursor_hook_configuration')
    hooks = existing.get('hooks', {})
    if not isinstance(hooks, dict) or any(not isinstance(v, list) for v in hooks.values()):
        raise ValueError('unsupported_cursor_hook_configuration')
    result = {**existing, 'version': 1, 'hooks': {k: list(v) for k, v in hooks.items()}}
    client = Path(client or Path(__file__).with_name('cursor_children.py')).resolve()
    if legacy_credential_path is not None:
        legacy = Path(__file__).with_name('cursor_hook.py').resolve()
        own = Path(legacy_credential_path).expanduser().resolve()
        interpreters = {Path(sys.executable).resolve(), Path('/usr/bin/python3').resolve()}
        def replaced(row):
            if not isinstance(row, dict) or not isinstance(row.get('command'), str):
                return False
            try:
                args = shlex.split(row['command'])
                return (len(args) == 4 and Path(args[0]).resolve() in interpreters and args[2] == '--credential'
                        and Path(args[1]).expanduser().resolve() == legacy
                        and Path(args[3]).expanduser().resolve() == own)
            except (ValueError, OSError):
                return False
        for name in ('beforeSubmitPrompt', 'afterAgentResponse', 'stop'):
            if name in result['hooks']:
                result['hooks'][name] = [row for row in result['hooks'][name] if not replaced(row)]
    directory = Path(observer_dir).expanduser().resolve()
    prefix = shlex.join([python or sys.executable, str(client), '--observer-dir', str(directory), '--event'])
    for name in EVENTS:
        command = prefix + ' ' + shlex.quote(name)
        rows = result['hooks'].setdefault(name, [])
        if not any(isinstance(row, dict) and row.get('command') == command for row in rows):
            rows.append({'command': command, 'timeout': 3, 'failClosed': False})
    return result


def _snapshot(path):
    if path.is_symlink():
        raise ValueError('unsupported_cursor_hook_configuration')
    try:
        with path.open('rb') as source:
            stat = os.fstat(source.fileno())
            if stat.st_size > 256000:
                raise ValueError('unsupported_cursor_hook_configuration')
            value = source.read(256001)
        if len(value) > 256000:
            raise ValueError('unsupported_cursor_hook_configuration')
        return (stat.st_dev, stat.st_ino, stat.st_mtime_ns, value)
    except FileNotFoundError:
        return None


def _write_locked(observer_path, hooks_path, legacy_credential_path=None):
    original = _snapshot(hooks_path)
    existing = json.loads(original[3]) if original is not None else {'version': 1, 'hooks': {}}
    result = merged(existing, observer_path.parent, legacy_credential_path=legacy_credential_path)
    if result == existing:
        return {'hook_installed': True, 'changed': False, 'hooks_file': str(hooks_path), 'hook_events': len(EVENTS)}
    temporary = hooks_path.with_name(hooks_path.name + '.office-tmp-' + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump(result, output, indent=2, ensure_ascii=False)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        # Serialize Office installers and abort if another settings writer changed
        # the file while the merged configuration was being prepared.
        if _snapshot(hooks_path) != original:
            raise ValueError('cursor_hook_configuration_changed')
        if original is not None:
            backup = hooks_path.with_name(hooks_path.name + '.office-backup-' + uuid.uuid4().hex[:8])
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as output:
                output.write(original[3])
        if _snapshot(hooks_path) != original:
            raise ValueError('cursor_hook_configuration_changed')
        os.replace(temporary, hooks_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {'hook_installed': True, 'changed': True, 'hooks_file': str(hooks_path), 'hook_events': len(EVENTS)}


def install(observer_path, hooks_path, legacy_credential_path=None):
    observer_path = Path(observer_path).expanduser().resolve()
    if not observer_path.is_file() or observer_path.stat().st_size > 16384:
        raise ValueError('observer_unavailable')
    observer = json.loads(observer_path.read_text())
    if observer.get('provider') != 'cursor' or not observer.get('observer_id'):
        raise ValueError('cursor_observer_required')
    client = Path(__file__).with_name('cursor_children.py')
    if not client.is_file():
        raise ValueError('cursor_child_adapter_unavailable')
    hooks_path = Path(hooks_path).expanduser().absolute()
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = hooks_path.with_name(hooks_path.name + '.office-install.lock')
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return _write_locked(observer_path, hooks_path, legacy_credential_path)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--observer', required=True)
    p.add_argument('--hooks-file', required=True)
    p.add_argument('--legacy-credential', help='Replace only this installation own legacy observation command')
    args = p.parse_args()
    print(json.dumps(install(args.observer, args.hooks_file, args.legacy_credential), ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError):
        print(json.dumps({'hook_installed': False, 'reason': 'cursor_hook_installation_unavailable'}))
        raise SystemExit(1)
