"""Explicit owner enrollment for existing Grok Bot sessions.

This service never creates a native Bot or sends instructions. Browser login
belongs to the owner; local registration has a durable, recoverable receipt.
"""
import json
import re
import threading
import urllib.parse
import uuid

from configuration import GROK_BOT_ID, GROK_SESSION_ID, agent_name, load_config
from connections import ConnectionError
from history import timestamp
from security_keys import integrity_fingerprint, matches_integrity


def request_uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ConnectionError('invalid_request_id') from None
    return value


def _bot_id(value):
    if not isinstance(value, str) or not re.fullmatch(GROK_BOT_ID, value):
        raise ConnectionError('grok_sdk_invalid_bot')
    return value


def _session_id(value):
    # The server uses the empty string for its main chat. Missing selection is
    # None; explicit empty selection is verified against the native roster.
    if not isinstance(value, str) or not re.fullmatch(GROK_SESSION_ID, value):
        raise ConnectionError('grok_sdk_invalid_session')
    return value


def _safe_reason(value, fallback='grok_sdk_unavailable'):
    return value if isinstance(value, str) and re.fullmatch(r'[a-z_]{1,80}', value) else fallback


def _safe_name(value, fallback):
    try:
        return agent_name(value)
    except (ValueError, TypeError, UnicodeError):
        return fallback[:80]


