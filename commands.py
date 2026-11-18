"""Owner-submitted commands, durable receipts and original-session correlation.

The dispatcher is deterministic Python. Providers stay behind adapters; selected
agents get a private mailbox while native execution requires a saved binding.
"""
import hashlib
import hmac
from contextlib import nullcontext
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from command_adapters import DeliveryError, make_adapters
from history import redact, timestamp
from security_keys import integrity_fingerprint, matches_integrity

TERMINAL = {'completed', 'failed', 'cancelled'}
MARKER = re.compile(r'^\[OFFICE:([0-9a-f-]{36})\]\n')
MOBILE_COMMAND_TTL = 900


class CommandError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


class Commands:
    def __init__(self, config, history, enabled=True, adapters=None, on_status=None):
        self.config, self.history, self.enabled = config, history, enabled
        self.on_status = on_status
        # The gateway installs a validator before starting the dispatcher.
        # Missing or unavailable mobile authorization always fails closed.
        self.mobile_authorizer = None
        self.bindings = config.get('command_bindings', {})
        self.adapters = make_adapters(config, self.observe_native) if adapters is None else adapters
        self.lock = threading.RLock()
        path = Path(config['data_dir']) / 'commands.sqlite3'
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=3)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA secure_delete=ON')
        self.db.execute('''CREATE TABLE IF NOT EXISTS commands (
            id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, session_id TEXT NOT NULL,
            adapter TEXT NOT NULL, fingerprint TEXT NOT NULL, preview TEXT NOT NULL,
            payload TEXT, state TEXT NOT NULL, native_message_id TEXT, run_id TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, detail_code TEXT)''')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(commands)')}
        for name, kind in (('source_device', 'TEXT'), ('source_revision', 'INTEGER'),
                           ('source_expires_at', 'REAL'), ('source_membership', 'TEXT')):
            if name not in columns:
                self.db.execute('ALTER TABLE commands ADD COLUMN ' + name + ' ' + kind)
        self.db.execute('CREATE INDEX IF NOT EXISTS command_agent_time ON commands(agent_id,created_at)')
        self.db.execute('CREATE INDEX IF NOT EXISTS command_mobile_source ON commands(source_device,state)')
        self.db.execute('''CREATE TABLE IF NOT EXISTS command_reconciliations (
            id TEXT PRIMARY KEY, command_id TEXT NOT NULL UNIQUE, agent_id TEXT NOT NULL,
            prior_state TEXT NOT NULL, prior_run_id TEXT, command_fingerprint TEXT NOT NULL,
            terminal_event_id TEXT NOT NULL, terminal_event_fingerprint TEXT NOT NULL,
            review_note_sha256 TEXT NOT NULL, provenance TEXT NOT NULL, created_at TEXT NOT NULL)''')
        # Detaching from Office is not a native completion or cancellation.
        # Keep the original unknown command untouched and fence its callbacks.
        self.db.execute('''CREATE TABLE IF NOT EXISTS command_detachments (
            command_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, prior_state TEXT NOT NULL,
            native_message_id TEXT, run_id TEXT, detached_at TEXT NOT NULL)''')
        # Never replay an interrupted external side effect. Original-session
        # observations can still reconcile an uncertain delivery after restart.
        self.db.execute("UPDATE commands SET state='delivery_unknown',payload=NULL,detail_code='interrupted_delivery',updated_at=? WHERE state='sending'", (timestamp(),))
        self.db.commit()
        self.migrate_pending_inbox()
        retired=set(config.get('logged_out_agent_ids',[]))-{a['id'] for a in config.get('agents',[])}
        for agent in retired:self.detach_unconfirmed(agent,commit=False)
        self.db.commit()
        self.recovery_pending=True
        self.reconcile()
        # A restarted Office no longer owns an in-flight ACP request. Preserve
        # the uncertainty and history; never replay a possibly executed prompt.
        self.db.execute("UPDATE commands SET state='delivery_unknown',payload=NULL,detail_code='interrupted_delivery',updated_at=? WHERE adapter IN ('hermes_acp','cursor_local','codex_background') AND state IN ('queued','running','waiting_user')", (timestamp(),))
        self.db.commit()

    def restore_observations(self):
        # GET a recorded native run only after the owning Bridge is initialized.
        with self.lock:
            rows=self.db.execute("SELECT * FROM commands WHERE adapter='cursor_cloud' AND native_message_id IS NOT NULL AND state IN ('queued','running','waiting_user','delivery_unknown') AND id NOT IN (SELECT command_id FROM command_detachments)").fetchall()
        for row in rows:
            with self.lock:
                binding=self.binding(row['agent_id'])
                adapter=self.adapters.get('cursor_cloud')
                if self.enabled and not self.detached(row['id']) and binding=={'adapter':'cursor_cloud','session_id':row['session_id']} and hasattr(adapter,'resume'):
                    with self.history.lock:
                        ids=self.history.db.execute('SELECT event_id FROM messages WHERE agent_id=? AND run_id=? AND role=?',(row['agent_id'],row['id'],'assistant')).fetchall()
                    segments=[int(item[0].rsplit(':',1)[-1]) for item in ids if item[0] and item[0].startswith('hermes:'+row['id']+':text:') and item[0].rsplit(':',1)[-1].isdigit()]
                    adapter.resume(row['session_id'],row['id'],row['native_message_id'],row['preview'],recovery=True,last_segment=max(segments,default=0))

    def reconcile(self):
        rows=self.db.execute("SELECT agent_id,run_id FROM commands WHERE run_id IS NOT NULL AND state NOT IN ('completed','failed','cancelled') AND id NOT IN (SELECT command_id FROM command_detachments)").fetchall()
        for row in rows:
            with self.history.lock:
                run=self.history.db.execute('SELECT status FROM runs WHERE agent_id=? AND run_id=?',(row['agent_id'],row['run_id'])).fetchone()
            if run:self.observe_run(row['agent_id'],row['run_id'],run['status'])

    def fingerprint(self, agent, text):
        return integrity_fingerprint(self.config, (agent + '\0' + text).encode())

    def matches_fingerprint(self, agent, text, expected):
        """Legacy dedupe proofs survive owner-key migration without replay."""
        return matches_integrity(self.config, (agent + '\0' + text).encode(), expected)

    @staticmethod
    def _origin(origin, agent, *, creating=False):
        if origin is None:
            return None
        if (not isinstance(origin, dict) or
                not {'device_id', 'revision', 'expires_at'} <= set(origin) or
                set(origin) - {'device_id', 'revision', 'expires_at', 'agent_id', 'membership'} or
                not isinstance(origin.get('device_id'), str) or
                not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', origin['device_id']) or
                type(origin.get('revision')) is not int or origin['revision'] < 1 or
                type(origin.get('expires_at')) not in (int, float) or
                not math.isfinite(origin['expires_at']) or
                ('agent_id' in origin and origin['agent_id'] != agent) or
                ('membership' in origin and
                 (not isinstance(origin['membership'], str) or not 1 <= len(origin['membership']) <= 512
                  or any(ord(ch) < 32 or ord(ch) == 127 for ch in origin['membership'])))):
            raise CommandError(400, 'invalid_mobile_origin')
        now = time.time()
        if creating and origin['expires_at'] > now + MOBILE_COMMAND_TTL:
            raise CommandError(400, 'invalid_mobile_origin')
        return {**origin, 'agent_id': agent}

    def _origin_allowed(self, origin, agent):
        if origin is None:
            return True
        if origin['expires_at'] <= time.time():
            return False
        try:
            return callable(self.mobile_authorizer) and self.mobile_authorizer(origin, agent) is True
        except Exception:
            return False

    @staticmethod
    def _row_origin(row):
        if (row['source_device'] is None and row['source_revision'] is None
                and row['source_expires_at'] is None and row['source_membership'] is None):
            return None
        origin = {'device_id': row['source_device'], 'revision': row['source_revision'],
                  'expires_at': row['source_expires_at'], 'agent_id': row['agent_id']}
        if row['source_membership'] is not None:
            origin['membership'] = row['source_membership']
        return origin

    @staticmethod
    def _same_origin(row, origin):
        if origin is None:
            return (row['source_device'] is None and row['source_revision'] is None
                    and row['source_expires_at'] is None and row['source_membership'] is None)
        return (row['source_device'] == origin['device_id'] and row['source_revision'] == origin['revision']
                and row['source_membership'] == origin.get('membership'))

    def _cancel_mobile_row(self, row, reason):
        """The caller holds commands.lock; an inbox receipt has execute=False.

        A native dispatch already in flight cannot be undone here. An inbox
        receipt still needs a separate start authorization before execution.
        """
        if row['state'] != 'pending' and not (row['adapter'] == 'agent_inbox' and row['state'] == 'queued'):
            return False
        changed = self.db.execute("UPDATE commands SET state='cancelled',payload=NULL,detail_code=?,updated_at=? WHERE id=? AND (state='pending' OR (adapter='agent_inbox' AND state='queued'))",
                                 (reason, timestamp(), row['id'])).rowcount
        self.db.commit()
        if changed != 1:
            return False
        agent = next((a for a in self.config.get('agents', []) if a['id'] == row['agent_id']), None)
        if agent and row['adapter'] == 'agent_inbox':
            self.history.record(agent['id'], agent['provider'], agent['name'], row['id'], 'cancelled',
                prompt=row['preview'], delivery='手機授權已失效，指示未獲准開始執行。', source_kind='agent_inbox')
        return True

    def validate_mobile_delivery(self, row):
        """Recheck the originating device immediately before dispatch/claim.

        Lock order is commands.lock then the mobile authorization store lock.
        Revocation must use that same order. Never call a mutating gateway here.
        """
        with self.lock:
            try:
                origin = self._origin(self._row_origin(row), row['agent_id'])
                allowed = self._origin_allowed(origin, row['agent_id'])
            except (CommandError, KeyError, TypeError, ValueError):
                origin, allowed = {}, False
            if not allowed:
                reason = ('mobile_command_expired' if isinstance(origin.get('expires_at'), (int, float))
                          and origin['expires_at'] <= time.time() else 'mobile_authorization_revoked')
                self._cancel_mobile_row(row, reason)
            return allowed

    def revoke_mobile_pending(self, device_id):
        """Cancel unstarted device work; report native in-flight work honestly."""
        if not isinstance(device_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', device_id):
            raise CommandError(400, 'invalid_mobile_device')
        counts = {'cancelled': 0, 'blocked_before_start': 0, 'already_received': 0}
        with self.lock:
            for row in self.db.execute("SELECT * FROM commands WHERE source_device=? AND state NOT IN ('completed','failed','cancelled')",
                                       (device_id,)).fetchall():
                if row['state'] == 'pending':
                    counts['cancelled'] += int(self._cancel_mobile_row(row, 'mobile_authorization_revoked'))
                elif row['adapter'] == 'agent_inbox' and row['state'] == 'queued':
                    counts['blocked_before_start'] += int(self._cancel_mobile_row(row, 'mobile_authorization_revoked'))
                else:
                    counts['already_received'] += 1
        return counts

    def detached(self, command_id):
        """Caller holds the command lock, including native event projection."""
        return self.db.execute('SELECT 1 FROM command_detachments WHERE command_id=?', (command_id,)).fetchone() is not None

    def detach_unconfirmed(self, agent, *, commit=True):
        """Audit an owner's membership retirement; never rewrite native outcome."""
        with self.lock:
            rows=self.db.execute("SELECT id,agent_id,state,native_message_id,run_id FROM commands WHERE agent_id=? AND state='delivery_unknown'", (agent,)).fetchall()
            for row in rows:
                self.db.execute('INSERT OR IGNORE INTO command_detachments VALUES (?,?,?,?,?,?)',
                    (row['id'],agent,row['state'],row['native_message_id'],row['run_id'],timestamp()))
            if commit:self.db.commit()
            return len(rows)

    def binding(self, agent):
        if 'agents' in self.config and not any(a['id']==agent for a in self.config['agents']):return None
        if agent in self.bindings:return self.bindings[agent]
        definition=next((a for a in self.config.get('agents',[]) if a['id']==agent),None)
        return {'adapter':'agent_inbox','session_id':definition['source_id']} if definition else None

    def migrate_pending_inbox(self):
        """Only never-delivered rows follow a newly saved native binding.

        Running after config load also reconciles a crash between saving the
        binding and committing this update. Request IDs and payloads stay intact.
        """
        with self.lock,self.db:
            for agent,binding in self.bindings.items():
                self.db.execute("UPDATE commands SET adapter=?,session_id=? WHERE agent_id=? AND adapter='agent_inbox' AND state='pending' AND native_message_id IS NULL AND run_id IS NULL",
                    (binding['adapter'],binding['session_id'],agent))

    def allowed(self, agent):
        selected='agents' not in self.config or any(a['id']==agent for a in self.config['agents'])
        return self.enabled and selected and agent not in self.config.get('disabled_command_agents',[])

    def capability(self, agent):
        binding = self.binding(agent)
        enabled=self.allowed(agent)
        if binding and binding['adapter']=='agent_inbox':
            listening=bool(getattr(self,'inbox',None) and self.inbox.listening(agent))
            definition=next((a for a in self.config.get('agents',[]) if a['id']==agent),{})
            local_cursor=definition.get('provider')=='cursor' and definition.get('driver')=='push'
            return self._connection_capability(agent,{'can_send':enabled,'mode':'agent_inbox','connected':True,'same_session':False,
                    'receiver_online':listening and enabled,'persistent':False,'native_ready':False,'requires_active_turn':True,
                    'reason':'dispatch_disabled' if not enabled else 'cursor_local_background_unavailable' if local_cursor else None})
        adapter = self.adapters.get(binding['adapter']) if binding else None
        ready = bool(enabled and adapter and (adapter.available(binding['session_id'])
                     if getattr(adapter, 'serial', False) else adapter.available()))
        return self._connection_capability(agent,{'can_send': bool(enabled and binding), 'native_ready':ready,
                'mode': getattr(adapter,'mode','queue'), 'connected':bool(binding),
                'same_session': bool(binding) and getattr(adapter,'same_session',True),
                'persistent':bool(binding),'requires_active_turn':False,
                'reason': None if ready else 'not_connected' if not binding else
                'dispatch_disabled' if not enabled else getattr(adapter, 'reason', 'adapter_unavailable')})

    def _connection_capability(self, agent, capability):
        from public_sync import capability as public_sync_capability
        capability['public_sync']=public_sync_capability(self.config,agent,self.binding(agent),
            getattr(self,'public_sync_observer',None))
        if self.binding(agent) and self.binding(agent)['adapter']=='cursor_local':
            binding=self.binding(agent);adapter=self.adapters.get('cursor_local')
            capability.update(conversation_scope='office_owned_session', tool_policy='chat_only',
                              tools_enabled=False, sdk_child_activity='parent_status_only',
                              native_target_verified=bool(adapter and adapter.native_verified(binding['session_id'])))
        if self.binding(agent) and self.binding(agent)['adapter']=='codex_background':
            binding=self.binding(agent);adapter=self.adapters.get('codex_background')
            capability.update(conversation_scope='office_owned_session', tool_policy='read_only',
                              tools_enabled=True, approval_policy='on-request',
                              background_child_activity='unsupported',
                              native_target_verified=bool(adapter and adapter.native_verified(binding['session_id'])))
        definition=next((a for a in self.config.get('agents',[]) if a['id']==agent),{})
        if self.binding(agent) and self.binding(agent)['adapter']=='grok_sdk':
            binding=self.binding(agent);adapter=self.adapters.get('grok_sdk')
            capability.update(conversation_scope='selected_native_session', experimental=True,
                              native_target_verified=bool(adapter and adapter.native_verified(binding['session_id'])))
            observer=getattr(self,'grok_sdk_sync',None)
            proof=observer.describe(agent) if observer else None
            capability['execution_verified']=bool(isinstance(proof,dict) and proof.get('execution_verified'))
        elif definition.get('provider')=='grok_bot':
            checks=getattr(self,'connection_checks',None)
            proof=checks.status(agent,native_ready=capability.get('native_ready')) if checks else {'state':'unconfigured' if capability['mode']=='agent_inbox' else 'ready_to_test',
                'verified':False,'received':False,'replied':False,'test_id':None,'can_test':False,'can_retry':False}
            capability.update(connection_check=proof,execution_verified=bool(proof.get('verified')))
        return capability

    @staticmethod
    def public(row):
        return {key: row[key] for key in ('id', 'agent_id', 'preview', 'state', 'run_id',
                                          'created_at', 'updated_at', 'detail_code')}

    def create(self, data, *, origin=None, commit=True):
        if not isinstance(data, dict) or set(data) != {'request_id', 'agent_id', 'text'}:
            raise CommandError(400, 'invalid_command')
        ident, agent, text = data['request_id'], data['agent_id'], data['text']
        try:
            if not isinstance(ident, str) or str(uuid.UUID(ident)) != ident:
                raise ValueError()
        except (ValueError, AttributeError):
            raise CommandError(400, 'invalid_request_id')
        if not isinstance(agent, str) or not isinstance(text, str) or not text.strip() or len(text.encode()) > 24000 or any(ord(ch) < 32 and ch not in '\n\r\t' for ch in text):
            raise CommandError(400, 'invalid_command')
        text = text.strip()
        fingerprint = self.fingerprint(agent, text)
        with self.lock:
            origin = self._origin(origin, agent, creating=True)
            if not self._origin_allowed(origin, agent):
                raise CommandError(403, 'mobile_authorization_revoked')
            binding=self.binding(agent)
            if not binding:raise CommandError(409,'not_connected')
            old = self.db.execute('SELECT * FROM commands WHERE id=?', (ident,)).fetchone()
            if old:
                if (old['agent_id'] != agent or not self._same_origin(old, origin) or
                        not self.matches_fingerprint(agent, text, old['fingerprint'])):
                    raise CommandError(409, 'request_id_conflict')
                if origin is not None and old['state'] == 'pending' and not self.validate_mobile_delivery(old):
                    old = self.db.execute('SELECT * FROM commands WHERE id=?', (ident,)).fetchone()
                return {'command': self.public(old), 'duplicate': True}
            if not self.capability(agent)['can_send']:
                raise CommandError(503, 'adapter_unavailable')
            count = self.db.execute("SELECT COUNT(*) FROM commands WHERE agent_id=? AND state IN ('pending','sending','queued','delivery_unknown','running','waiting_user') AND id NOT IN (SELECT command_id FROM command_detachments)", (agent,)).fetchone()[0]
            if count >= 20:
                raise CommandError(429, 'queue_full')
            at = timestamp()
            self.db.execute('''INSERT INTO commands
                (id,agent_id,session_id,adapter,fingerprint,preview,payload,state,native_message_id,run_id,
                 created_at,updated_at,detail_code,source_device,source_revision,source_expires_at,source_membership)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (ident, agent, binding['session_id'], binding['adapter'], fingerprint,
                 redact(text, self.history.secrets, 6000), text, 'pending', None, None, at, at, None,
                 origin['device_id'] if origin else None, origin['revision'] if origin else None,
                 origin['expires_at'] if origin else None, origin.get('membership') if origin else None))
            if commit:self.db.commit()
            row = self.db.execute('SELECT * FROM commands WHERE id=?', (ident,)).fetchone()
            if binding['adapter']=='agent_inbox':
                self.history.message(agent,ident,'user','prompt',row['preview'],event_id='inbox:'+ident+':prompt')
            return {'command': self.public(row), 'duplicate': False}

    def list(self, agent):
        with self.lock:
            rows = self.db.execute('SELECT * FROM commands WHERE agent_id=? ORDER BY created_at DESC,id DESC LIMIT 20', (agent,)).fetchall()
        binding = self.binding(agent) or {}
        adapter = self.adapters.get(binding.get('adapter'))
        approvals = adapter.approvals(binding['session_id']) if hasattr(adapter, 'approvals') else []
        return {'capability': self.capability(agent), 'commands': [self.public(row) for row in rows], 'approvals': approvals}

    def list_mobile(self, agent, origin):
        """Selected-agent public command history without permission decisions."""
        with self.lock:
            origin = self._origin(origin, agent)
            if origin is None or not self._origin_allowed(origin, agent):
                raise CommandError(403, 'mobile_authorization_revoked')
            return self.list_mobile_view(agent, origin['device_id'], revision=origin['revision'])

    def list_mobile_view(self, agent, device_id=None, *, revision=None):
        """Trusted projection helper; caller must enforce mobile view scope.

        This never reads native approval requests. A view-only caller must set
        can_cancel=False regardless of ownership; cancel_pending enforces send.
        """
        with self.lock:
            rows = self.db.execute('SELECT * FROM commands WHERE agent_id=? ORDER BY created_at DESC,id DESC LIMIT 20',
                                   (agent,)).fetchall()
            commands = [{**self.public(row), 'can_cancel': bool(device_id is not None and
                         row['source_device'] == device_id and row['state'] == 'pending' and
                         (revision is None or row['source_revision'] == revision))} for row in rows]
            return {'capability': self.capability(agent), 'commands': commands, 'approvals': []}

    def cancel_pending(self, data, *, origin=None):
        if not isinstance(data,dict) or set(data)!={'agent_id','command_id'}:raise CommandError(400,'invalid_command')
        with self.lock:
            origin = self._origin(origin, data['agent_id'])
            if not self._origin_allowed(origin, data['agent_id']):
                raise CommandError(403, 'mobile_authorization_revoked')
            row=self.db.execute('SELECT * FROM commands WHERE agent_id=? AND id=?',(data['agent_id'],data['command_id'])).fetchone()
            if not row:raise CommandError(404,'command_not_found')
            if origin is not None and not self._same_origin(row, origin):
                raise CommandError(404, 'command_not_found')
            if row['state']=='cancelled':return {'command':self.public(row),'duplicate':True}
            if row['state']!='pending':raise CommandError(409,'command_already_received')
            self.db.execute("UPDATE commands SET state='cancelled',payload=NULL,updated_at=? WHERE id=?",(timestamp(),row['id']));self.db.commit()
            agent=next((a for a in self.config.get('agents',[]) if a['id']==data['agent_id']),None)
            if agent and row['adapter']=='agent_inbox':self.history.record(agent['id'],agent['provider'],agent['name'],row['id'],'cancelled',prompt=row['preview'],delivery='用戶在 Agent 收取前取消。',source_kind='agent_inbox')
            return {'command':self.public(self.db.execute('SELECT * FROM commands WHERE id=?',(row['id'],)).fetchone()),'duplicate':False}

    def decide(self, data):
        if not isinstance(data, dict) or set(data) != {'agent_id','approval_id','decision'} or data['decision'] not in {'allow_once','reject_once'}:
            raise CommandError(400, 'invalid_approval')
        binding = self.bindings.get(data['agent_id'], {})
        adapter = self.adapters.get(binding.get('adapter'))
        if not self.allowed(data['agent_id']) or not hasattr(adapter, 'decide') or not adapter.decide(binding['session_id'], data['approval_id'], data['decision']):
            raise CommandError(409, 'approval_expired')
        return {'accepted': True}

    def tick(self):
        if not self.enabled:
            return
        checks=getattr(self,'connection_checks',None)
        if checks:checks.expire_pending()
        if self.recovery_pending:
            self.recovery_pending=False
            self.restore_observations()
        with self.lock:
            for mobile in self.db.execute("SELECT * FROM commands WHERE source_device IS NOT NULL AND (state='pending' OR (adapter='agent_inbox' AND state='queued'))").fetchall():
                self.validate_mobile_delivery(mobile)
            row = None
            for candidate in self.db.execute("SELECT * FROM commands WHERE state='pending' AND adapter!='agent_inbox' AND id NOT IN (SELECT command_id FROM command_detachments) ORDER BY created_at,id").fetchall():
                if not self.allowed(candidate['agent_id']):continue
                if candidate['adapter']=='cursor_cloud' and getattr(self,'cloud_sync',None) and self.cloud_sync.busy(candidate['agent_id']):
                    continue
                if candidate['adapter']=='grok_sdk' and getattr(self,'grok_sdk_sync',None) and self.grok_sdk_sync.busy(candidate['agent_id']):
                    continue
                if candidate['adapter']=='grok_webhook' and self.reported_work_in_progress(candidate['agent_id']):
                    # The App may already be doing a separately reported job.
                    # Our queue serializes Office requests only; a new routine
                    # must not supersede that authenticated manual run.
                    if candidate['detail_code']!='waiting_for_reported_work':
                        self.db.execute("UPDATE commands SET detail_code='waiting_for_reported_work',updated_at=? WHERE id=? AND state='pending'",
                            (timestamp(),candidate['id']))
                        self.db.commit()
                    continue
                if candidate['adapter']=='grok_webhook' and candidate['detail_code']=='waiting_for_reported_work':
                    self.db.execute("UPDATE commands SET detail_code=NULL,updated_at=? WHERE id=? AND state='pending'",
                        (timestamp(),candidate['id']))
                    self.db.commit()
                adapter = self.adapters.get(candidate['adapter'])
                if adapter and not (adapter.available(candidate['session_id']) if getattr(adapter,'serial',False) else adapter.available()):
                    continue  # Keep the durable pending row until the native entry returns.
                if hasattr(adapter, 'can_dispatch') and not adapter.can_dispatch(candidate['session_id']):
                    continue
                if getattr(adapter, 'serial', False):
                    prior = self.db.execute("SELECT 1 FROM commands WHERE agent_id=? AND state IN ('sending','queued','running','waiting_user','delivery_unknown') AND id NOT IN (SELECT command_id FROM command_detachments) LIMIT 1", (candidate['agent_id'],)).fetchone()
                    if prior:
                        continue
                row = candidate
                break
            if not row:
                return
            self.db.execute("UPDATE commands SET state='sending',updated_at=? WHERE id=?", (timestamp(), row['id']))
            self.db.commit()

        receipt, issue, state = {}, None, 'queued'
        try:
            binding = self.bindings.get(row['agent_id'])
            if not binding or binding != {'adapter': row['adapter'], 'session_id': row['session_id']}:
                raise DeliveryError('binding_changed')
            adapter = self.adapters.get(row['adapter'])
            if adapter is None:
                raise DeliveryError('adapter_unavailable')
            # A phone revocation and the start of native delivery are serialized.
            # Once submit has started it may already have an external effect;
            # revocation cannot honestly promise to undo that effect.
            with self.lock if row['source_device'] is not None else nullcontext():
                if not self.validate_mobile_delivery(row):
                    with self.lock:
                        self.db.execute("UPDATE commands SET state='cancelled',payload=NULL,detail_code='mobile_authorization_revoked',updated_at=? WHERE id=? AND state='sending'",
                                        (timestamp(), row['id']))
                        self.db.commit()
                    return
                if not self.allowed(row['agent_id']):
                    raise DeliveryError('dispatch_disabled')
                receipt = adapter.submit(row['session_id'], '[OFFICE:' + row['id'] + ']\n' + row['payload'])
            issue = receipt.get('attention_code')
        except DeliveryError as error:
            state, issue = ('delivery_unknown' if error.uncertain else 'failed'), error.code
        except Exception:
            state, issue = 'delivery_unknown', 'delivery_unknown'
        with self.lock:
            # Observation can beat the CLI's return. Never demote running/done.
            self.db.execute('''UPDATE commands SET payload=NULL,native_message_id=COALESCE(native_message_id,?),
                state=CASE WHEN state='sending' THEN ? ELSE state END,
                detail_code=CASE WHEN state='sending' THEN ? ELSE detail_code END,
                updated_at=? WHERE id=?''',
                (receipt.get('native_message_id'), state, issue, timestamp(), row['id']))
            self.db.commit()

            final=self.db.execute('SELECT * FROM commands WHERE id=?',(row['id'],)).fetchone()
        if (row['adapter'] in {'cursor_local','codex_background','grok_sdk'} and final['state'] in {'failed','delivery_unknown'}
                and not final['native_message_id']):
            # A local dispatcher failure is not a native start event. Keep the
            # owner's prompt and uncertainty visible even before a run receipt.
            definition=next((agent for agent in self.config.get('agents',[]) if agent['id']==row['agent_id']),None)
            if definition:
                unknown=final['state']=='delivery_unknown'
                self.history.record(row['agent_id'],definition['provider'],definition['name'],row['id'],
                    'unknown' if unknown else 'error',prompt=row['preview'],
                    delivery='執行結果未確認；不會自動重送。' if unknown else '指示未能送出；請查看接駁狀態。',
                    source_kind=row['adapter']+'_dispatch')
                if self.on_status:
                    self.on_status(row['agent_id'],{'type':'interrupted' if unknown else 'finished',
                        'state':final['state'],'run_id':row['id'],'transport_only':True})

    def reported_work_in_progress(self, agent):
        """Called under the command lock; no timeout fabricates completion."""
        with self.history.lock:
            live=self.history.db.execute("SELECT run_id FROM live_runs WHERE agent_id=? AND status='working'",(agent,)).fetchone()
        if not live:
            return False
        known=self.db.execute('SELECT 1 FROM commands WHERE agent_id=? AND (id=? OR run_id=?) LIMIT 1',
            (agent,live['run_id'],live['run_id'])).fetchone()
        return known is None

    def observe_prompt(self, agent, run_id, text):
        match = MARKER.match(text) if isinstance(text, str) else None
        if not match:
            return text
        # Submission canonicalizes outer whitespace. Local reporter tools may
        # append a newline; preserve all whitespace inside the command body.
        body = text[match.end():].strip()
        with self.lock:
            row = self.db.execute('SELECT * FROM commands WHERE id=? AND agent_id=?', (match[1], agent)).fetchone()
            if not row or self.detached(row['id']) or row['state'] in {'pending','cancelled'} or not self.matches_fingerprint(agent, body, row['fingerprint']) or (row['run_id'] and row['run_id'] != run_id):
                return text
            next_state = row['state'] if row['state'] in TERMINAL else 'running'
            self.db.execute('UPDATE commands SET state=?,run_id=?,payload=NULL,detail_code=NULL,updated_at=? WHERE id=?',
                (next_state, run_id, timestamp(), row['id']))
            self.db.commit()
            with self.history.lock,self.history.db:
                self.history.db.execute('UPDATE messages SET run_id=? WHERE agent_id=? AND event_id=?',
                    (run_id,agent,'inbox:'+row['id']+':prompt'))
        return body

    def grok_start_body(self, agent, event):
        """Validate a Webhook command before event ingestion can create a run.

        Manual/onboarding Grok turns remain valid. A turn referring to an Office
        command, even through its run ID without a marker, must match exactly.
        This method does not mutate either command or history state.
        """
        if agent.get('provider') != 'grok_bot' or event.get('type') != 'turn_started':
            return None
        from event_ingest import EventConflict
        text = event.get('text')
        match = MARKER.match(text) if isinstance(text, str) else None
        with self.lock:
            run_row = self.db.execute('SELECT * FROM commands WHERE id=?', (event.get('run_id'),)).fetchone()
            has_marker = isinstance(text, str) and text.lstrip().startswith('[OFFICE')
            if not has_marker and run_row is None:
                return None
            if not match:
                raise EventConflict('grok_command_start_mismatch')
            ident = match[1]
            try:
                canonical = str(uuid.UUID(ident)) == ident
            except ValueError:
                canonical = False
            row = self.db.execute('SELECT * FROM commands WHERE id=?', (ident,)).fetchone()
            binding = self.binding(agent['id'])
            body = text[match.end():].strip()
            if (not canonical or row is None or self.detached(ident) or event.get('run_id') != ident or
                    row['agent_id'] != agent['id'] or row['adapter'] != 'grok_webhook' or
                    row['state'] in {'pending','cancelled'} or row['run_id'] not in (None, ident) or
                    binding != {'adapter': 'grok_webhook', 'session_id': row['session_id']} or
                    event.get('source_id') != agent['source_id'] or
                    not self.matches_fingerprint(agent['id'], body, row['fingerprint'])):
                raise EventConflict('grok_command_start_mismatch')
            return body

    def reconcile_grok_terminal(self, agent, data):
        """Explicit owner review of an existing authenticated terminal receipt.

        This never emits a callback or edits the original event/history. Exact
        original-task and terminal-event fingerprints are both mandatory. The
        owner endpoint is loopback/authenticated; Bots have no access to it.
        """
        from event_ingest import validate_event
        fields = {'agent_id', 'command_id', 'original_text', 'terminal_event', 'review_note', 'validate_only'}
        if not isinstance(data, dict) or set(data) != fields or type(data.get('validate_only')) is not bool:
            raise CommandError(400, 'invalid_reconciliation')
        ident, text, event, note = (data.get(k) for k in ('command_id', 'original_text', 'terminal_event', 'review_note'))
        try:
            if not isinstance(ident, str) or str(uuid.UUID(ident)) != ident:
                raise ValueError()
            if not isinstance(text, str) or not 0 < len(text.strip()) <= 24000:
                raise ValueError()
            if not isinstance(note, str) or not 20 <= len(note.strip()) <= 1000:
                raise ValueError()
            if agent.get('provider') != 'grok_bot' or data['agent_id'] != agent['id']:
                raise ValueError()
            validate_event(event, agent['source_id'])
            if (set(event) - {'source_id', 'run_id', 'event_id', 'type', 'text', 'at'} or
                    event['type'] != 'turn_completed' or event['run_id'] != ident or
                    not isinstance(event.get('text'), str) or not event['text'].strip()):
                raise ValueError()
        except (ValueError, KeyError, TypeError, AttributeError):
            raise CommandError(400, 'invalid_reconciliation') from None
        event_fingerprint = hashlib.sha256(json.dumps(event, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        note_sha256 = hashlib.sha256(note.strip().encode()).hexdigest()
        provenance = 'owner_reviewed_authenticated_terminal'
        with self.lock, self.history.lock:
            row = self.db.execute('SELECT * FROM commands WHERE id=?', (ident,)).fetchone()
            if row is None or row['agent_id'] != agent['id']:
                raise CommandError(404, 'reconciliation_command_not_found')
            if (row['adapter'] != 'grok_webhook' or
                    self.binding(agent['id']) != {'adapter': 'grok_webhook', 'session_id': row['session_id']} or
                    not self.matches_fingerprint(agent['id'], text.strip(), row['fingerprint'])):
                raise CommandError(409, 'reconciliation_command_mismatch')
            receipt = self.history.db.execute('SELECT * FROM event_receipts WHERE agent_id=? AND event_id=?',
                (agent['id'], event['event_id'])).fetchone()
            run = self.history.db.execute('SELECT * FROM runs WHERE agent_id=? AND run_id=?', (agent['id'], ident)).fetchone()
            message = self.history.db.execute('SELECT * FROM messages WHERE agent_id=? AND event_id=?',
                (agent['id'], event['event_id'])).fetchone()
            terminal_text = redact(event['text'], self.history.secrets)
            if (receipt is None or receipt['run_id'] != ident or
                    not hmac.compare_digest(receipt['fingerprint'], event_fingerprint) or
                    run is None or run['provider'] != 'grok_bot' or run['status'] != 'done' or
                    message is None or message['run_id'] != ident or message['role'] != 'assistant' or
                    message['phase'] != 'final' or message['text'] != terminal_text or run['delivery'] != terminal_text):
                raise CommandError(409, 'reconciliation_terminal_proof_mismatch')
            proof = {'command_fingerprint': row['fingerprint'], 'terminal_event_id': event['event_id'],
                     'terminal_event_fingerprint': event_fingerprint, 'review_note_sha256': note_sha256,
                     'provenance': provenance}
            prior = self.db.execute('SELECT * FROM command_reconciliations WHERE command_id=?', (ident,)).fetchone()
            if prior is not None:
                if (any(prior[k] != value for k, value in proof.items()) or prior['agent_id'] != agent['id'] or
                        row['state'] != 'completed' or row['run_id'] != ident):
                    raise CommandError(409, 'reconciliation_proof_conflict')
                return {'validated': True, 'applied': False, 'duplicate': True,
                        'command': self.public(row), 'reconciliation': dict(prior)}
            live = self.history.db.execute('SELECT * FROM live_runs WHERE agent_id=?', (agent['id'],)).fetchone()
            if (row['state'] not in {'queued', 'delivery_unknown'} or row['run_id'] is not None or
                    live is None or live['run_id'] != ident or live['status'] != 'done'):
                raise CommandError(409, 'reconciliation_not_terminal_unbound')
            audit = {'id': str(uuid.uuid4()), 'command_id': ident, 'agent_id': agent['id'],
                     'prior_state': row['state'], 'prior_run_id': row['run_id'], **proof, 'created_at': timestamp()}
            if data['validate_only']:
                return {'validated': True, 'applied': False, 'duplicate': False,
                        'command': self.public(row), 'reconciliation': {k: v for k, v in audit.items() if k not in {'id', 'created_at'}}}
            with self.db:
                changed = self.db.execute("UPDATE commands SET state='completed',run_id=?,payload=NULL,detail_code='owner_reconciled_terminal',updated_at=? WHERE id=? AND agent_id=? AND state=? AND run_id IS NULL",
                    (ident, audit['created_at'], ident, agent['id'], row['state'])).rowcount
                if changed != 1:
                    raise CommandError(409, 'reconciliation_state_changed')
                self.db.execute('INSERT INTO command_reconciliations VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    tuple(audit[k] for k in ('id', 'command_id', 'agent_id', 'prior_state', 'prior_run_id',
                        'command_fingerprint', 'terminal_event_id', 'terminal_event_fingerprint',
                        'review_note_sha256', 'provenance', 'created_at')))
            current = self.db.execute('SELECT * FROM commands WHERE id=?', (ident,)).fetchone()
            return {'validated': True, 'applied': True, 'duplicate': False,
                    'command': self.public(current), 'reconciliation': audit}

    def observe_run(self, agent, run_id, status):
        terminal = {'done': 'completed', 'error': 'failed', 'cancelled': 'cancelled'}.get(status)
        if not terminal:
            return
        with self.lock:
            self.db.execute("UPDATE commands SET state=?,payload=NULL,detail_code=NULL,updated_at=? WHERE agent_id=? AND run_id=? AND state NOT IN ('completed','failed','cancelled') AND id NOT IN (SELECT command_id FROM command_detachments)",
                (terminal, timestamp(), agent, run_id))
            self.db.commit()

    def observe_native(self, agent, event):
        run, kind = event['run_id'], event['type']
        with self.lock:
            # Membership may change while a native callback waits for this
            # lock. Validate it here, never from a pre-lock snapshot.
            binding = self.bindings.get(agent)
            if not binding or binding['adapter'] != event.get('adapter','hermes_acp') or binding['adapter'] not in {'hermes_acp','cursor_cloud','cursor_local','codex_background','grok_sdk'} or binding['session_id'] != event.get('session_id'):
                return
            definition = next((a for a in self.config['agents'] if a['id'] == agent), None)
            if not definition:
                return
            row = self.db.execute('SELECT * FROM commands WHERE id=? AND agent_id=?', (run, agent)).fetchone()
            if not row or self.detached(run) or row['state'] in TERMINAL:
                return
            native=event.get('native_run_id')
            if binding['adapter'] in {'cursor_cloud','cursor_local','codex_background','grok_sdk'}:
                if not native or (row['native_message_id'] and native!=row['native_message_id']):return
                if kind=='accepted':
                    self.db.execute("UPDATE commands SET native_message_id=?,state='queued',payload=NULL,updated_at=? WHERE id=? AND state='sending'",(native,timestamp(),run))
                    self.db.commit()
                    return
                if row['native_message_id']!=native:return
            if kind == 'started':
                if event.get('recovery'):
                    prompt=row['preview']
                    self.db.execute("UPDATE commands SET state='running',run_id=?,updated_at=? WHERE id=?",(run,timestamp(),run));self.db.commit()
                else:prompt = self.observe_prompt(agent, run, event['text'])
                self.history.record(agent, definition['provider'], definition['name'], run, 'working', prompt=prompt,
                                    source_kind=binding['adapter'], source_ref=native or event['session_id'])
                self.history.message(agent, run, 'user', 'prompt', prompt, event_id=binding['adapter']+':'+run+':prompt')
            elif kind == 'text':
                self.history.stream_message(agent, run, event['segment'], event['text'], event.get('final', False))
            elif kind == 'status':
                state = 'waiting_user' if event.get('waiting') else 'running'
                self.db.execute("UPDATE commands SET state=?,updated_at=? WHERE id=? AND state IN ('running','waiting_user')", (state, timestamp(), run))
                self.db.commit()
            elif kind in {'finished', 'interrupted'}:
                state = event['state']
                history_state = {'completed':'done','failed':'error','cancelled':'cancelled','delivery_unknown':'unknown'}[state]
                self.history.record(agent, definition['provider'], definition['name'], run, history_state,
                    delivery=event.get('text'), source_kind=binding['adapter'], source_ref=native or event['session_id'])
                self.db.execute('UPDATE commands SET state=?,payload=NULL,run_id=COALESCE(run_id,?),detail_code=?,updated_at=? WHERE id=?',
                    (state, run, event.get('detail_code'), timestamp(), run))
                self.db.commit()
            if self.on_status:
                self.on_status(agent, event)

    def run(self, stop):
        while not stop.is_set():
            try:
                self.tick()
            except (OSError, sqlite3.Error):
                pass  # Keep telemetry independent; do not re-dispatch sending rows.
            stop.wait(0.5)

    def close(self):
        if getattr(self,'grok_sdk_sync',None):
            self.grok_sdk_sync.close()
        if getattr(self,'cloud_sync',None):
            self.cloud_sync.close()
        for adapter in self.adapters.values():
            if hasattr(adapter, 'close'):
                adapter.close()
        with self.lock:
            self.db.close()
