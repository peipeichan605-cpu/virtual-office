"""Installation-local phone authorization, independent of owner/platform keys.

The HTTP boundary supplies a verified Tailscale identity and checks Host/Origin.
This store never trusts a client-supplied principal or calls command dispatch.
Callers that also take the commands lock must take it before this store's lock.
"""
import base64
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from private_files import ensure_private_directory, prepare_private_file, require_private


class MobileAuthError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


class MobilePrincipal(dict):
    """JSON-safe public metadata, with process-private authentication evidence."""
    __slots__ = ('_issuer', '_session_hash', '_identity', '_write_verified')


def _digest(kind, value):
    return hashlib.sha256(('office-mobile/' + kind + '\0' + value).encode()).hexdigest()


def _token(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{43}', value) is not None


def _csrf(token):
    # The high-entropy HttpOnly token is the HMAC key, not its persisted hash.
    return hmac.new(token.encode(), b'office-mobile/csrf/v1', hashlib.sha256).hexdigest()


def _identity(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 254
            or value != value.strip() or any(ord(c) < 33 or ord(c) == 127 for c in value)):
        raise MobileAuthError('mobile_identity_required', 401)
    return value


def _number(value, maximum):
    if (type(value) not in (float, int) or not math.isfinite(value)
            or not 0 < value <= maximum):
        raise ValueError('Invalid mobile security duration')
    return float(value)


class MobileAuth:
    SESSION_TTL = 86400
    IDLE_TTL = 3600
    PAIRING_TTL = 300
    RENEWAL_TTL = 30 * 86400
    RENEWAL_IDLE_TTL = 7 * 86400
    COMMAND_TTL = 900
    MAX_DEVICES = 32
    MAX_PAIRINGS = 8
    MAX_AUDIT = 2000
    MAX_RATE_BUCKETS = 512
    # Fixed-window budgets: static assets 240/min per verified identity (1200
    # globally), API reads 360/min per authenticated device (1800 globally),
    # and authentication 600/min per identity (2400 globally). The HTTP caller
    # supplies these subject scopes and must not charge assets to API reads.
    # This permits a ~90-file 3D startup plus normal status/dialogue polling.
    # Writes stay 30/min per subject (120 globally); pairing is 8 attempts/5min
    # (32 globally), and owner pairing creation is 12/5min (24 globally).
    RATE_LIMITS = {'asset': (240, 60), 'read': (360, 60),
                   'write': (30, 60), 'pair': (8, 300),
                   'auth': (600, 60), 'pair_create': (12, 300)}
    GLOBAL_LIMITS = {'asset': 1200, 'read': 1800, 'write': 120, 'pair': 32,
                     'auth': 2400, 'pair_create': 24}

    def __init__(self, config, selected_agents=None, *, clock=None,
                 session_ttl=SESSION_TTL, idle_ttl=IDLE_TTL,
                 pairing_ttl=PAIRING_TTL):
        self.config = config
        self.clock = clock or time.time
        self.session_ttl = _number(session_ttl, self.SESSION_TTL)
        self.idle_ttl = _number(idle_ttl, self.IDLE_TTL)
        self.pairing_ttl = _number(pairing_ttl, self.PAIRING_TTL)
        self.selected_agents = selected_agents or self._config_agents
        self.lock = threading.RLock()
        self._issuer = object()
        self._rates = OrderedDict()
        root = Path(config['data_dir']).expanduser()
        try:
            ensure_private_directory(root)
        except (ValueError, OSError) as error:
            raise MobileAuthError('mobile_private_directory_required', 503) from error
        path = root / 'mobile-auth.sqlite3'
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        except FileExistsError:
            pass
        else:
            try:
                prepare_private_file(path, fd)
            except (ValueError, OSError) as error:
                raise MobileAuthError('mobile_private_database_required', 503) from error
            finally:
                os.close(fd)
        try:
            require_private(path)
        except (ValueError, OSError) as error:
            raise MobileAuthError('mobile_private_database_required', 503) from error
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=3, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA secure_delete=ON')
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS mobile_pairings (
                id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
                identity TEXT NOT NULL, label TEXT NOT NULL,
                view_agents TEXT NOT NULL, send_agents TEXT NOT NULL,
                bindings TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS mobile_devices (
                id TEXT PRIMARY KEY, session_hash TEXT NOT NULL UNIQUE,
                csrf_hash TEXT NOT NULL, identity TEXT NOT NULL, label TEXT NOT NULL,
                view_agents TEXT NOT NULL, send_agents TEXT NOT NULL,
                bindings TEXT NOT NULL, revision INTEGER NOT NULL,
                created_at REAL NOT NULL, expires_at REAL NOT NULL,
                last_seen REAL NOT NULL, idle_ttl REAL NOT NULL, revoked_at REAL);
            CREATE TABLE IF NOT EXISTS mobile_audit (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
                event TEXT NOT NULL, device_id TEXT, reason TEXT);
            CREATE TABLE IF NOT EXISTS mobile_membership_epochs (
                agent_id TEXT PRIMARY KEY, epoch INTEGER NOT NULL);
        ''')
        # Existing installations keep their explicit, membership-bound grants.
        for table in ('mobile_pairings', 'mobile_devices'):
            columns = {row[1] for row in self.db.execute('PRAGMA table_info(' + table + ')')}
            for name in ('view_mode', 'send_mode'):
                if name not in columns:
                    self.db.execute('ALTER TABLE ' + table + ' ADD COLUMN ' + name + " TEXT NOT NULL DEFAULT 'selected'")

        # A remembered grant is separate from the short access session. Old
        # rows remain non-remembered; migration must never extend existing access.
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(mobile_devices)')}
        for name, definition in (
                ('renewal_hash', 'TEXT'), ('renewal_nonce', 'TEXT'),
                ('renewal_expires_at', 'REAL'), ('renewal_last_seen', 'REAL')):
            if name not in columns:
                self.db.execute('ALTER TABLE mobile_devices ADD COLUMN ' + name + ' ' + definition)
        self.db.execute('CREATE UNIQUE INDEX IF NOT EXISTS mobile_renewal_hash ON mobile_devices(renewal_hash)')

    def _config_agents(self):
        result = {}
        for agent in self.config.get('agents', []):
            ident = agent.get('id') or agent.get('provider', '') + ':' + agent.get('source_id', '')
            record = {key: agent.get(key) for key in ('provider', 'driver', 'source_id', 'token_sha256')}
            record['binding'] = self.config.get('command_bindings', {}).get(ident)
            result[ident] = _digest('membership', json.dumps(record, sort_keys=True, separators=(',', ':')))
        return result

    def _selected(self):
        selected = self.selected_agents()
        if isinstance(selected, dict):
            if any(not isinstance(k, str) or not isinstance(v, str) or not v or len(v) > 512
                   for k, v in selected.items()):
                raise MobileAuthError('mobile_membership_unavailable', 503)
            return dict(selected)
        if isinstance(selected, (list, tuple, set, frozenset)) and all(isinstance(v, str) for v in selected):
            return {v: v for v in selected}
        raise MobileAuthError('mobile_membership_unavailable', 503)

    @contextmanager
    def _transaction(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
            else:
                self.db.execute('COMMIT')

    def _audit(self, event, device_id=None, reason=None):
        self.db.execute('INSERT INTO mobile_audit(at,event,device_id,reason) VALUES (?,?,?,?)',
                        (self.clock(), event, device_id, reason))
        self.db.execute('DELETE FROM mobile_audit WHERE seq <= (SELECT COALESCE(MAX(seq),0)-? FROM mobile_audit)',
                        (self.MAX_AUDIT,))

    def _prune(self, now):
        self.db.execute('DELETE FROM mobile_pairings WHERE expires_at<=?', (now,))
        # Keep bounded, non-secret incident metadata for the most recent devices.
        self.db.execute('''DELETE FROM mobile_devices WHERE id IN
            (SELECT id FROM mobile_devices WHERE revoked_at IS NOT NULL OR
             ((expires_at<=? OR last_seen+idle_ttl<=?) AND
              (renewal_hash IS NULL OR renewal_expires_at<=? OR renewal_last_seen+?<=?))
             ORDER BY created_at DESC LIMIT -1 OFFSET 128)''', (now, now, now, self.RENEWAL_IDLE_TTL, now))

    def allow_request(self, subject, kind='read'):
        """Bounded fixed-window limits; callers pass verified identity, not tokens."""
        if kind not in self.RATE_LIMITS or not isinstance(subject, str) or not 1 <= len(subject) <= 1024:
            raise MobileAuthError('invalid_mobile_rate_subject')
        now = self.clock()
        limit, window = self.RATE_LIMITS[kind]
        key = (kind, _digest('rate', subject))
        global_key = (kind, '*')
        with self.lock:
            for old_key, (end, _) in list(self._rates.items()):
                if end <= now:
                    del self._rates[old_key]
            # Do not evict an unexpired limiter merely because new identities arrive.
            needed = sum(k not in self._rates for k in (key, global_key))
            if len(self._rates) + needed > self.MAX_RATE_BUCKETS:
                raise MobileAuthError('mobile_rate_limited', 429)
            current = self._rates.get(key, (now + window, 0))
            global_current = self._rates.get(global_key, (now + window, 0))
            if current[1] >= limit or global_current[1] >= self.GLOBAL_LIMITS[kind]:
                raise MobileAuthError('mobile_rate_limited', 429)
            self._rates[key] = (current[0], current[1] + 1)
            self._rates[global_key] = (global_current[0], global_current[1] + 1)
        return True

    @staticmethod
    def _scopes(view_agents, send_agents, view_mode='selected', send_mode='selected'):
        if view_mode not in ('selected', 'enrolled') or send_mode not in ('selected', 'enrolled'):
            raise MobileAuthError('invalid_mobile_scope')
        if send_mode == 'enrolled' and view_mode != 'enrolled':
            raise MobileAuthError('invalid_mobile_scope')
        if (not isinstance(view_agents, (list, tuple)) or not isinstance(send_agents, (list, tuple))
                or len(view_agents) > 256 or len(send_agents) > 256
                or any(not isinstance(v, str) for v in (*view_agents, *send_agents))):
            raise MobileAuthError('invalid_mobile_scope')
        views, sends = set(view_agents), set(send_agents)
        if (len(views) != len(view_agents) or len(sends) != len(send_agents)
                or view_mode == 'selected' and (not views or not sends <= views)):
            raise MobileAuthError('invalid_mobile_scope')
        # Following the roster is an owner policy, not a snapshot grant. Do
        # not retain redundant selected IDs that could revoke unrelated work
        # when one member logs out. Only selected-mode grants have bindings.
        return (sorted(views) if view_mode == 'selected' else [],
                sorted(sends) if send_mode == 'selected' else [])

    def create_pairing(self, identity, label, view_agents, send_agents,
                       view_mode='selected', send_mode='selected'):
        """Owner-only entry point: HTTP caller must independently verify owner."""
        identity = _identity(identity)
        self.allow_request(identity, 'pair_create')
        if (not isinstance(label, str) or not 1 <= len(label.strip()) <= 80
                or any(ord(c) < 32 or ord(c) == 127 for c in label)):
            raise MobileAuthError('invalid_mobile_label')
        view_list, send_list = self._scopes(view_agents, send_agents, view_mode, send_mode)
        with self._transaction():
            now = self.clock()
            selected = self._selected()
            if not set(view_list + send_list) <= selected.keys():
                raise MobileAuthError('mobile_agent_not_selected', 403)
            self._prune(now)
            count = self.db.execute('SELECT COUNT(*) FROM mobile_pairings').fetchone()[0]
            if count >= self.MAX_PAIRINGS:
                raise MobileAuthError('mobile_pairing_limit', 429)
            token = secrets.token_urlsafe(32)
            ident = str(uuid.uuid4())
            expires = now + self.pairing_ttl
            bound = set(view_list + send_list)
            self.db.execute('''INSERT INTO mobile_pairings
                (id,token_hash,identity,label,view_agents,send_agents,bindings,created_at,expires_at,view_mode,send_mode)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                            (ident, _digest('pair', token), identity, label.strip(), json.dumps(view_list),
                             json.dumps(send_list), json.dumps({a: selected[a] for a in bound}, sort_keys=True), now, expires,
                             view_mode, send_mode))
            self._audit('pairing_created')
            return {'pairing_id': ident, 'pairing_token': token, 'expires_at': expires,
                    'identity': identity, 'label': label.strip(), 'view_agents': view_list, 'send_agents': send_list,
                    'view_mode': view_mode, 'send_mode': send_mode}

    def pair(self, token, identity, subject=None, *, remember_device=False):
        # Apply global throttling even for malformed identities or random tokens.
        self.allow_request(subject if isinstance(subject, str) and subject else 'pairing', 'pair')
        identity = _identity(identity)
        if type(remember_device) is not bool:
            raise MobileAuthError('invalid_mobile_remember_choice')
        if not _token(token):
            raise MobileAuthError('invalid_mobile_pairing', 401)
        with self._transaction():
            now = self.clock()
            row = self.db.execute('SELECT * FROM mobile_pairings WHERE token_hash=?', (_digest('pair', token),)).fetchone()
            if not row or row['expires_at'] <= now or not hmac.compare_digest(row['identity'].encode(), identity.encode()):
                raise MobileAuthError('invalid_mobile_pairing', 401)
            selected = self._selected()
            bindings = json.loads(row['bindings'])
            if any(selected.get(a) != generation for a, generation in bindings.items()):
                raise MobileAuthError('mobile_pairing_membership_changed', 403)
            active = self.db.execute('''SELECT COUNT(*) FROM mobile_devices
                WHERE revoked_at IS NULL AND ((expires_at>? AND last_seen+idle_ttl>?) OR
                 (renewal_hash IS NOT NULL AND renewal_expires_at>? AND renewal_last_seen+?>?))''',
                 (now, now, now, self.RENEWAL_IDLE_TTL, now)).fetchone()[0]
            if active >= self.MAX_DEVICES:
                raise MobileAuthError('mobile_device_limit', 429)
            renewal = secrets.token_urlsafe(32) if remember_device else None
            nonce = secrets.token_urlsafe(18) if remember_device else None
            session = self._access_token(renewal, nonce) if renewal else secrets.token_urlsafe(32)
            csrf = _csrf(session)
            device_id = 'phone-' + uuid.uuid4().hex
            self.db.execute('''INSERT INTO mobile_devices
                (id,session_hash,csrf_hash,identity,label,view_agents,send_agents,bindings,revision,
                 created_at,expires_at,last_seen,idle_ttl,revoked_at,view_mode,send_mode)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                            (device_id, _digest('session', session), _digest('csrf', csrf), identity,
                             row['label'], row['view_agents'], row['send_agents'], row['bindings'],
                             1, now, now + self.session_ttl, now, self.idle_ttl, None, row['view_mode'], row['send_mode']))
            if renewal:
                self.db.execute('''UPDATE mobile_devices SET renewal_hash=?,renewal_nonce=?,
                    renewal_expires_at=?,renewal_last_seen=? WHERE id=?''',
                    (_digest('renewal', renewal), nonce, now + self.RENEWAL_TTL, now, device_id))
            self.db.execute('DELETE FROM mobile_pairings WHERE id=?', (row['id'],))
            self._prune(now)
            self._audit('device_paired', device_id)
            device = self.db.execute('SELECT * FROM mobile_devices WHERE id=?', (device_id,)).fetchone()
            return {'session_token': session, 'csrf_token': csrf, 'device': self._public(device, now),
                    **({'renewal_token': renewal} if renewal else {})}

    @staticmethod
    def _access_token(renewal, nonce):
        # Repeated/concurrent renewal requests return the same current access
        # token without persisting either bearer secret. Only access tokens
        # rotate; the bounded renewal grant never gets a later absolute expiry.
        return base64.urlsafe_b64encode(hmac.new(renewal.encode(),
            ('office-mobile/access/v1/' + nonce).encode(), hashlib.sha256).digest()).decode().rstrip('=')

    def _renewable(self, row, now):
        return bool(row and row['revoked_at'] is None and row['renewal_hash']
                    and row['renewal_expires_at'] > now
                    and row['renewal_last_seen'] + self.RENEWAL_IDLE_TTL > now)

    def _renewal_row(self, token, identity, now):
        identity = _identity(identity)
        row = self.db.execute('SELECT * FROM mobile_devices WHERE renewal_hash=?',
            (_digest('renewal', token),)).fetchone() if _token(token) else None
        if not self._renewable(row, now) or not hmac.compare_digest(row['identity'].encode(), identity.encode()):
            raise MobileAuthError('mobile_auth_required', 401)
        return row

    def renewal_device(self, token, identity):
        """Validate a remembered grant, without extending it or issuing access."""
        with self.lock:
            return self._public(self._renewal_row(token, identity, self.clock()))

    def renew(self, token, identity):
        """Same-origin HTTP boundary required. Preserve device/scopes; rotate access.

        The HTTP renewal cookie is immutable and expires after 30 days, or
        seven days without authenticated use. It cannot authenticate an API
        read/write on its own. No prior authorization is restored on renewal.
        """
        self.allow_request(identity if isinstance(identity, str) else 'invalid-identity', 'auth')
        with self._transaction():
            now = self.clock()
            row = self._renewal_row(token, identity, now)
            if not self._live(row, now):
                nonce = secrets.token_urlsafe(18)
                session = self._access_token(token, nonce)
                self.db.execute('''UPDATE mobile_devices SET renewal_nonce=?,session_hash=?,csrf_hash=?,
                    expires_at=?,last_seen=?,renewal_last_seen=?,revision=revision+1 WHERE id=?''',
                    (nonce, _digest('session', session), _digest('csrf', _csrf(session)),
                     min(now + self.session_ttl, row['renewal_expires_at']), now, now, row['id']))
                self._audit('device_session_renewed', row['id'])
            else:
                session = self._access_token(token, row['renewal_nonce'])
                self.db.execute('UPDATE mobile_devices SET renewal_last_seen=?,last_seen=? WHERE id=?', (now, now, row['id']))
            row = self.db.execute('SELECT * FROM mobile_devices WHERE id=?', (row['id'],)).fetchone()
            return {'session_token': session, 'csrf_token': _csrf(session), 'device': self._public(row, now)}

    def remember(self, token, identity, csrf, enabled):
        """Explicit CSRF-verified browser choice; never called by automatic renewal."""
        if type(enabled) is not bool:
            raise MobileAuthError('invalid_mobile_remember_choice')
        principal = self.authenticate(token, identity, csrf=csrf, write=True)
        with self._transaction():
            now = self.clock()
            row = self._principal_row(principal, now, write=True)
            if enabled:
                renewal, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(18)
                session = self._access_token(renewal, nonce)
                self.db.execute('''UPDATE mobile_devices SET renewal_hash=?,renewal_nonce=?,renewal_expires_at=?,
                    renewal_last_seen=?,session_hash=?,csrf_hash=?,expires_at=?,last_seen=?,revision=revision+1 WHERE id=?''',
                    (_digest('renewal', renewal), nonce, now + self.RENEWAL_TTL, now, _digest('session', session),
                     _digest('csrf', _csrf(session)), now + self.session_ttl, now, row['id']))
            else:
                renewal, session = None, token
                self.db.execute('''UPDATE mobile_devices SET renewal_hash=NULL,renewal_nonce=NULL,
                    renewal_expires_at=NULL,renewal_last_seen=NULL,revision=revision+1 WHERE id=?''', (row['id'],))
            self._audit('device_remember_enabled' if enabled else 'device_remember_disabled', row['id'])
            row = self.db.execute('SELECT * FROM mobile_devices WHERE id=?', (row['id'],)).fetchone()
            return {'session_token': session, 'csrf_token': _csrf(session), 'device': self._public(row, now),
                    **({'renewal_token': renewal} if renewal else {})}

    def _live(self, row, now):
        return bool(row and row['revoked_at'] is None and row['expires_at'] > now
                    and row['last_seen'] + row['idle_ttl'] > now)

    def _public(self, row, now=None):
        now = self.clock() if now is None else now
        selected = self._selected()
        bindings = json.loads(row['bindings'])
        current = {a for a, generation in bindings.items() if selected.get(a) == generation}
        views = sorted(selected) if row['view_mode'] == 'enrolled' else [a for a in json.loads(row['view_agents']) if a in current]
        sends = sorted(selected) if row['send_mode'] == 'enrolled' else [a for a in json.loads(row['send_agents']) if a in current]
        return {'device_id': row['id'], 'label': row['label'], 'identity': row['identity'],
                'view_agents': views, 'send_agents': [a for a in sends if a in views],
                'view_mode': row['view_mode'], 'send_mode': row['send_mode'],
                'revision': row['revision'], 'created_at': row['created_at'],
                'expires_at': row['expires_at'], 'idle_expires_at': min(row['expires_at'], row['last_seen'] + row['idle_ttl']),
                'last_seen': row['last_seen'], 'revoked_at': row['revoked_at'],
                'remembered': row['renewal_expires_at'] is not None,
                'renewal_expires_at': row['renewal_expires_at'],
                'renewal_idle_expires_at': min(row['renewal_expires_at'], row['renewal_last_seen'] + self.RENEWAL_IDLE_TTL)
                    if row['renewal_expires_at'] is not None else None,
                'state': 'revoked' if row['revoked_at'] is not None else 'ready' if self._live(row, now)
                    else 'renewal_required' if self._renewable(row, now) else 'expired'}

    def authenticate(self, token, identity, csrf=None, write=False):
        self.allow_request(identity if isinstance(identity, str) and 1 <= len(identity) <= 1024 else 'invalid-identity', 'auth')
        identity = _identity(identity)
        if not _token(token):
            raise MobileAuthError('mobile_auth_required', 401)
        key = _digest('session', token)
        with self._transaction():
            now = self.clock()
            row = self.db.execute('SELECT * FROM mobile_devices WHERE session_hash=?', (key,)).fetchone()
            if not self._live(row, now) or not hmac.compare_digest(row['identity'].encode(), identity.encode()):
                raise MobileAuthError('mobile_auth_required', 401)
            if write and (not isinstance(csrf, str) or len(csrf) != 64
                          or not hmac.compare_digest(row['csrf_hash'], _digest('csrf', csrf))):
                raise MobileAuthError('mobile_csrf_required', 403)
            self.db.execute('''UPDATE mobile_devices SET last_seen=?,renewal_last_seen=CASE WHEN renewal_hash IS NOT NULL
                THEN ? ELSE renewal_last_seen END WHERE id=?''', (now, now, row['id']))
            row = self.db.execute('SELECT * FROM mobile_devices WHERE id=?', (row['id'],)).fetchone()
            result = MobilePrincipal(self._public(row, now))
            result._issuer, result._session_hash = self._issuer, key
            result._identity, result._write_verified = identity, bool(write)
            return result

    def _principal_row(self, principal, now, write=False):
        if not isinstance(principal, MobilePrincipal) or principal._issuer is not self._issuer:
            raise MobileAuthError('mobile_auth_required', 401)
        row = self.db.execute('SELECT * FROM mobile_devices WHERE session_hash=?', (principal._session_hash,)).fetchone()
        if (not self._live(row, now) or row['identity'] != principal._identity
                or row['id'] != principal.get('device_id')):
            raise MobileAuthError('mobile_auth_required', 401)
        if row['revision'] != principal.get('revision'):
            # An owner policy update may race a request authenticated just
            # before it. Fence that request without revoking a valid cookie;
            # the next request will authenticate against the new revision.
            raise MobileAuthError('mobile_scope_changed', 409)
        if write and not principal._write_verified:
            raise MobileAuthError('mobile_csrf_required', 403)
        return row

    def authorize(self, principal, agent, action='view'):
        if action not in {'view', 'send'} or not isinstance(agent, str):
            raise MobileAuthError('invalid_mobile_action')
        with self.lock:
            row = self._principal_row(principal, self.clock(), write=action == 'send')
            public = self._public(row)
            if agent not in public[action + '_agents']:
                raise MobileAuthError('mobile_agent_forbidden', 403)
            return principal

    def refresh(self, principal):
        """Revalidate current owner policy without extending idle time."""
        with self.lock:
            row = self._principal_row(principal, self.clock())
            result = MobilePrincipal(self._public(row))
            result._issuer, result._session_hash = self._issuer, principal._session_hash
            result._identity, result._write_verified = principal._identity, principal._write_verified
            return result

    def csrf_for(self, token, identity):
        self.authenticate(token, identity)
        return _csrf(token)

    def session_status(self, token, identity):
        principal = self.authenticate(token, identity)
        return {'device': dict(principal), 'csrf_token': _csrf(token)}

    def command_origin(self, principal, agent_id):
        with self.lock:
            self.authorize(principal, agent_id, 'send')
            origin = {'device_id': principal['device_id'], 'revision': principal['revision'],
                      'expires_at': min(principal['expires_at'], self.clock() + self.COMMAND_TTL), 'agent_id': agent_id}
            row = self._principal_row(principal, self.clock(), write=True)
            if row['send_mode'] == 'enrolled':
                origin['membership'] = self._command_membership(agent_id, self._selected()[agent_id])
            return origin

    def _command_membership(self, agent_id, generation):
        epoch = self.db.execute('SELECT epoch FROM mobile_membership_epochs WHERE agent_id=?', (agent_id,)).fetchone()
        # A durable logout epoch also fences a same-ID rejoin whose definition
        # happens to be byte-for-byte identical to the retired membership.
        return _digest('command-membership', json.dumps([agent_id, generation, epoch[0] if epoch else 0]))

    def validate_command_origin(self, origin, agent_id=None):
        """A dispatch fence; does not extend a phone's idle lifetime."""
        if (not isinstance(origin, dict) or set(origin) - {'device_id', 'revision', 'expires_at', 'agent_id', 'membership'}
                or not {'device_id', 'revision', 'expires_at'} <= origin.keys()
                or not isinstance(origin['device_id'], str) or type(origin['revision']) is not int
                or type(origin['expires_at']) not in (int, float) or not math.isfinite(origin['expires_at'])
                or origin['revision'] < 1):
            return False
        target = agent_id or origin.get('agent_id')
        if not isinstance(target, str) or ('agent_id' in origin and origin['agent_id'] != target):
            return False
        try:
            with self.lock:
                now = self.clock()
                if not now < origin['expires_at'] <= now + self.COMMAND_TTL:
                    return False
                row = self.db.execute('SELECT * FROM mobile_devices WHERE id=?', (origin['device_id'],)).fetchone()
                generation = self._selected().get(target)
                fingerprint = origin.get('membership')
                if fingerprint is not None and (not isinstance(fingerprint, str) or generation is None
                        or not hmac.compare_digest(fingerprint, self._command_membership(target, generation))):
                    return False
                if row and row['send_mode'] == 'enrolled' and fingerprint is None:
                    return False
                return bool(self._live(row, now) and row['revision'] == origin['revision']
                            and origin['expires_at'] <= row['expires_at']
                            and target in self._public(row, now)['send_agents'])
        except (ValueError, TypeError, KeyError, sqlite3.Error):
            return False

    def list_devices(self):
        with self.lock:
            return [self._public(row) for row in self.db.execute('SELECT * FROM mobile_devices ORDER BY created_at DESC,id')]

    def update_scopes(self, device_id, view_agents, send_agents, view_mode='selected', send_mode='selected'):
        """Owner-only policy change; retain cookie, fence all older commands."""
        if not isinstance(device_id, str):
            raise MobileAuthError('invalid_mobile_device')
        views, sends = self._scopes(view_agents, send_agents, view_mode, send_mode)
        with self._transaction():
            now = self.clock()
            row = self.db.execute('SELECT * FROM mobile_devices WHERE id=?', (device_id,)).fetchone()
            if row is None:
                raise MobileAuthError('mobile_device_not_found', 404)
            if not (self._live(row, now) or self._renewable(row, now)):
                raise MobileAuthError('mobile_device_unavailable', 409)
            selected = self._selected()
            if not set(views + sends) <= selected.keys():
                raise MobileAuthError('mobile_agent_not_selected', 403)
            bound = {a: selected[a] for a in set(views + sends)}
            values = (json.dumps(views), json.dumps(sends), json.dumps(bound, sort_keys=True), view_mode, send_mode)
            old = tuple(row[key] for key in ('view_agents','send_agents','bindings','view_mode','send_mode'))
            changed = values != old
            if changed:
                self.db.execute('''UPDATE mobile_devices SET view_agents=?,send_agents=?,bindings=?,view_mode=?,send_mode=?,
                    revision=revision+1 WHERE id=?''', (*values, device_id))
                self._audit('device_scopes_changed', device_id)
            device = self._public(self.db.execute('SELECT * FROM mobile_devices WHERE id=?',(device_id,)).fetchone(), now)
            return {'device': device, 'changed': changed}

    def revoke(self, device_id):
        if not isinstance(device_id, str):
            raise MobileAuthError('invalid_mobile_device')
        with self._transaction():
            row = self.db.execute('SELECT * FROM mobile_devices WHERE id=?', (device_id,)).fetchone()
            if row is None:
                raise MobileAuthError('mobile_device_not_found', 404)
            if row['revoked_at'] is None:
                self.db.execute('UPDATE mobile_devices SET revoked_at=?,revision=revision+1,renewal_hash=NULL,renewal_nonce=NULL WHERE id=?',
                                (self.clock(), device_id))
                self._audit('device_revoked', device_id)
            return self._public(self.db.execute('SELECT * FROM mobile_devices WHERE id=?', (device_id,)).fetchone())

    def invalidate_agent(self, agent_id):
        """Called under commands.lock on logout, before the native ID can rejoin."""
        if not isinstance(agent_id, str):
            raise MobileAuthError('invalid_mobile_agent')
        affected = []
        with self._transaction():
            self.db.execute('''INSERT INTO mobile_membership_epochs(agent_id,epoch) VALUES (?,1)
                ON CONFLICT(agent_id) DO UPDATE SET epoch=epoch+1''', (agent_id,))
            for row in self.db.execute('SELECT * FROM mobile_devices WHERE revoked_at IS NULL').fetchall():
                views, sends, bindings = json.loads(row['view_agents']), json.loads(row['send_agents']), json.loads(row['bindings'])
                if agent_id not in bindings:
                    continue
                bindings.pop(agent_id)
                self.db.execute('UPDATE mobile_devices SET view_agents=?,send_agents=?,bindings=?,revision=revision+1 WHERE id=?',
                                (json.dumps([a for a in views if a != agent_id]), json.dumps([a for a in sends if a != agent_id]),
                                 json.dumps(bindings, sort_keys=True), row['id']))
                self._audit('agent_scope_removed', row['id'])
                affected.append(row['id'])
            for row in self.db.execute('SELECT id,bindings FROM mobile_pairings').fetchall():
                if agent_id in json.loads(row['bindings']):
                    self.db.execute('DELETE FROM mobile_pairings WHERE id=?', (row['id'],))
        return affected

    def audit_events(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise MobileAuthError('invalid_mobile_audit_limit')
        with self.lock:
            return [dict(row) for row in self.db.execute('SELECT * FROM mobile_audit ORDER BY seq DESC LIMIT ?', (limit,))]

    def close(self):
        with self.lock:
            self.db.close()
