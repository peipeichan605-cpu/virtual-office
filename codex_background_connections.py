"""Owner-selected Codex sessions, reserved locally before the first instruction.

The registration ledger records intent before changing configuration. A lost
HTTP response can be recovered without starting or repeating a native turn.
This module never reads authentication files or creates a native thread.
"""
import copy
from contextlib import closing
import json
from pathlib import Path
import re
import threading
import time
import uuid

from configuration import CODEX_BACKGROUND_MODEL, agent_name, load_config
from connections import ConnectionError
from history import timestamp
from security_keys import integrity_fingerprint, matches_integrity
from workspaces import workspace_path


def request_uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ConnectionError('invalid_request_id')
    return value


def _default_probe(settings):
    from codex_background import probe
    return probe(settings)


def _model_metadata(value, configured):
    """A bounded catalog is information, never a model execution guarantee."""
    catalog = value.get('models')
    available = value.get('model_catalog_status') == 'available' and isinstance(catalog, list)
    models, seen = [], set()
    for item in catalog[:100] if available else []:
        if not isinstance(item, dict):
            continue
        ident = item.get('id')
        if not isinstance(ident, str) or not re.fullmatch(CODEX_BACKGROUND_MODEL, ident) or ident in seen:
            continue
        try:
            name = agent_name(item.get('name'))
        except (ValueError, TypeError, UnicodeError):
            name = ident[:80]
        seen.add(ident)
        models.append({'id': ident, 'name': name, 'is_default': item.get('is_default') is True,
                       'support': 'catalog_listed'})
    defaults = [item['id'] for item in models if item['is_default']]
    return {'models': models, 'model_catalog_status': 'available' if available else 'unavailable',
            'configured_model': configured,
            'configured_model_listed': configured in seen if available and configured is not None else None,
            'default_model': defaults[0] if len(defaults) == 1 else None}


