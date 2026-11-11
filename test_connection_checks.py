"""Connection proof contracts. Fixtures never call a vendor or use real Bots."""
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from command_adapters import DeliveryError
from commands import Commands, CommandError
from connection_checks import ConnectionChecks, ConnectionCheckError
from event_ingest import EventConflict, ingest
from history import HistoryStore


class FixtureAdapter:
    mode, serial, same_session = 'grok_webhook', True, False
    reason = None
    def __init__(self):
        self.calls, self.effect, self.ready = [], None, True
    def available(self, session):
        return self.ready
    def submit(self, session, text):
        self.calls.append((session, text))
        if self.effect:
            return self.effect(session, text)
        return {'native_message_id': 'fixture-receipt'}


class ConnectionCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.now = 1000.0
        source = 'fixture-' + uuid.uuid4().hex
        self.agent = {'id': 'grok_bot:' + source, 'source_id': source, 'provider': 'grok_bot',
            'driver': 'push', 'name': 'Selected fixture', 'token_sha256': 'fixture-enrollment-epoch'}
        self.config = {'data_dir': str(self.folder), 'topic': 'fixture-private-office-key',
            'agents': [self.agent], 'command_bindings': {self.agent['id']: {'adapter': 'grok_webhook', 'session_id': source}}}
        self.history = HistoryStore(self.folder / 'history.sqlite3')
        self.adapter = FixtureAdapter()
        self.commands = Commands(self.config, self.history, adapters={'grok_webhook': self.adapter})
        self.checks = ConnectionChecks(self.commands, clock=lambda: self.now)
        self.commands.connection_checks = self.checks

    def tearDown(self):
        self.commands.close()
        self.history.close()
        self.tmp.cleanup()

    def start(self, ident=None):
        return self.checks.start({'agent_id': self.agent['id'], 'request_id': ident or str(uuid.uuid4())})

    def event(self, ident, kind, text='', **changes):
        return {'source_id': self.agent['source_id'], 'event_id': str(uuid.uuid4()),
            'run_id': ident, 'type': kind, 'text': text, **changes}

    def accept(self, event, observe=True):
        # This is the same authenticated ingestion ordering used by Bridge.
        clean = self.commands.grok_start_body(self.agent, event)
        receipt = ingest(self.history, self.agent, event, display_text=clean)
        if clean is not None:
            self.commands.observe_prompt(self.agent['id'], event['run_id'], event['text'])
        terminal = {'turn_completed': 'done', 'turn_failed': 'error', 'turn_cancelled': 'cancelled'}.get(event['type'])
        if terminal:
            self.commands.observe_run(self.agent['id'], event['run_id'], terminal)
        if observe:
            self.checks.observe(self.agent, event, receipt)
        return receipt

    def dispatched(self):
        result = self.start()
        self.commands.tick()
        self.assertEqual(len(self.adapter.calls), 1)
        body = self.adapter.calls[0][1]
        nonce = body.splitlines()[-1]
        self.assertTrue(nonce.startswith('OFFICE_CONNECTION_OK_'))
        return result['test_id'], body, nonce

    def finish(self):
        ident, body, nonce = self.dispatched()
        self.accept(self.event(ident, 'turn_started', body))
        self.accept(self.event(ident, 'turn_completed', nonce))
        return ident, body, nonce

    def test_start_is_explicit_idempotent_and_http_receipt_is_not_verification(self):
        self.assertEqual(self.checks.status(self.agent['id'])['state'], 'ready_to_test')
        for _ in range(3):
            self.checks.status(self.agent['id'])
        self.assertEqual(self.adapter.calls, [])
        request = str(uuid.uuid4())
        first = self.start(request)
        again = self.start(request)
        self.assertEqual(first['test_id'], request)
        self.assertTrue(again['duplicate'])
        self.commands.tick(); self.commands.tick()
        status = self.checks.status(self.agent['id'])
        self.assertEqual(status['state'], 'queued')
        self.assertFalse(status['verified']); self.assertFalse(status['received']); self.assertFalse(status['replied'])
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.commands.db.execute('SELECT COUNT(*) FROM commands').fetchone()[0], 1)
        self.assertNotIn('expected_reply', status)

    def test_reported_app_work_prevents_new_connection_probe_until_its_real_completion(self):
        self.accept(self.event('manual-app-task', 'turn_started', 'Human task from the App'))
        self.assertFalse(self.checks.status(self.agent['id'])['can_test'])
        with self.assertRaises(ConnectionCheckError) as denied:
            self.start()
        self.assertEqual(denied.exception.code, 'agent_busy')
        self.assertEqual(self.commands.db.execute('SELECT count(*) FROM commands').fetchone()[0], 0)
        self.assertEqual(self.adapter.calls, [])
        self.accept(self.event('manual-app-task', 'turn_completed', 'Public delivery'))
        self.assertTrue(self.checks.status(self.agent['id'])['can_test'])
        self.start()
        self.commands.tick()
        self.assertEqual(len(self.adapter.calls), 1)

    def test_opaque_route_revision_is_consistent_for_status_start_and_same_route(self):
        token = self.checks.status(self.agent['id'])['route_revision']
        self.assertRegex(token, r'^[0-9a-f]{64}$')
        started = self.start()
        self.assertEqual(started['route_revision'], token)
        self.assertEqual(self.start(started['test_id'])['route_revision'], token)
        self.assertEqual(self.checks.status(self.agent['id'])['route_revision'], token)
        public = json.dumps(started)
        for private in (self.config['topic'], self.agent['token_sha256'], self.agent['source_id']):
            self.assertNotIn(private, public)

    def test_mailbox_disabled_replaced_and_new_membership_have_distinct_route_versions(self):
        native = self.checks.status(self.agent['id'])['route_revision']
        binding = self.commands.bindings.pop(self.agent['id'])
        mailbox = self.checks.status(self.agent['id'])
        self.assertEqual(mailbox['state'], 'unconfigured')
        self.assertNotEqual(mailbox['route_revision'], native)
        self.commands.bindings[self.agent['id']] = binding
        self.assertEqual(self.checks.status(self.agent['id'])['route_revision'], native)
        self.config.setdefault('disabled_command_agents', []).append(self.agent['id'])
        disabled = self.checks.status(self.agent['id'])
        self.assertEqual(disabled['state'], 'disabled')
        self.assertEqual(disabled['route_revision'], native)
        self.config['disabled_command_agents'].clear()
        self.checks.route_changed(self.agent['id'])
        replaced = self.checks.status(self.agent['id'])['route_revision']
        self.assertNotEqual(replaced, native)
        self.agent['token_sha256'] = 'a-new-registration'
        registration = self.checks.status(self.agent['id'])['route_revision']
        self.assertNotEqual(registration, replaced)
        self.config['agents'] = []
        self.assertNotIn('route_revision', self.checks.status(self.agent['id']))
        other = dict(self.agent, id='generic:fixture', provider='generic', source_id='fixture')
        self.config['agents'] = [other]
        self.assertNotIn('route_revision', self.checks.status(other['id']))

    def test_exact_native_start_public_reply_and_normal_completion_are_all_required(self):
        ident, body, nonce = self.dispatched()
        self.accept(self.event(ident, 'turn_started', body))
        status = self.checks.status(self.agent['id'])
        self.assertTrue(status['received']); self.assertFalse(status['verified'])
        self.accept(self.event(ident, 'message', nonce, role='assistant', phase='final'))
        status = self.checks.status(self.agent['id'])
        self.assertTrue(status['replied']); self.assertFalse(status['verified'])
        self.accept(self.event(ident, 'turn_completed', ''))
        status = self.checks.status(self.agent['id'])
        self.assertEqual(status['state'], 'verified'); self.assertTrue(status['verified'])
        self.assertIsNotNone(status['verified_at'])

    def test_wrong_nonce_unicode_and_commentary_are_not_a_public_final_reply(self):
        for text, commentary in [('不相配的中文回覆', False), ('prefix ', False), ('', True)]:
            with self.subTest(text=text, commentary=commentary):
                if self.commands.db.execute('SELECT COUNT(*) FROM commands').fetchone()[0]:
                    self.commands.db.execute('DELETE FROM commands'); self.commands.db.execute('DELETE FROM connection_checks'); self.commands.db.commit()
                    self.adapter.calls.clear()
                ident, body, nonce = self.dispatched()
                self.accept(self.event(ident, 'turn_started', body))
                if commentary:
                    self.accept(self.event(ident, 'message', nonce, role='assistant', phase='commentary'))
                self.accept(self.event(ident, 'turn_completed', text + (nonce if text == 'prefix ' else '')))
                result = self.checks.status(self.agent['id'])
                self.assertFalse(result['verified']); self.assertFalse(result['replied'])
                self.assertEqual(result['state'], 'failed'); self.assertTrue(result['can_retry'])

    def test_final_without_authentic_accepted_events_cannot_verify(self):
        ident, body, nonce = self.dispatched()
        self.commands.observe_prompt(self.agent['id'], ident, body)
        self.history.record(self.agent['id'], 'grok_bot', 'Fixture', ident, 'done', prompt=body, delivery=nonce)
        self.history.message(self.agent['id'], ident, 'assistant', 'final', nonce, event_id='fabricated')
        self.commands.observe_run(self.agent['id'], ident, 'done')
        fake = self.event(ident, 'turn_completed', nonce, event_id='fabricated')
        self.checks.observe(self.agent, fake, {'accepted': True})
        result = self.checks.status(self.agent['id'])
        self.assertFalse(result['verified']); self.assertEqual(result['state'], 'unconfirmed')

    def test_altered_body_wrong_run_wrong_agent_and_out_of_order_terminal_are_rejected(self):
        ident, body, nonce = self.dispatched()
        for event in [self.event(ident, 'turn_started', body + ' altered'),
                      self.event('other-run', 'turn_started', body),
                      self.event(ident, 'turn_completed', nonce)]:
            with self.subTest(kind=event['type']):
                with self.assertRaises(EventConflict): self.accept(event)
        other = dict(self.agent, id='grok_bot:other', source_id='other')
        fake = self.event(ident, 'turn_started', body, source_id='other')
        with self.assertRaises(EventConflict): self.commands.grok_start_body(other, fake)
        self.checks.observe(other, self.event(ident, 'turn_completed', nonce, source_id='other'), {'accepted': True})
        self.assertFalse(self.checks.status(self.agent['id'])['verified'])

    def test_receipt_fingerprint_source_and_real_terminal_kind_are_required(self):
        ident, body, nonce = self.dispatched()
        start = self.event(ident, 'turn_started', body)
        self.accept(start)
        reply = self.event(ident, 'message', nonce, role='assistant', phase='final')
        self.accept(reply)
        terminal = self.event(ident, 'turn_completed', nonce)
        receipt = self.accept(terminal, observe=False)
        self.checks.observe(self.agent, {**terminal, 'type': 'message', 'role': 'assistant', 'phase': 'final'}, receipt)
        self.assertFalse(self.checks.status(self.agent['id'])['verified'])
        self.checks.observe(self.agent, terminal, receipt)
        self.assertTrue(self.checks.status(self.agent['id'])['verified'])

    def test_duplicate_callback_repairs_missing_observation_without_replaying_task(self):
        ident, body, nonce = self.dispatched()
        start = self.event(ident, 'turn_started', body)
        receipt = self.accept(start, observe=False)
        self.accept(self.event(ident, 'turn_completed', nonce))
        self.assertFalse(self.checks.status(self.agent['id'])['verified'])
        self.assertTrue(self.accept(start)['duplicate'])
        self.assertTrue(self.checks.status(self.agent['id'])['verified'])
        self.commands.tick(); self.assertEqual(len(self.adapter.calls), 1)

    def test_owner_reconciliation_is_not_fresh_automatic_verification(self):
        ident, body, nonce = self.finish()
        self.commands.db.execute("UPDATE commands SET detail_code='owner_reconciled_terminal' WHERE id=?", (ident,)); self.commands.db.commit()
        self.assertFalse(self.checks.status(self.agent['id'])['verified'])
        self.assertEqual(self.checks.status(self.agent['id'])['detail_code'], 'automatic_callback_proof_required')

    def test_reconciliation_audit_cannot_be_hidden_by_cleared_transient_detail(self):
        ident, _, _ = self.finish()
        self.commands.db.execute('INSERT INTO command_reconciliations VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (uuid.uuid4().hex, ident, self.agent['id'], 'delivery_unknown', None,
             'fixture-fingerprint', 'fixture-terminal', 'fixture-terminal-fingerprint',
             'fixture-review-hash', 'owner_review', '2026-01-01T00:00:00Z'))
        self.commands.db.execute('UPDATE commands SET detail_code=NULL WHERE id=?', (ident,))
        self.commands.db.commit()
        self.assertFalse(self.checks.status(self.agent['id'])['verified'])
        self.assertEqual(self.checks.status(self.agent['id'])['detail_code'], 'automatic_callback_proof_required')

    def test_status_and_success_survive_restart_without_new_dispatch(self):
        self.finish()
        before = self.checks.status(self.agent['id'])
        self.commands.close()
        self.commands = Commands(self.config, self.history, adapters={'grok_webhook': self.adapter})
        self.checks = ConnectionChecks(self.commands, clock=lambda: self.now)
        self.commands.connection_checks = self.checks
        self.assertEqual(self.checks.status(self.agent['id']), before)
        self.commands.tick(); self.assertEqual(len(self.adapter.calls), 1)

    def test_retry_of_old_success_does_not_show_currently_unavailable_route_as_ready(self):
        ident, _, _ = self.finish()
        self.adapter.ready = False
        result = self.start(ident)
        self.assertTrue(result['duplicate'])
        self.assertFalse(result['verified'])
        self.assertEqual(result['detail_code'], 'native_unavailable')
        self.assertEqual(len(self.adapter.calls), 1)

    def test_timeout_after_delivery_stays_unknown_and_late_exact_reply_can_reconcile(self):
        ident, body, nonce = self.dispatched()
        self.now += 121
        result = self.checks.status(self.agent['id'])
        self.assertEqual(result['state'], 'unconfirmed'); self.assertFalse(result['can_retry'])
        self.assertEqual(result['detail_code'], 'test_timeout')
        with self.assertRaises(ConnectionCheckError): self.start()
        self.assertTrue(self.start(ident)['duplicate'])
        self.checks.expire_pending(); self.commands.tick(); self.assertEqual(len(self.adapter.calls), 1)
        self.accept(self.event(ident, 'turn_started', body)); self.accept(self.event(ident, 'turn_completed', nonce))
        self.assertTrue(self.checks.status(self.agent['id'])['verified'])

    def test_never_delivered_expired_check_is_cancelled_without_touching_ordinary_pending_work(self):
        started = self.start(); ident = started['test_id']
        ordinary = str(uuid.uuid4())
        self.commands.create({'request_id': ordinary, 'agent_id': self.agent['id'], 'text': 'Ordinary task remains'})
        self.now += 121
        self.checks.expire_pending()
        rows = {r['id']: r for r in self.commands.db.execute('SELECT * FROM commands')}
        self.assertEqual(rows[ident]['state'], 'cancelled'); self.assertIsNone(rows[ident]['payload'])
        self.assertEqual(rows[ordinary]['state'], 'pending'); self.assertEqual(rows[ordinary]['payload'], 'Ordinary task remains')
        self.assertEqual(self.adapter.calls, [])

    def test_route_change_cancels_only_unsent_check_and_invalidates_prior_success(self):
        first = self.start()['test_id']
        self.checks.route_changed(self.agent['id'])
        self.assertEqual(self.commands.db.execute('SELECT state FROM commands WHERE id=?', (first,)).fetchone()[0], 'cancelled')
        self.assertEqual(self.checks.status(self.agent['id'])['state'], 'ready_to_test')
        with self.assertRaises(ConnectionCheckError): self.start(first)
        self.finish()
        self.checks.route_changed(self.agent['id'])
        self.assertFalse(self.checks.status(self.agent['id'])['verified'])

    def test_disable_logout_membership_change_and_runtime_unavailable_cannot_show_verified(self):
        self.finish()
        self.assertFalse(self.checks.status(self.agent['id'], native_ready=False)['verified'])
        self.config.setdefault('disabled_command_agents', []).append(self.agent['id'])
        self.assertEqual(self.checks.status(self.agent['id'])['state'], 'disabled')
        self.config['disabled_command_agents'].clear()
        self.agent['token_sha256'] = 'a-new-enrollment'
        self.assertEqual(self.checks.status(self.agent['id'])['state'], 'ready_to_test')
        self.config['agents'] = []
        self.assertFalse(self.checks.status(self.agent['id'])['verified'])
        self.assertEqual(self.checks.status(self.agent['id'])['detail_code'], 'agent_not_selected')

    def test_command_id_collision_busy_agent_and_unavailable_route_create_no_check(self):
        ordinary = str(uuid.uuid4())
        self.commands.create({'request_id': ordinary, 'agent_id': self.agent['id'], 'text': 'Existing user work'})
        for ident in [ordinary, str(uuid.uuid4())]:
            with self.assertRaises(ConnectionCheckError): self.start(ident)
        self.assertEqual(self.commands.db.execute('SELECT COUNT(*) FROM connection_checks').fetchone()[0], 0)
        self.commands.db.execute('DELETE FROM commands'); self.commands.db.commit()
        self.adapter.ready = False
        with self.assertRaises(ConnectionCheckError): self.start()
        self.assertEqual(self.commands.db.execute('SELECT COUNT(*) FROM connection_checks').fetchone()[0], 0)

    def test_command_creation_failure_rolls_back_check_and_command_atomically(self):
        original = self.commands.create
        def fail_after_insert(data, *, commit=True):
            original(data, commit=commit)
            raise RuntimeError('simulated precommit failure')
        with patch.object(self.commands, 'create', side_effect=fail_after_insert):
            with self.assertRaises(RuntimeError): self.start()
        self.assertEqual(self.commands.db.execute('SELECT COUNT(*) FROM connection_checks').fetchone()[0], 0)
        self.assertEqual(self.commands.db.execute('SELECT COUNT(*) FROM commands').fetchone()[0], 0)
        self.assertEqual(self.adapter.calls, [])

    def test_unicode_prompt_callback_can_beat_submit_receipt(self):
        def early(session, body):
            ident = body.splitlines()[0][8:-1]
            self.accept(self.event(ident, 'turn_started', body))
            self.accept(self.event(ident, 'turn_completed', body.splitlines()[-1]))
            return {'native_message_id': 'early-receipt'}
        self.adapter.effect = early
        self.start(); self.commands.tick()
        self.assertTrue(self.checks.status(self.agent['id'])['verified'])

    def test_unknown_delivery_never_retries_or_cancels(self):
        def unknown(*args): raise DeliveryError('delivery_unknown', uncertain=True)
        self.adapter.effect = unknown
        request = self.start()['test_id']; self.commands.tick()
        self.now += 121
        self.checks.expire_pending(); self.commands.tick()
        self.assertEqual(self.checks.status(self.agent['id'])['state'], 'unconfirmed')
        self.assertFalse(self.checks.status(self.agent['id'])['can_retry'])
        self.assertTrue(self.start(request)['duplicate'])
        with self.assertRaises(ConnectionCheckError): self.start()
        self.assertEqual(len(self.adapter.calls), 1)


if __name__ == '__main__':
    unittest.main()
