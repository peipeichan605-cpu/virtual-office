"""Durable, owner-started round trips. Configuration alone is never verification.

The dispatcher and authenticated event path remain authoritative. This module
creates one normal command only on explicit start(), never on status polling.
"""
import hashlib
import hmac
import json
import time
import uuid
from history import timestamp


class ConnectionCheckError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


class ConnectionChecks:
    SUPPORTED = frozenset({'grok_webhook'})
    ACTIVE = ('pending', 'sending', 'queued', 'running', 'waiting_user', 'delivery_unknown')
    FAILURE_CODES = frozenset({'platform_auth_required', 'platform_access_denied',
        'webhook_not_configured', 'adapter_unavailable', 'binding_changed',
        'platform_connection_failed', 'cancelled', 'connection_test_expired',
        'connection_test_route_changed'})

    def __init__(self, commands, timeout_seconds=120, clock=None):
        if not 10 <= timeout_seconds <= 600:
            raise ValueError('Invalid connection check timeout')
        self.commands, self.history = commands, commands.history
        self.timeout, self.clock = timeout_seconds, clock or time.time
        with commands.lock, commands.db:
            commands.db.execute('''CREATE TABLE IF NOT EXISTS connection_check_routes (
                agent_id TEXT PRIMARY KEY, revision TEXT NOT NULL)''')
            commands.db.execute('''CREATE TABLE IF NOT EXISTS connection_checks (
                id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, adapter TEXT NOT NULL,
                session_id TEXT NOT NULL, route_fingerprint TEXT NOT NULL,
                expected_reply TEXT NOT NULL, prompt TEXT NOT NULL,
                created REAL NOT NULL, deadline REAL NOT NULL,
                started_event TEXT, reply_event TEXT, terminal_event TEXT,
                terminal_kind TEXT, verified_at REAL)''')
            commands.db.execute('CREATE INDEX IF NOT EXISTS connection_check_agent ON connection_checks(agent_id,created)')

    def route_changed(self, agent_id):
        """Call under the lifecycle lock before changing credentials/membership.

        A crash before the actual write only invalidates prior proof. This must
        not reuse an observer generation, which can rotate independently.
        """
        with self.commands.lock, self.commands.db:
            self.commands.db.execute("UPDATE commands SET state='cancelled',payload=NULL,"
                "detail_code='connection_test_route_changed',updated_at=? WHERE agent_id=? AND state='pending' "
                'AND native_message_id IS NULL AND run_id IS NULL '
                'AND id IN (SELECT id FROM connection_checks WHERE agent_id=?)', (timestamp(), agent_id, agent_id))
            self.commands.db.execute('INSERT INTO connection_check_routes VALUES (?,?) '
                'ON CONFLICT(agent_id) DO UPDATE SET revision=excluded.revision',
                (agent_id, uuid.uuid4().hex))

    def expire_pending(self):
        """Dispatcher maintenance; never called as a side effect of GET status.

        Only known never-delivered checks may be cancelled. An external send,
        even one with an unknown outcome, is never cancelled or replayed here.
        """
        with self.commands.lock, self.commands.db:
            rows = self.commands.db.execute("SELECT checks.* FROM connection_checks AS checks "
                "JOIN commands AS c ON c.id=checks.id AND c.agent_id=checks.agent_id "
                "WHERE c.state='pending' AND c.native_message_id IS NULL AND c.run_id IS NULL").fetchall()
            for test in rows:
                definition = self._definition(test['agent_id'])
                binding = self.commands.binding(test['agent_id'])
                current = bool(definition and binding == {'adapter': test['adapter'], 'session_id': test['session_id']}
                    and hmac.compare_digest(test['route_fingerprint'], self._signature(test['agent_id'], binding, definition)))
                if current and self.clock() <= test['deadline']:
                    continue
                reason = 'connection_test_expired' if current else 'connection_test_route_changed'
                self.commands.db.execute("UPDATE commands SET state='cancelled',payload=NULL,detail_code=?,updated_at=? "
                    "WHERE id=? AND state='pending' AND native_message_id IS NULL AND run_id IS NULL", (reason, timestamp(), test['id']))

    def _definition(self, agent_id):
        return next((a for a in self.commands.config.get('agents', []) if a['id'] == agent_id), None)

    def _signature(self, agent_id, binding, definition):
        row = self.commands.db.execute('SELECT revision FROM connection_check_routes WHERE agent_id=?', (agent_id,)).fetchone()
        value = {'binding': binding, 'revision': row[0] if row else '',
                 'source_id': definition.get('source_id'), 'provider': definition.get('provider'),
                 'credential_hash': definition.get('token_sha256')}
        return hmac.new(self.commands.config['topic'].encode(),
            json.dumps(value, sort_keys=True, separators=(',', ':')).encode(), hashlib.sha256).hexdigest()

    def _busy(self, agent_id, except_id=None):
        if self.commands.reported_work_in_progress(agent_id):
            return True
        marks = ','.join('?' for _ in self.ACTIVE)
        return bool(self.commands.db.execute(
            'SELECT 1 FROM commands WHERE agent_id=? AND state IN (' + marks + ') AND id!=? LIMIT 1',
            (agent_id, *self.ACTIVE, except_id or '')).fetchone())

    def _owner_reconciled(self, command):
        # A later duplicate start can clear a command's transient detail_code;
        # the immutable reconciliation audit still distinguishes owner review.
        return command['detail_code'] == 'owner_reconciled_terminal' or bool(
            self.commands.db.execute('SELECT 1 FROM command_reconciliations WHERE command_id=? LIMIT 1',
                (command['id'],)).fetchone())

    @staticmethod
    def _public(state, *, test=None, verified=False, received=False, replied=False,
                can_test=False, can_retry=False, reason=None, message=None):
        messages = {
            'unconfigured': '已入場，仲差最後連接。',
            'unsupported': '此平台尚未提供這項連接測試。',
            'ready_to_test': '連接資料已準備好，按「測試連接」確認收發。',
            'queued': '測試訊息已排隊，等待 Agent 收到。',
            'waiting_reply': 'Agent 已收到測試訊息，等待回覆。',
            'verified': '測試訊息已收到並回覆，連接已確認。',
            'failed': '這次測試未能確認收發，請查看原因。',
            'unconfirmed': '收發結果尚未確認，請勿重複傳送測試。',
            'disabled': '此 Agent 的指令傳送已停用。',
        }
        return {'state': state, 'verified': bool(verified), 'received': bool(received),
                'replied': bool(replied), 'test_id': test['id'] if test else None,
                'can_test': bool(can_test), 'can_retry': bool(can_retry),
                'reason': reason, 'detail_code': reason, 'message': message or messages[state],
                'verified_at': test['verified_at'] if test else None}

    def status(self, agent_id, native_ready=None):
        """Read-only projection; availability may be supplied by capability()."""
        with self.commands.lock:
            return self._status(agent_id, native_ready=native_ready)

    def _status(self, agent_id, native_ready=None, test=None):
        result = self._status_for_route(agent_id, native_ready=native_ready, test=test)
        definition = self._definition(agent_id)
        if definition and definition.get('provider') == 'grok_bot':
            # Public opaque version only. Clients can retire a superseded
            # snapshot without seeing credentials or trusting response order.
            result['route_revision'] = self._signature(agent_id, self.commands.binding(agent_id), definition)
        return result

    def _status_for_route(self, agent_id, native_ready=None, test=None):
        definition = self._definition(agent_id)
        if not definition:
            return self._public('unconfigured', reason='agent_not_selected')
        if not self.commands.allowed(agent_id):
            return self._public('disabled', reason='dispatch_disabled')
        binding = self.commands.binding(agent_id)
        if not binding or binding['adapter'] == 'agent_inbox':
            return self._public('unconfigured', reason='native_setup_required')
        if binding['adapter'] not in self.SUPPORTED:
            return self._public('unsupported', reason='check_not_supported')
        signature = self._signature(agent_id, binding, definition)
        if test is None:
            test = self.commands.db.execute('SELECT * FROM connection_checks WHERE agent_id=? '
                'AND route_fingerprint=? ORDER BY created DESC,rowid DESC LIMIT 1',
                (agent_id, signature)).fetchone()
        if test is not None and not hmac.compare_digest(test['route_fingerprint'], signature):
            return self._public('unconfirmed', test=test, reason='connection_changed')
        ready = native_ready is not False and binding['adapter'] in self.commands.adapters
        free = not self._busy(agent_id, test['id'] if test else None)
        if test is None:
            return self._public('ready_to_test', can_test=ready and free,
                reason=None if ready and free else 'agent_busy' if not free else 'native_unavailable',
                message=None if ready and free else 'Agent 正在處理其他工作，完成後可按測試。' if not free else '原平台暫時無法使用，請稍後再測試。')
        row = self.commands.db.execute('SELECT * FROM commands WHERE id=? AND agent_id=?',
            (test['id'], agent_id)).fetchone()
        received, replied = bool(test['started_event']), bool(test['reply_event'])
        common = {'test': test, 'received': received, 'replied': replied}
        if row is None:
            return self._public('unconfirmed', reason='check_command_missing', **common)
        if row['adapter'] != binding['adapter'] or row['session_id'] != binding['session_id']:
            return self._public('unconfirmed', reason='connection_changed', **common)
        if self._owner_reconciled(row):
            return self._public('unconfirmed', reason='automatic_callback_proof_required', **common)
        if (row['state'] == 'completed' and row['run_id'] == test['id'] and received and replied
                and test['terminal_event'] and test['terminal_kind'] == 'turn_completed'):
            if not ready:
                return self._public('unconfirmed', reason='native_unavailable',
                    message='收發曾經驗證成功；原平台目前暫時無法使用。', **common)
            return self._public('verified', verified=True, can_test=free, can_retry=free, **common)
        if row['state'] in {'failed', 'cancelled'}:
            reason = row['detail_code'] if row['detail_code'] in self.FAILURE_CODES else 'cancelled' if row['state'] == 'cancelled' else 'test_dispatch_failed'
            return self._public('failed', reason=reason,
                can_test=ready and free, can_retry=ready and free, **common)
        if row['state'] == 'completed':
            terminal = test['terminal_event'] and test['terminal_kind'] == 'turn_completed'
            return self._public('failed' if terminal and received else 'unconfirmed',
                reason='test_reply_mismatch' if terminal and received else 'callback_proof_missing',
                can_test=bool(terminal and received and ready and free),
                can_retry=bool(terminal and received and ready and free), **common)
        if row['state'] == 'delivery_unknown' or self.clock() > test['deadline']:
            return self._public('unconfirmed', reason='delivery_unknown' if row['state'] == 'delivery_unknown' else 'test_timeout', **common)
        if received:
            return self._public('waiting_reply', **common)
        return self._public('queued', **common)

    def start(self, data):
        if not isinstance(data, dict) or set(data) != {'agent_id', 'request_id'}:
            raise ConnectionCheckError('invalid_connection_check')
        agent_id, ident = data['agent_id'], data['request_id']
        try:
            if not isinstance(agent_id, str) or not isinstance(ident, str) or str(uuid.UUID(ident)) != ident:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ConnectionCheckError('invalid_connection_check')
        with self.commands.lock:
            definition = self._definition(agent_id)
            if not definition:
                raise ConnectionCheckError('agent_not_selected', 404)
            if not self.commands.allowed(agent_id):
                raise ConnectionCheckError('dispatch_disabled', 409)
            binding = self.commands.binding(agent_id)
            if not binding or binding['adapter'] == 'agent_inbox':
                raise ConnectionCheckError('native_setup_required', 409)
            if binding['adapter'] not in self.SUPPORTED:
                raise ConnectionCheckError('check_not_supported', 409)
            signature = self._signature(agent_id, binding, definition)
            prior = self.commands.db.execute('SELECT * FROM connection_checks WHERE id=?', (ident,)).fetchone()
            if prior:
                if prior['agent_id'] != agent_id or not hmac.compare_digest(prior['route_fingerprint'], signature):
                    raise ConnectionCheckError('request_id_conflict', 409)
                ready = self.commands.capability(agent_id).get('native_ready')
                return dict(self._status(agent_id, native_ready=ready, test=prior), duplicate=True)
            # A normal user command must never be reinterpreted as a check.
            if self.commands.db.execute('SELECT 1 FROM commands WHERE id=?', (ident,)).fetchone():
                raise ConnectionCheckError('request_id_conflict', 409)
            latest = self._status(agent_id)
            if latest['state'] == 'unconfirmed' and not latest['can_retry']:
                raise ConnectionCheckError('check_result_unconfirmed', 409)
            if self._busy(agent_id):
                raise ConnectionCheckError('agent_busy', 409)
            capability = self.commands.capability(agent_id)
            if capability.get('native_ready') is not True:
                raise ConnectionCheckError('native_unavailable', 409)
            expected = 'OFFICE_CONNECTION_OK_' + uuid.uuid4().hex
            prompt = ('這是 OFFICE 連接測試。只完成這次測試，不建立子 Agent、服務或其他任務。'
                '除現有 OFFICE 上報工具外，不讀取其他檔案、不使用其他工具、不修改其他資料。'
                '請按既有上報規則回傳開始與完成，並在公開最終回覆只寫下一行原文：\n' + expected)
            now = self.clock()
            with self.commands.db:
                self.commands.db.execute('INSERT INTO connection_checks '
                    '(id,agent_id,adapter,session_id,route_fingerprint,expected_reply,prompt,created,deadline) VALUES (?,?,?,?,?,?,?,?,?)',
                    (ident, agent_id, binding['adapter'], binding['session_id'], signature, expected, prompt, now, now + self.timeout))
                self.commands.create({'request_id': ident, 'agent_id': agent_id, 'text': prompt}, commit=False)
            return dict(self._status(agent_id), duplicate=False)

    def observe(self, agent, event, receipt):
        """Called only after the normal authenticated ingestion has accepted it."""
        if not isinstance(receipt, dict) or receipt.get('accepted') is not True:
            return
        ident, agent_id = event.get('run_id'), agent.get('id')
        with self.commands.lock, self.history.lock:
            test = self.commands.db.execute('SELECT * FROM connection_checks WHERE id=? AND agent_id=?', (ident, agent_id)).fetchone()
            if test is None:
                return
            definition = self._definition(agent_id)
            binding = self.commands.binding(agent_id)
            if (not definition or not self.commands.allowed(agent_id) or
                    binding != {'adapter': test['adapter'], 'session_id': test['session_id']} or
                    event.get('source_id') != definition.get('source_id') or
                    not hmac.compare_digest(test['route_fingerprint'], self._signature(agent_id, binding, definition))):
                return
            proof = self.history.db.execute('SELECT fingerprint,run_id FROM event_receipts WHERE agent_id=? AND event_id=?',
                (agent_id, event.get('event_id'))).fetchone()
            fingerprint = hashlib.sha256(json.dumps(event, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            if not proof or proof['run_id'] != ident or not hmac.compare_digest(proof['fingerprint'], fingerprint):
                return
            command = self.commands.db.execute('SELECT * FROM commands WHERE id=? AND agent_id=?', (ident, agent_id)).fetchone()
            if (not command or command['run_id'] != ident or command['adapter'] != test['adapter']
                    or command['session_id'] != test['session_id'] or self._owner_reconciled(command)):
                return
            kind, text = event.get('type'), event.get('text', '')
            updates = {}
            if kind == 'turn_started':
                expected_text = '[OFFICE:' + ident + ']\n' + test['prompt']
                if not isinstance(text, str) or not hmac.compare_digest(text.strip().encode(), expected_text.encode()):
                    return
                updates['started_event'] = event['event_id']
            final = kind == 'turn_completed' or kind == 'message' and event.get('role') == 'assistant' and event.get('phase') == 'final'
            if final and isinstance(text, str) and hmac.compare_digest(text.strip().encode(), test['expected_reply'].encode()):
                updates['reply_event'] = event['event_id']
            if kind in {'turn_completed', 'turn_failed', 'turn_cancelled'}:
                updates.update(terminal_event=event['event_id'], terminal_kind=kind)
            if not updates:
                return
            with self.commands.db:
                self.commands.db.execute('UPDATE connection_checks SET ' + ','.join(k + '=?' for k in updates) + ' WHERE id=?', (*updates.values(), ident))
                current = self.commands.db.execute('SELECT * FROM connection_checks WHERE id=?', (ident,)).fetchone()
                if (current['started_event'] and current['reply_event'] and current['terminal_kind'] == 'turn_completed'
                        and command['state'] == 'completed' and current['verified_at'] is None):
                    self.commands.db.execute('UPDATE connection_checks SET verified_at=? WHERE id=?', (self.clock(), ident))
