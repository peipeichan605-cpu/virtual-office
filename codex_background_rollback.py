"""Park only Codex background registrations before restoring older Office code.

Dry run is the default. Run --apply only while the Office service is stopped;
this helper does not stop applications or services. It exports the removed
configuration privately and retains every lifecycle file and history database.
It never replaces configuration with an older snapshot.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import uuid

from configuration import DEFAULT_CONFIG, load_config
from history import timestamp


def _durable_json(path, value):
    path = Path(path)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        fd = os.open(str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
        if os.name == 'posix':
            directory = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def parked_configuration(config):
    """Pure transform: retain all non-background settings and retirement rows."""
    clean = copy.deepcopy(config)
    agents = [a for a in clean.get('agents', []) if a.get('driver') == 'codex_background']
    ids = {a.get('id', a['provider'] + ':' + a['source_id']) for a in agents}
    bindings = clean.get('command_bindings', {})
    adapter = clean.get('command_adapters', {}).get('codex_background')
    # Refuse an inconsistent route rather than silently park another mode.
    if any(binding.get('adapter') == 'codex_background' and ident not in ids for ident, binding in bindings.items()):
        raise ValueError('background_binding_conflict')
    if any(bindings.get(ident) != {'adapter': 'codex_background', 'session_id': ident.split(':', 1)[1]} for ident in ids):
        raise ValueError('background_binding_conflict')
    if adapter is not None and set(adapter.get('sessions', {})) != {a['source_id'] for a in agents}:
        raise ValueError('background_session_conflict')
    exported = {'schema': 1, 'mode': 'codex_background', 'agents': agents,
                'command_bindings': {ident: bindings[ident] for ident in sorted(ids)},
                'command_adapter': adapter,
                'disabled_command_agents': [ident for ident in clean.get('disabled_command_agents', []) if ident in ids]}
    clean['agents'] = [a for a in clean.get('agents', []) if a.get('driver') != 'codex_background']
    if 'command_bindings' in clean:
        clean['command_bindings'] = {ident: value for ident, value in bindings.items() if ident not in ids}
    if 'command_adapters' in clean:
        clean['command_adapters'].pop('codex_background', None)
    if 'disabled_command_agents' in clean:
        clean['disabled_command_agents'] = [ident for ident in clean['disabled_command_agents'] if ident not in ids]
    if ids:
        clean['logged_out_agent_ids'] = list(dict.fromkeys(clean.get('logged_out_agent_ids', []) + sorted(ids)))
    return clean, exported


def park(config_path=DEFAULT_CONFIG, apply=False):
    path = Path(config_path).expanduser().resolve()
    original = path.read_bytes()
    validated = load_config(path)
    raw = json.loads(original)
    clean, exported = parked_configuration(raw)
    ids = [a.get('id', a['provider'] + ':' + a['source_id']) for a in exported['agents']]
    changed = clean != raw
    result = {'applied': False, 'changed': changed, 'agent_ids': sorted(ids),
              'history_preserved': True, 'native_sessions_preserved': True,
              'export_path': None}
    if not apply or not changed:
        return result
    check = path.with_name('.codex-background-rollback-check-' + uuid.uuid4().hex + '.json')
    try:
        _durable_json(check, clean)
        load_config(check)
    finally:
        if check.exists():
            check.unlink()
    root = Path(validated['data_dir']) / 'codex-background-exports'
    if root.is_symlink():
        raise ValueError('private_export_path_required')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    export_path = root / ('parked-' + uuid.uuid4().hex + '.json')
    exported.update(parked_at=timestamp(), config_sha256=hashlib.sha256(original).hexdigest())
    _durable_json(export_path, exported)
    # An unrelated human edit is never overwritten by a stale plan.
    if path.read_bytes() != original:
        raise ValueError('configuration_changed_during_rollback')
    _durable_json(path, clean)
    result.update(applied=True, export_path=str(export_path))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--apply', action='store_true', help='Apply after stopping Office; default is a read-only plan')
    args = parser.parse_args()
    try:
        result = park(args.config, apply=args.apply)
    except (OSError, ValueError, KeyError, TypeError):
        parser.exit(1, 'Codex background rollback could not safely update this configuration. No history was removed.\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