class CodexBackgroundConnections:
    def __init__(self, bridge, runtime_probe=None):
        self.bridge = bridge
        self.root = Path(bridge.config['data_dir']) / 'codex-background'
        self.runtime_probe = runtime_probe or _default_probe
        self._default_runtime_probe = runtime_probe is None
        self._creation_lock = threading.RLock()
        self._creating = set()
        self._probe_lock = threading.RLock()
        self._probe_cache = None
        with bridge.history.lock, bridge.history.db:
            bridge.history.db.execute('''CREATE TABLE IF NOT EXISTS codex_background_creations (
                request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                state TEXT NOT NULL, response TEXT NOT NULL, reason TEXT,
                session_id TEXT NOT NULL, cwd TEXT NOT NULL, model TEXT,
                membership_epoch TEXT, created_at TEXT NOT NULL)''')
            bridge.history.db.execute('''CREATE TABLE IF NOT EXISTS codex_background_model_changes (
                request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, agent_id TEXT NOT NULL,
                membership_epoch TEXT, before_settings TEXT NOT NULL, desired_settings TEXT NOT NULL,
                state TEXT NOT NULL, response TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL)''')

    def _probe(self):
        settings = self.bridge.config.get('command_adapters', {}).get('codex_background', {})
        cache_key = settings.get('binary')
        if self._default_runtime_probe:
            # App upgrades and an installation switching to a usable bundled
            # runtime invalidate cached failure/model metadata immediately.
            from codex_rpc import RpcError, resolve_binary, runtime_info
            try:
                cache_key = runtime_info(resolve_binary(settings))['identity']
            except (RpcError, OSError):
                cache_key = ('unavailable', cache_key)
        with self._probe_lock:
            if self._probe_cache and self._probe_cache[0] == cache_key and time.monotonic() < self._probe_cache[1]:
                return copy.deepcopy(self._probe_cache[2])
            try:
                callback = self.runtime_probe if callable(self.runtime_probe) else self.runtime_probe.probe
                value = callback(settings)
                if not isinstance(value, dict):
                    raise ValueError()
            except Exception:
                value = {'available': False, 'reason': 'codex_probe_failed', 'logged_in': None}
            # Only capability metadata crosses the owner-facing API. In
            # particular, never forward account/read or auth details verbatim.
            available = value.get('available') is True
            logged_in = value.get('logged_in') if type(value.get('logged_in')) is bool else None
            reason = None if available and logged_in is True else (
                'codex_login_required' if available and logged_in is False else
                'codex_login_unknown' if available else 'codex_runtime_required')
            safe_reasons = {'codex_binary_unavailable', 'codex_version_unavailable',
                'codex_login_required', 'codex_login_unknown', 'codex_process_unavailable',
                'codex_process_closed', 'codex_rpc_timeout', 'codex_rpc_rejected',
                'codex_probe_unavailable', 'codex_probe_failed', 'codex_runtime_required'}
            if isinstance(value.get('reason'), str) and value['reason'] in safe_reasons:
                reason = value['reason']
            version = value.get('version')
            if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9 .+_-]{1,80}', version):
                version = None
            model = value.get('model')
            if not isinstance(model, str) or not re.fullmatch(CODEX_BACKGROUND_MODEL, model):
                model = None
            result = {'available': available, 'logged_in': logged_in,
                      'reason': reason, 'version': version, 'model': model,
                      'runtime_source':value.get('runtime_source') if value.get('runtime_source') in {'desktop_app','cli'} else None,
                      **_model_metadata(value, model)}
            self._probe_cache = (cache_key, time.monotonic() + 30, result)
            return copy.deepcopy(result)

    def _epoch(self, ident):
        db = self.bridge.history.db
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='office_membership_events'").fetchone():
            return None
        row = db.execute('SELECT event_id FROM office_membership_events WHERE agent_id=? ORDER BY rowid DESC LIMIT 1', (ident,)).fetchone()
        return row['event_id'] if row else None

    def _row(self, request_id):
        return self.bridge.history.db.execute('SELECT * FROM codex_background_creations WHERE request_id=?', (request_id,)).fetchone()

    def _matches(self, row, config):
        ident, sid = 'codex:' + row['session_id'], row['session_id']
        response = json.loads(row['response'])
        agent = next((a for a in config['agents'] if a['id'] == ident), None)
        settings = config.get('command_adapters', {}).get('codex_background', {}).get('sessions', {}).get(sid)
        expected = {'cwd': row['cwd'], 'lifecycle_file': str(self.root / (sid + '.json')),
                    'sandbox': 'read-only', 'approval_policy': 'on-request'}
        if row['model'] is not None:
            expected['model'] = row['model']
        settings_match = settings == expected
        if not settings_match and isinstance(settings, dict):
            # An explicit, audited model choice does not revoke the original
            # registration receipt. An unrelated settings edit still does.
            if {k:v for k,v in settings.items() if k!='model'} == {k:v for k,v in expected.items() if k!='model'}:
                changes = self.bridge.history.db.execute('SELECT desired_settings,membership_epoch FROM codex_background_model_changes WHERE agent_id=? AND state IN (?,?,?) ORDER BY rowid DESC',
                    (ident, 'applying', 'unknown', 'completed')).fetchall()
                settings_match = any(json.loads(change['desired_settings']) == settings
                    and change['membership_epoch'] == row['membership_epoch'] for change in changes)
        return bool(agent and agent['driver'] == 'codex_background' and agent['name'] == response['name']
            and ident not in config.get('logged_out_agent_ids', [])
            and config.get('command_bindings', {}).get(ident) == {'adapter': 'codex_background', 'session_id': sid}
            and settings_match and self._epoch(ident) == row['membership_epoch'])

    def _finish(self, request_id):
        with self.bridge.history.lock, self.bridge.history.db:
            self.bridge.history.db.execute('UPDATE codex_background_creations SET state=?,reason=NULL WHERE request_id=?', ('completed', request_id))

    def _reconcile(self, row):
        """Caller holds the command and history locks; no native RPC is made."""
        with self._creation_lock:
            active = row['request_id'] in self._creating
        result = {'request_id': row['request_id'], 'state': row['state']}
        if row['state'] == 'creating' and active:
            result.update(state='pending', reason='codex_creation_pending')
            return result
        try:
            config = load_config(self.bridge.config['config_path'])
            matches = self._matches(row, config)
        except (ValueError, OSError, KeyError, TypeError):
            config, matches = None, False
        if row['state'] in {'creating', 'unknown'} and matches:
            # Configuration may have committed immediately before a process
            # crash or a failed in-memory refresh. Reload only this committed
            # configuration, then repair the receipt; never reserve twice.
            self.bridge.refresh_connections(config)
            self._finish(row['request_id'])
            result['state'] = 'completed'
        elif row['state'] == 'completed' and not matches:
            with self.bridge.history.db:
                self.bridge.history.db.execute('UPDATE codex_background_creations SET state=?,reason=? WHERE request_id=?',
                                              ('revoked', 'agent_membership_changed', row['request_id']))
            result.update(state='revoked', reason='agent_membership_changed')
        elif row['state'] == 'creating':
            result.update(state='unknown', reason='codex_creation_unknown')
        if result['state'] == 'completed':
            response = json.loads(row['response'])
            result.update(response=response, agent_id=response['agent_id'], name=response['name'])
        elif row['reason']:
            result.setdefault('reason', row['reason'])
        return result

    def _model_target(self, config, agent_id):
        if not isinstance(agent_id, str):
            raise ConnectionError('codex_background_agent_not_selected', 404)
        agent = next((a for a in config['agents'] if a['id'] == agent_id and a['driver'] == 'codex_background'), None)
        if not agent or agent_id in config.get('logged_out_agent_ids', []):
            raise ConnectionError('codex_background_agent_not_selected', 404)
        sid = agent['source_id']
        if config.get('command_bindings', {}).get(agent_id) != {'adapter':'codex_background','session_id':sid}:
            raise ConnectionError('agent_membership_changed', 409)
        settings = config['command_adapters']['codex_background']['sessions'][sid]
        try:
            state = json.loads(Path(settings['lifecycle_file']).read_text())
        except (OSError, ValueError):
            raise ConnectionError('codex_background_recovery_required', 409)
        if not isinstance(state, dict) or state.get('schema') != 1 or state.get('session_id') != sid or state.get('cwd') != settings['cwd']:
            raise ConnectionError('codex_background_recovery_required', 409)
        return agent, settings, state

    def _model_change_reason(self, agent_id, state):
        db = getattr(self.bridge.commands, 'db', None)
        if db is not None:
            if db.execute("SELECT 1 FROM commands WHERE agent_id=? AND state='delivery_unknown' LIMIT 1", (agent_id,)).fetchone():
                return 'codex_background_recovery_required'
            if db.execute("SELECT 1 FROM commands WHERE agent_id=? AND state IN ('pending','sending','queued','running','waiting_user') LIMIT 1", (agent_id,)).fetchone():
                return 'codex_model_change_busy'
        if state.get('phase') not in {'reserved', 'ready'}:
            return 'codex_background_recovery_required'
        adapter = getattr(self.bridge.commands, 'adapters', {}).get('codex_background')
        if adapter and hasattr(adapter, 'model_change_reason'):
            return adapter.model_change_reason(agent_id.split(':', 1)[1])
        return None

    def _model_warnings(self, availability):
        """Historical rejections are scoped to the runtime which observed them.

        Legacy records without a version are warnings only. A catalog refresh
        after an upgrade must not preserve an obsolete model rejection block.
        """
        rejected, blocked = [], set()
        current_version = availability.get('version')
        listed = {item['id'] for item in availability.get('models', [])}
        catalog_available = availability.get('model_catalog_status') == 'available'
        for sid, settings in self.bridge.config.get('command_adapters', {}).get('codex_background', {}).get('sessions', {}).items():
            try:
                state = json.loads(Path(settings['lifecycle_file']).read_text())
            except (OSError, ValueError):
                continue
            model = (state.get('model_error_model') or state.get('model')) if isinstance(state, dict) else None
            if (not isinstance(model, str) or not re.fullmatch(CODEX_BACKGROUND_MODEL, model)
                    or state.get('detail_code') != 'codex_model_unavailable'):
                continue
            # The last failure's runtime/model must not be reassigned to a
            # new executable or a newly resumed model before its turn ends.
            version = state.get('model_error_runtime_version') if 'model_error_runtime_version' in state else state.get('runtime_version') if state.get('phase')=='ready' else None
            if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9 .+_-]{1,80}', version):
                version = None
            matches = bool(version and current_version and version == current_version)
            if not any(item['model']==model and item['runtime_version']==version for item in rejected):
                rejected.append({'model':model, 'runtime_version':version, 'current_runtime_match':matches})
            if matches and catalog_available and model not in listed:
                blocked.add(model)
        configured = availability.get('model')
        return {'known_rejected_models':rejected[:100], 'known_unavailable_models':sorted(blocked)[:100],
                'configured_model_warning':any(item['model']==configured for item in rejected),
                'configured_model_requires_selection':configured in blocked,
                'configured_model_unavailable':configured in blocked}

    def status(self, request_id=None, agent_id=None):
        if request_id is not None:
            request_id = request_uuid(request_id)
        catalog = self.bridge.workspaces.catalog() if hasattr(self.bridge, 'workspaces') else []
        workspaces = [{'name': item['name'], 'path': item['path']} for item in catalog]
        settings = self.bridge.config.get('command_adapters', {}).get('codex_background', {}).get('sessions', {})
        candidates = [item.get('cwd') for item in settings.values()] + [item['path'] for item in workspaces] + [str(Path.cwd())]
        workspace = next((item for item in candidates if isinstance(item, str) and Path(item).is_dir()), None)
        result = {**self._probe(), 'mode': 'codex_background',
                  'workspace': workspace, 'workspaces': workspaces,
                  'scope': 'office_owned_session', 'conversation_scope': 'office_owned_session',
                  'tool_policy': 'read_only', 'sandbox': 'read-only', 'approval_policy': 'on-request'}
        with self.bridge.commands.lock, self.bridge.history.lock:
            result.update(self._model_warnings(result))
            if agent_id is not None:
                config = load_config(self.bridge.config['config_path'])
                agent, selected, state = self._model_target(config, agent_id)
                reason = self._model_change_reason(agent_id, state)
                requested, actual = selected.get('model'), state.get('model')
                result['current_agent'] = {'agent_id':agent_id, 'name':agent['name'],
                    'current_model':requested or actual or result.get('model'),
                    'requested_model':requested, 'actual_model':actual,
                    'pending_model_change':bool(requested and requested != actual),
                    'can_change':reason is None, 'reason':reason}
        if request_id is not None:
            with self.bridge.commands.lock, self.bridge.history.lock:
                change = self.bridge.history.db.execute('SELECT * FROM codex_background_model_changes WHERE request_id=?', (request_id,)).fetchone()
                row = self._row(request_id)
                result['request'] = self._reconcile_model(change) if change else self._reconcile(row) if row else {'request_id': request_id, 'state': 'not_found'}
        return result

    def _reconcile_model(self, row):
        result = {'request_id':row['request_id'], 'state':row['state'], 'operation':'model_change'}
        try:
            config = load_config(self.bridge.config['config_path'])
            _, settings, _ = self._model_target(config, row['agent_id'])
            membership_matches = self._epoch(row['agent_id']) == row['membership_epoch']
        except (ConnectionError, ValueError, OSError, KeyError, TypeError):
            membership_matches, settings = False, None
        if not membership_matches:
            result.update(state='revoked', reason='agent_membership_changed')
        elif settings == json.loads(row['desired_settings']):
            # The only side effect was atomic config replacement. A lost HTTP
            # reply or in-memory refresh can be reconciled without native RPC.
            self.bridge.refresh_connections(config)
            result.update(state='completed', response=json.loads(row['response']))
        elif row['state'] == 'completed':
            result.update(state='superseded', reason='codex_model_change_superseded')
        elif settings == json.loads(row['before_settings']):
            result.update(state='failed', reason='codex_model_change_not_applied')
        else:
            result.update(state='unknown', reason='codex_model_change_unknown')
        with self.bridge.history.db:
            self.bridge.history.db.execute('UPDATE codex_background_model_changes SET state=?,reason=? WHERE request_id=?',
                (result['state'], result.get('reason'), row['request_id']))
        return result

    def update_model(self, data):
        if not isinstance(data, dict) or set(data) != {'request_id','agent_id','model'}:
            raise ConnectionError('invalid_connection')
        request_id = request_uuid(data['request_id'])
        model, agent_id = data['model'], data['agent_id']
        if not isinstance(model, str) or not re.fullmatch(CODEX_BACKGROUND_MODEL, model):
            raise ConnectionError('codex_model_invalid')
        if not isinstance(agent_id, str):
            raise ConnectionError('codex_background_agent_not_selected', 404)
        fingerprint_payload = json.dumps({'agent_id':agent_id, 'model':model}, sort_keys=True).encode()
        fingerprint = integrity_fingerprint(self.bridge.config, fingerprint_payload)
        with self.bridge.commands.lock, self.bridge.history.lock:
            row = self.bridge.history.db.execute('SELECT * FROM codex_background_model_changes WHERE request_id=?', (request_id,)).fetchone()
            if row:
                if not matches_integrity(self.bridge.config, fingerprint_payload, row['fingerprint']):
                    raise ConnectionError('request_id_conflict', 409)
                receipt = self._reconcile_model(row)
                if receipt['state']=='completed':
                    return receipt['response']
                raise ConnectionError(receipt.get('reason') or 'codex_model_change_unknown', 409)
            if self._row(request_id):
                raise ConnectionError('request_id_conflict', 409)
            config = load_config(self.bridge.config['config_path'])
            agent, settings, state = self._model_target(config, agent_id)
            expected_epoch = self._epoch(agent_id)
            expected_settings = dict(settings)
            reason = self._model_change_reason(agent_id, state)
            if reason:
                raise ConnectionError(reason, 409)
            desired = {**settings, 'model':model}
            response = {'request_id':request_id, 'agent_id':agent_id, 'model':model,
                'model_saved':True, 'same_session':True, 'native_thread_preserved':True,
                'pending_next_instruction':True, 'execution_verified':False}
            adapter = getattr(self.bridge.commands, 'adapters', {}).get('codex_background')
            from codex_background import _Lease
            from command_adapters import DeliveryError
            try:
                guard = adapter.model_change_guard(agent['source_id']) if adapter and hasattr(adapter, 'model_change_guard') else closing(_Lease(settings['lifecycle_file']))
                with guard:
                    # Re-read after acquiring the cross-process native lease.
                    config = load_config(self.bridge.config['config_path'])
                    agent, settings, state = self._model_target(config, agent_id)
                    if settings != expected_settings or self._epoch(agent_id) != expected_epoch:
                        raise ConnectionError('agent_membership_changed', 409)
                    reason = self._model_change_reason(agent_id, state)
                    if reason:
                        raise ConnectionError(reason, 409)
                    with self.bridge.history.db:
                        self.bridge.history.db.execute('INSERT INTO codex_background_model_changes VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (request_id, fingerprint, agent_id, self._epoch(agent_id), json.dumps(settings, sort_keys=True),
                             json.dumps(desired, sort_keys=True), 'applying', json.dumps(response), None, timestamp()))
                    config['command_adapters']['codex_background']['sessions'][agent['source_id']] = desired
                    try:
                        self.bridge.connections.save(config)
                    except Exception as error:
                        with self.bridge.history.db:
                            self.bridge.history.db.execute('UPDATE codex_background_model_changes SET state=?,reason=? WHERE request_id=?',
                                ('unknown', 'codex_model_change_unknown', request_id))
                        raise ConnectionError('codex_model_change_unknown', 409) from error
                    with self.bridge.history.db:
                        self.bridge.history.db.execute('UPDATE codex_background_model_changes SET state=? WHERE request_id=?', ('completed', request_id))
            except DeliveryError as error:
                raise ConnectionError(error.code, 409) from error
            return response

    def create(self, data):
        allowed = {'request_id', 'name', 'workspace', 'create_folder', 'model'}
        if not isinstance(data, dict) or set(data) - allowed:
            raise ConnectionError('invalid_connection')
        request_id = request_uuid(data.get('request_id'))
        try:
            name = agent_name(data.get('name'))
        except (ValueError, TypeError, UnicodeError):
            raise ConnectionError('invalid_connection')
        if 'create_folder' in data and type(data['create_folder']) is not bool:
            raise ConnectionError('invalid_workspace')
        creating_folder = data.get('create_folder') is True
        try:
            cwd = workspace_path(data.get('workspace'))
        except (ValueError, TypeError, OSError, RuntimeError):
            raise ConnectionError('invalid_workspace')
        model = data.get('model')
        if 'model' in data and (not isinstance(model, str) or not re.fullmatch(CODEX_BACKGROUND_MODEL, model)):
            raise ConnectionError('codex_model_invalid')
        fingerprint_payload = json.dumps({
            'name': name, 'cwd': cwd, 'model': model, 'create_folder': creating_folder},
            sort_keys=True).encode()
        fingerprint = integrity_fingerprint(self.bridge.config, fingerprint_payload)
        # Replaying a successful registration does not require the runtime to
        # be currently logged in, and never changes its membership generation.
        with self.bridge.commands.lock, self.bridge.history.lock:
            row = self._row(request_id)
            if row:
                return self._existing(row, fingerprint_payload)
            if self.bridge.history.db.execute('SELECT 1 FROM codex_background_model_changes WHERE request_id=?', (request_id,)).fetchone():
                raise ConnectionError('request_id_conflict', 409)
        availability = self._probe()
        if not availability['available'] or availability['logged_in'] is not True:
            raise ConnectionError(availability['reason'] or 'codex_login_required', 409)
        with self.bridge.commands.lock, self.bridge.history.lock:
            if model is None and self._model_warnings(availability)['configured_model_requires_selection']:
                raise ConnectionError('codex_model_selection_required', 409)
        folder = Path(cwd)
        if not folder.is_dir() and not (creating_folder and not folder.exists() and folder.parent.is_dir()):
            raise ConnectionError('invalid_workspace')
        sid, ident = 'bg-' + request_id, 'codex:bg-' + request_id
        response = {'connected': True, 'agent_id': ident, 'name': name,
                    'mode': 'codex_background', 'request_id': request_id, 'same_session': True,
                    'native_target_verified': False, 'execution_verified': False,
                    'registration_state': 'pending_first_instruction',
                    'conversation_scope': 'office_owned_session', 'scope': 'office_owned_session',
                    'tool_policy': 'read_only'}
        with self.bridge.commands.lock, self.bridge.history.lock, self.bridge.history.db:
            row = self._row(request_id)
            if row:
                return self._existing(row, fingerprint_payload)
            if self.bridge.history.db.execute('SELECT 1 FROM codex_background_model_changes WHERE request_id=?', (request_id,)).fetchone():
                raise ConnectionError('request_id_conflict', 409)
            config = load_config(self.bridge.config['config_path'])
            if ident in config.get('logged_out_agent_ids', []) or any(a['id'] == ident for a in config['agents']):
                raise ConnectionError('agent_membership_changed', 409)
            if any(a['name'] == name for a in config['agents']):
                raise ConnectionError('name_in_use', 409)
            self.bridge.history.db.execute('INSERT INTO codex_background_creations VALUES (?,?,?,?,?,?,?,?,?,?)',
                (request_id, fingerprint, 'creating', json.dumps(response), None, sid, cwd, model,
                 self._epoch(ident), timestamp()))
            with self._creation_lock:
                self._creating.add(request_id)
        try:
            if creating_folder:
                folder.mkdir(exist_ok=True)
            lifecycle = self.root / (sid + '.json')
            if self.root.is_symlink() or lifecycle.exists() or lifecycle.is_symlink():
                raise ConnectionError('codex_private_state_conflict', 409)
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.root.chmod(0o700)
            from codex_background import durable_state
            durable_state(lifecycle, {'schema': 1, 'session_id': sid, 'cwd': cwd,
                'phase': 'reserved', 'native_thread_id': None, 'native_turn_id': None,
                'command_id': None, 'native_target_verified': False, 'name': name,
                'created_at': timestamp()})
            with self.bridge.commands.lock, self.bridge.history.lock:
                config = load_config(self.bridge.config['config_path'])
                if ident in config.get('logged_out_agent_ids', []) or any(a['id'] == ident for a in config['agents']):
                    raise ConnectionError('agent_membership_changed', 409)
                if any(a['name'] == name for a in config['agents']):
                    raise ConnectionError('name_in_use', 409)
                config['agents'].append({'id': ident, 'source_id': sid, 'name': name,
                                         'provider': 'codex', 'driver': 'codex_background'})
                config.setdefault('command_bindings', {})[ident] = {'adapter': 'codex_background', 'session_id': sid}
                session = {'cwd': cwd, 'lifecycle_file': str(lifecycle),
                           'sandbox': 'read-only', 'approval_policy': 'on-request'}
                if model is not None:
                    session['model'] = model
                config.setdefault('command_adapters', {}).setdefault('codex_background', {}).setdefault('sessions', {})[sid] = session
                self.bridge.connections.save(config)
                if hasattr(self.bridge, 'workspaces'):
                    workspace = self.bridge.workspaces.create(cwd)
                    self.bridge.workspaces.observe(ident, cwd, 'codex_background')
                    self.bridge.workspaces.assign(ident, workspace['id'], 'manual')
                self._finish(request_id)
            return response
        except Exception as error:
            reason = error.code if isinstance(error, ConnectionError) else 'codex_creation_unknown'
            with self.bridge.history.lock, self.bridge.history.db:
                self.bridge.history.db.execute('UPDATE codex_background_creations SET state=?,reason=? WHERE request_id=?',
                                              ('unknown', reason, request_id))
            raise ConnectionError(reason, 409) from error
        finally:
            with self._creation_lock:
                self._creating.discard(request_id)

    def _existing(self, row, fingerprint_payload):
        if not matches_integrity(self.bridge.config, fingerprint_payload, row['fingerprint']):
            raise ConnectionError('request_id_conflict', 409)
        result = self._reconcile(row)
        if result['state'] == 'completed':
            return result['response']
        raise ConnectionError(result.get('reason') or 'codex_creation_unknown', 409)