class GrokSdkConnections:
    def __init__(self, bridge, client=None):
        self.bridge = bridge
        self.enabled = not getattr(bridge, 'dry_run', False)
        if client is None:
            adapter = getattr(bridge.commands, 'adapters', {}).get('grok_sdk')
            client = getattr(adapter, 'client', None)
            if client is None:
                from grok_sdk import GrokSdkClient
                client = GrokSdkClient(bridge.config)
        self.client = client
        self._lock = threading.RLock()
        self._connecting = set()
        if self.enabled:
            with bridge.history.lock, bridge.history.db:
                bridge.history.db.execute('''CREATE TABLE IF NOT EXISTS grok_sdk_connections (
                    request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL, response TEXT NOT NULL, reason TEXT,
                    agent_id TEXT NOT NULL, settings TEXT NOT NULL,
                    membership_epoch TEXT, created_at TEXT NOT NULL,
                    baseline TEXT)''')
                columns = {row[1] for row in bridge.history.db.execute('PRAGMA table_info(grok_sdk_connections)')}
                if 'baseline' not in columns:
                    bridge.history.db.execute('ALTER TABLE grok_sdk_connections ADD COLUMN baseline TEXT')
        self._redact_credentials()

    def _write_allowed(self):
        if not self.enabled:
            raise ConnectionError('grok_sdk_dry_run', 409)

    def _call(self, method, *args):
        try:
            return getattr(self.client, method)(*args)
        except ConnectionError:
            raise
        except Exception as error:
            code = _safe_reason(getattr(error, 'code', None))
            raise ConnectionError(code, 409 if code != 'grok_sdk_unavailable' else 503) from None

    def _auth(self):
        if not self.enabled:
            return {'state': 'unavailable', 'available': False, 'reason': 'grok_sdk_dry_run'}
        try:
            value = self.client.auth_status()
        except Exception:
            value = {}
        if not isinstance(value, dict):
            value = {}
        state = value.get('state')
        if state not in {'logged_out', 'pending', 'authenticated', 'expired', 'unavailable'}:
            state = 'unavailable'
        result = {'state': state, 'available': value.get('available') is True}
        if value.get('reason') is not None:
            result['reason'] = _safe_reason(value['reason'])
        fingerprint = value.get('account_fingerprint')
        if isinstance(fingerprint, str) and re.fullmatch(r'[0-9a-f]{64}', fingerprint):
            result['account_fingerprint'] = fingerprint
        if state == 'authenticated' and 'account_fingerprint' not in result:
            result.update(state='unavailable', reason='grok_sdk_account_unverified')
        for key in ('sdk_version', 'client_version'):
            version = value.get(key)
            if isinstance(version, str) and re.fullmatch(r'[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}', version):
                result[key] = version
        latest = value.get('latest_login')
        if isinstance(latest, dict):
            try:
                ident = request_uuid(latest.get('login_id'))
                result['latest_login'] = self._login_receipt(latest, ident)
            except ConnectionError:
                pass
        return result

    def _authenticated(self):
        auth = self._auth()
        if auth['state'] != 'authenticated' or not auth['available']:
            raise ConnectionError(auth.get('reason') or 'grok_sdk_login_required', 409)
        return auth['account_fingerprint']

    def _redact_credentials(self):
        """Known local secrets stay in the redactor, never in any API receipt."""
        callback = getattr(self.client, 'redaction_secrets', None)
        if callback is None:
            return
        try:
            values = callback()
        except Exception:
            return
        if isinstance(values, (tuple, list)):
            with self.bridge.history.lock:
                previous = self.bridge.history.secrets
                self.bridge.history.secrets = tuple(dict.fromkeys((*previous,
                    *(value for value in values if isinstance(value, str) and len(value) >= 8))))

    def _epoch(self, ident):
        db = self.bridge.history.db
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='office_membership_events'").fetchone():
            return None
        row = db.execute('SELECT event_id FROM office_membership_events WHERE agent_id=? ORDER BY rowid DESC LIMIT 1', (ident,)).fetchone()
        return row['event_id'] if row else None

    def _row(self, ident):
        db = self.bridge.history.db
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='grok_sdk_connections'").fetchone():
            return None
        return db.execute('SELECT * FROM grok_sdk_connections WHERE request_id=?', (ident,)).fetchone()

    def _matches(self, row, config):
        ident = row['agent_id']
        sid = ident.split(':', 1)[1]
        response = json.loads(row['response'])
        agent = next((a for a in config['agents'] if a['id'] == ident), None)
        return bool(agent and agent['driver'] == 'grok_sdk' and agent['name'] == response['name']
            and ident not in config.get('logged_out_agent_ids', [])
            and config.get('command_bindings', {}).get(ident) == {'adapter': 'grok_sdk', 'session_id': sid}
            and config.get('command_adapters', {}).get('grok_sdk', {}).get('sessions', {}).get(sid) == json.loads(row['settings'])
            and self._epoch(ident) == row['membership_epoch'])

    def _finish(self, request_id):
        with self.bridge.history.db:
            self.bridge.history.db.execute('UPDATE grok_sdk_connections SET state=?,reason=NULL WHERE request_id=?', ('completed', request_id))

    def _reconcile(self, row):
        result = {'request_id': row['request_id'], 'state': row['state']}
        with self._lock:
            active = row['request_id'] in self._connecting
        if active and row['state'] == 'connecting':
            return {**result, 'state': 'pending', 'reason': 'grok_sdk_connection_pending'}
        try:
            config = load_config(self.bridge.config['config_path'])
            matches = self._matches(row, config)
        except (OSError, ValueError, TypeError, KeyError):
            config, matches = None, False
        if row['state'] in {'connecting', 'unknown'} and matches and self.enabled:
            # Recover only a proven committed registration. No login, native
            # message, or configuration rewrite occurs during recovery.
            self.bridge.refresh_connections(config)
            self._finish(row['request_id'])
            result['state'] = 'completed'
        elif row['state'] == 'completed' and not matches:
            result.update(state='revoked', reason='agent_membership_changed')
            if self.enabled:
                with self.bridge.history.db:
                    self.bridge.history.db.execute('UPDATE grok_sdk_connections SET state=?,reason=? WHERE request_id=?',
                        ('revoked', 'agent_membership_changed', row['request_id']))
        elif row['state'] == 'connecting':
            result.update(state='unknown', reason='grok_sdk_connection_unknown')
        if result['state'] == 'completed':
            result['response'] = json.loads(row['response'])
        elif row['reason']:
            result.setdefault('reason', _safe_reason(row['reason']))
        return result

    def _login_receipt(self, value, login_id):
        if not isinstance(value, dict):
            raise ConnectionError('grok_sdk_login_unavailable', 503)
        state = value.get('state')
        if state not in {'pending', 'authenticated', 'expired', 'cancelled', 'unavailable', 'not_found', 'failed'}:
            raise ConnectionError('grok_sdk_login_unavailable', 503)
        result = {'request_id': login_id, 'login_id': login_id, 'state': state}
        url = value.get('login_url')
        if state == 'pending' and url is not None:
            try:
                parsed = urllib.parse.urlsplit(url)
                if (parsed.scheme != 'https' or parsed.hostname != 'cursor.com'
                        or parsed.path != '/loginDeepControl' or parsed.username or parsed.password
                        or parsed.fragment or parsed.port not in (None, 443) or len(url) > 4096):
                    raise ValueError()
            except (ValueError, TypeError):
                raise ConnectionError('grok_sdk_login_url_invalid', 503) from None
            result['login_url'] = url
        if isinstance(value.get('expires_at'), (int, float, str)):
            result['expires_at'] = value['expires_at']
        if value.get('reason') is not None:
            result['reason'] = _safe_reason(value['reason'], 'grok_sdk_login_unavailable')
        return result

    def status(self, request_id=None, login_request_id=None):
        if request_id is not None:
            request_id = request_uuid(request_id)
        if login_request_id is not None:
            login_request_id = request_uuid(login_request_id)
        result = {'mode': 'grok_sdk', 'experimental': True, 'auth': self._auth(), 'connections': []}
        with self.bridge.commands.lock, self.bridge.history.lock:
            config = load_config(self.bridge.config['config_path'])
            for agent in config['agents']:
                if agent['driver'] != 'grok_sdk':
                    continue
                settings = config['command_adapters']['grok_sdk']['sessions'][agent['source_id']]
                result['connections'].append({'agent_id': agent['id'], 'name': agent['name'],
                    'bot_id': settings['bot_id'], 'session_id': settings['native_session_id'],
                    'state': 'configured', 'execution_verified': False})
            if request_id is not None:
                row = self._row(request_id)
                result['request'] = self._reconcile(row) if row else {'request_id': request_id, 'state': 'not_found'}
        if login_request_id is not None:
            value = self._call('login_status', login_request_id)
            receipt = self._login_receipt(value, login_request_id)
            if receipt['state'] == 'not_found':
                result['login_request'] = {'request_id': login_request_id, 'state': 'not_found'}
            else:
                result['login'] = receipt
        return result

    def start_login(self, data):
        self._write_allowed()
        if not isinstance(data, dict) or set(data) != {'request_id'}:
            raise ConnectionError('invalid_connection')
        ident = request_uuid(data['request_id'])
        result = self._login_receipt(self._call('start_login', ident), ident)
        return {**result, 'auth': self._auth(), 'experimental': True}

    def login_status(self, login_id):
        self._write_allowed()
        ident = request_uuid(login_id)
        result = self._login_receipt(self._call('poll_login', ident), ident)
        if result['state'] == 'authenticated':
            self._redact_credentials()
        return {**result, 'auth': self._auth(), 'experimental': True}

    def cancel_login(self, data):
        self._write_allowed()
        if not isinstance(data, dict) or set(data) != {'login_id'}:
            raise ConnectionError('invalid_connection')
        ident = request_uuid(data['login_id'])
        result = self._login_receipt(self._call('cancel_login', ident), ident)
        return {**result, 'auth': self._auth()}

    def bots(self):
        before = self._authenticated()
        value = self._call('list_bots')
        if not isinstance(value, dict) or not isinstance(value.get('bots'), list) or len(value['bots']) > 1000:
            raise ConnectionError('grok_sdk_roster_invalid', 503)
        bots, seen = [], set()
        for item in value['bots']:
            if not isinstance(item, dict):
                raise ConnectionError('grok_sdk_roster_invalid', 503)
            ident = _bot_id(item.get('id'))
            if ident in seen:
                raise ConnectionError('grok_sdk_roster_invalid', 503)
            seen.add(ident)
            # The SDK normalizes protobuf viewerSessionId to sessionId. An
            # empty value is the main chat, not a request to create a session.
            sid = _session_id(item.get('sessionId', item.get('session_id')))
            bots.append({'id': ident, 'name': _safe_name(item.get('name'), ident), 'session_id': sid})
        if before != self._authenticated():
            raise ConnectionError('grok_sdk_account_changed', 409)
        return {'bots': bots, 'truncated': value.get('truncated') is True}

    def sessions(self, bot_id):
        bot_id = _bot_id(bot_id)
        before = self._authenticated()
        roster = self.bots()
        selected = next((row for row in roster['bots'] if row['id'] == bot_id), None)
        if selected is None:
            raise ConnectionError('grok_sdk_bot_not_found', 404)
        value = self._call('list_sessions', bot_id)
        if not isinstance(value, dict) or not isinstance(value.get('sessions'), list) or len(value['sessions']) > 1000:
            raise ConnectionError('grok_sdk_sessions_invalid', 503)
        sessions, seen = [], set()
        for item in value['sessions']:
            if not isinstance(item, dict) or item.get('agentId', item.get('agent_id', bot_id)) != bot_id:
                raise ConnectionError('grok_sdk_sessions_invalid', 503)
            sid = _session_id(item.get('sessionId', item.get('session_id')))
            if sid in seen:
                continue
            seen.add(sid)
            sessions.append({'session_id': sid, 'kind': 'main' if not sid else 'session',
                             'label': '主要聊天' if not sid else sid})
        current = selected['session_id']
        if current not in seen:
            sessions.insert(0, {'session_id': current, 'kind': 'main' if not current else 'session',
                               'label': '主要聊天' if not current else current})
        if before != self._authenticated():
            raise ConnectionError('grok_sdk_account_changed', 409)
        return {'bot_id': bot_id, 'sessions': sessions}

    def _existing(self, row, fingerprint_payload):
        if not matches_integrity(self.bridge.config, fingerprint_payload, row['fingerprint']):
            raise ConnectionError('request_id_conflict', 409)
        result = self._reconcile(row)
        if result['state'] == 'completed':
            return result['response']
        raise ConnectionError(result.get('reason') or 'grok_sdk_connection_unknown', 409)

    def _baseline(self, bot_id, session_id):
        """Capture the observer boundary before an enrolled Agent is visible.

        Keep only native sequence metadata. Content before this boundary is
        not imported, and input immediately after enrollment cannot be skipped
        by a later observer bootstrap or a service restart.
        """
        from grok_sdk_sync import SyncError, cursor_token, native_generation, uint
        page = self._call('list_transcript', bot_id, session_id, 1)
        try:
            if not isinstance(page, dict) or not isinstance(page.get('entries'), list) or len(page['entries']) > 1:
                raise SyncError('grok_sdk_invalid_history')
            generation = native_generation(page.get('generation'))
            floor, updated = 0, 0
            for row in page['entries']:
                if not isinstance(row, dict):
                    raise SyncError('grok_sdk_invalid_history')
                seq = uint(row.get('seq'))
                changed = uint(row.get('updatedSeq', row.get('updated_seq', seq)))
                floor, updated = max(floor, seq), max(updated, changed)
            return {'cursor': cursor_token(bot_id, session_id, generation, updated),
                    'native_generation': generation, 'floor_seq': str(floor), 'captured_at': timestamp()}
        except (SyncError, ValueError, KeyError, TypeError):
            raise ConnectionError('grok_sdk_invalid_history', 503) from None

    def connect(self, data):
        self._write_allowed()
        if not isinstance(data, dict) or set(data) != {'request_id', 'bot_id', 'session_id', 'name'}:
            raise ConnectionError('invalid_connection')
        request_id = request_uuid(data['request_id'])
        bot_id, session_id = _bot_id(data['bot_id']), _session_id(data['session_id'])
        try:
            name = agent_name(data['name'])
        except (ValueError, TypeError, UnicodeError):
            raise ConnectionError('invalid_connection') from None
        fingerprint_payload = json.dumps(
            {'bot_id': bot_id, 'session_id': session_id, 'name': name}, sort_keys=True).encode()
        fingerprint = integrity_fingerprint(self.bridge.config, fingerprint_payload)
        with self.bridge.commands.lock, self.bridge.history.lock:
            row = self._row(request_id)
            if row:
                return self._existing(row, fingerprint_payload)
        account = self._authenticated()
        selected = self.sessions(bot_id)
        if not any(item['session_id'] == session_id for item in selected['sessions']):
            raise ConnectionError('grok_sdk_session_not_found', 404)
        baseline = self._baseline(bot_id, session_id)
        if account != self._authenticated():
            raise ConnectionError('grok_sdk_account_changed', 409)
        sid, ident = 'sdk-' + request_id, 'grok_bot:sdk-' + request_id
        settings = {'bot_id': bot_id, 'native_session_id': session_id, 'account_fingerprint': account}
        response = {'connected': True, 'agent_id': ident, 'name': name, 'mode': 'grok_sdk',
            'request_id': request_id, 'bot_id': bot_id, 'session_id': session_id, 'state': 'configured',
            'same_session': True, 'persistent': True, 'native_target_verified': True,
            'execution_verified': False, 'experimental': True, 'conversation_scope': 'selected_native_session'}
        with self.bridge.commands.lock, self.bridge.history.lock:
            row = self._row(request_id)
            if row:
                return self._existing(row, fingerprint_payload)
            config = load_config(self.bridge.config['config_path'])
            if ident in config.get('logged_out_agent_ids', []) or any(a['id'] == ident for a in config['agents']):
                raise ConnectionError('agent_membership_changed', 409)
            if any(a['name'] == name for a in config['agents']):
                raise ConnectionError('name_in_use', 409)
            if settings in config.get('command_adapters', {}).get('grok_sdk', {}).get('sessions', {}).values():
                raise ConnectionError('grok_sdk_session_already_selected', 409)
            if account != self._authenticated():
                raise ConnectionError('grok_sdk_account_changed', 409)
            with self.bridge.history.db:
                self.bridge.history.db.execute('''INSERT INTO grok_sdk_connections
                    (request_id,fingerprint,state,response,reason,agent_id,settings,membership_epoch,created_at,baseline)
                    VALUES (?,?,?,?,?,?,?,?,?,?)''',
                    (request_id, fingerprint, 'connecting', json.dumps(response), None, ident,
                     json.dumps(settings, sort_keys=True), self._epoch(ident), timestamp(), json.dumps(baseline)))
            with self._lock:
                self._connecting.add(request_id)
            try:
                config['agents'].append({'id': ident, 'source_id': sid, 'name': name,
                                         'provider': 'grok_bot', 'driver': 'grok_sdk'})
                config.setdefault('command_bindings', {})[ident] = {'adapter': 'grok_sdk', 'session_id': sid}
                config.setdefault('command_adapters', {}).setdefault('grok_sdk', {}).setdefault('sessions', {})[sid] = settings
                self.bridge.connections.save(config)
                self._finish(request_id)
            except Exception as error:
                reason = error.code if isinstance(error, ConnectionError) else 'grok_sdk_connection_unknown'
                with self.bridge.history.db:
                    self.bridge.history.db.execute('UPDATE grok_sdk_connections SET state=?,reason=? WHERE request_id=?',
                        ('unknown', _safe_reason(reason), request_id))
                raise ConnectionError(_safe_reason(reason), 409) from None
            finally:
                with self._lock:
                    self._connecting.discard(request_id)
        return response
