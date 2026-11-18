"""Phone dispatch authorization with fake adapters; no model or network calls."""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from agent_inbox import Inbox
from commands import Commands, CommandError, MOBILE_COMMAND_TTL
from history import HistoryStore
from security_keys import upgrade_security


SESSION = '00000000-0000-4000-8000-000000000001'
AGENT = 'codex:' + SESSION
OTHER = 'generic:fixture-inbox'


class FakeAdapter:
    reason = None

    def __init__(self):
        self.calls = []
        self.ready = True
        self.probe = None
        self.effect = None

    def available(self):
        if self.probe:
            self.probe()
        return self.ready

    def submit(self, session, text):
        self.calls.append((session, text))
        if self.effect:
            return self.effect(session, text)
        return {'native_message_id': 'fake-receipt'}

    def approvals(self, _session):
        raise AssertionError('Mobile views must not read native approvals')


class MobileCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.history = HistoryStore(self.root / 'history.sqlite3', secrets=('test-secret',))
        self.config = {'data_dir': str(self.root), 'topic': 'legacy-fixture-topic-abcdefghijklmnop',
                       'agents': [{'id': AGENT, 'provider': 'codex', 'source_id': SESSION, 'name': 'Fixture'},
                                  {'id': OTHER, 'provider': 'generic', 'driver': 'push',
                                   'source_id': 'fixture-inbox', 'name': 'Inbox'}],
                       'command_bindings': {AGENT: {'adapter': 'codex_app', 'session_id': SESSION}}}
        self.adapter = FakeAdapter()
        self.commands = Commands(self.config, self.history, adapters={'codex_app': self.adapter})
        self.active = True
        self.commands.mobile_authorizer = lambda origin, agent: self.active and origin['agent_id'] == agent

    def tearDown(self):
        self.commands.close()
        self.history.close()
        self.temp.cleanup()

    def request(self, agent=AGENT):
        return {'request_id': str(uuid.uuid4()), 'agent_id': agent, 'text': 'Compute a bounded fixture'}

    def origin(self, **overrides):
        return {'device_id': 'phone-fixture-a', 'revision': 1, 'expires_at': time.time() + 600, **overrides}

    def row(self, request):
        return self.commands.db.execute('SELECT * FROM commands WHERE id=?', (request['request_id'],)).fetchone()

    def inbox(self):
        bridge = SimpleNamespace(config=self.config, commands=self.commands, history=self.history,
            definitions={a['id']: a for a in self.config['agents']}, stop=threading.Event(), report=lambda *a, **k: None)
        inbox = Inbox(bridge)
        self.commands.inbox = inbox
        return inbox

    @staticmethod
    def receive_data():
        return {'action': 'receive', 'receiver_id': str(uuid.uuid4()),
                'request_id': str(uuid.uuid4()), 'wait_seconds': 0}

    def test_mobile_origin_is_stored_but_not_exposed_in_public_command(self):
        request, origin = self.request(), self.origin()
        result = self.commands.create(request, origin=origin)
        row = self.row(request)
        self.assertEqual(row['source_device'], origin['device_id'])
        self.assertEqual(row['source_revision'], 1)
        self.assertEqual(row['source_expires_at'], origin['expires_at'])
        self.assertNotIn('source_device', result['command'])
        self.assertNotIn('payload', result['command'])
        self.commands.tick()
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.row(request)['state'], 'queued')

    def test_owner_request_cannot_forge_origin_in_json(self):
        with self.assertRaises(CommandError) as error:
            self.commands.create({**self.request(), 'origin': self.origin()})
        self.assertEqual(error.exception.code, 'invalid_command')

    def test_membership_origin_survives_restart_without_public_disclosure(self):
        request = self.request()
        membership = 'fixture-membership-generation-1'
        result = self.commands.create(request, origin=self.origin(membership=membership))
        self.assertEqual(self.row(request)['source_membership'], membership)
        self.assertNotIn('source_membership', result['command'])
        self.assertNotIn('membership', result['command'])
        self.commands.close()
        self.commands = Commands(self.config, self.history, adapters={'codex_app': self.adapter})
        seen = []
        def authorize(origin, agent):
            seen.append(origin)
            return origin.get('membership') == membership and agent == AGENT
        self.commands.mobile_authorizer = authorize
        self.commands.tick()
        self.assertTrue(seen)
        self.assertTrue(all(origin.get('membership') == membership for origin in seen))
        self.assertEqual(len(self.adapter.calls), 1)

    def test_membership_rebinding_cancels_old_command_even_with_same_native_session(self):
        request = self.request()
        current = ['membership-before-rejoin']
        self.commands.mobile_authorizer = lambda origin, agent: origin.get('membership') == current[0]
        self.commands.create(request, origin=self.origin(membership=current[0]))
        # The native session and Agent ID are unchanged; only the enrollment changed.
        self.adapter.probe = lambda: current.__setitem__(0, 'membership-after-rejoin')
        self.commands.tick()
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.row(request)['state'], 'cancelled')
        self.assertEqual(self.row(request)['detail_code'], 'mobile_authorization_revoked')

    def test_wrong_membership_is_rejected_before_a_command_is_created(self):
        request = self.request()
        self.commands.mobile_authorizer = lambda origin, agent: origin.get('membership') == 'current'
        with self.assertRaises(CommandError) as error:
            self.commands.create(request, origin=self.origin(membership='previous'))
        self.assertEqual(error.exception.code, 'mobile_authorization_revoked')
        self.assertIsNone(self.row(request))

    def test_same_uuid_cannot_cross_membership_generations(self):
        request = self.request()
        self.commands.create(request, origin=self.origin(membership='first'))
        self.assertTrue(self.commands.create(request, origin=self.origin(membership='first'))['duplicate'])
        for origin in (self.origin(membership='second'), self.origin()):
            with self.subTest(origin=origin), self.assertRaises(CommandError) as error:
                self.commands.create(request, origin=origin)
            self.assertEqual(error.exception.code, 'request_id_conflict')

    def test_existing_mobile_command_without_membership_migrates_as_legacy(self):
        request = self.request()
        self.commands.create(request, origin=self.origin())
        before = self.row(request)['source_device']
        self.commands.close()
        with sqlite3.connect(self.root / 'commands.sqlite3') as db:
            db.execute('ALTER TABLE commands DROP COLUMN source_membership')
        self.commands = Commands(self.config, self.history, adapters={'codex_app': self.adapter})
        self.commands.mobile_authorizer = lambda origin, agent: 'membership' not in origin and agent == AGENT
        self.assertEqual(self.row(request)['source_device'], before)
        self.assertIsNone(self.row(request)['source_membership'])
        self.assertTrue(self.commands.create(request, origin=self.origin())['duplicate'])
        self.commands.tick()
        self.assertEqual(len(self.adapter.calls), 1)

    def test_missing_authorizer_fails_closed_for_mobile_but_keeps_owner(self):
        self.commands.mobile_authorizer = None
        with self.assertRaises(CommandError) as error:
            self.commands.create(self.request(), origin=self.origin())
        self.assertEqual(error.exception.status, 403)
        self.commands.create(self.request())
        self.commands.tick()
        self.assertEqual(len(self.adapter.calls), 1)

    def test_invalid_expiry_and_target_and_revision_rejected(self):
        cases = [dict(expires_at=float('inf')), dict(expires_at=float('nan')),
                 dict(expires_at=time.time() + MOBILE_COMMAND_TTL + 20), dict(expires_at=True),
                 dict(revision=True), dict(revision=0), dict(agent_id=OTHER), dict(device_id='../phone'),
                 dict(membership=None), dict(membership=''), dict(membership='x' * 513),
                 dict(membership='unsafe\nvalue'), dict(membership=False)]
        for override in cases:
            with self.subTest(override=override), self.assertRaises(CommandError):
                self.commands.create(self.request(), origin=self.origin(**override))
        with self.assertRaises(CommandError) as error:
            self.commands.create(self.request(), origin=self.origin(expires_at=time.time() - 1))
        self.assertEqual(error.exception.status, 403)

    def test_same_device_duplicate_does_not_extend_original_expiry(self):
        request, origin = self.request(), self.origin()
        self.commands.create(request, origin=origin)
        self.assertTrue(self.commands.create(request, origin=self.origin(expires_at=time.time() + 800))['duplicate'])
        self.assertEqual(self.row(request)['source_expires_at'], origin['expires_at'])

    def test_fresh_retry_cannot_revive_expired_pending_command(self):
        request, origin = self.request(), self.origin(expires_at=time.time() + 2)
        self.commands.create(request, origin=origin)
        with patch('commands.time.time', return_value=origin['expires_at'] + 1):
            result = self.commands.create(request, origin={**origin, 'expires_at': origin['expires_at'] + 500})
        self.assertTrue(result['duplicate'])
        self.assertEqual(result['command']['state'], 'cancelled')
        self.assertEqual(self.adapter.calls, [])

    def test_same_uuid_other_device_revision_or_owner_cannot_disclose_command(self):
        request = self.request()
        self.commands.create(request, origin=self.origin())
        for origin in (self.origin(device_id='phone-fixture-b'), self.origin(revision=2), None):
            with self.subTest(origin=origin), self.assertRaises(CommandError) as error:
                self.commands.create(request, origin=origin)
            self.assertEqual(error.exception.code, 'request_id_conflict')
        owner = self.request()
        self.commands.create(owner)
        with self.assertRaises(CommandError):
            self.commands.create(owner, origin=self.origin())

    def test_duplicate_changed_text_and_agent_are_rejected(self):
        request = self.request()
        self.commands.create(request, origin=self.origin())
        for changed in ({**request, 'text': 'Different body'}, {**request, 'agent_id': OTHER}):
            with self.assertRaises(CommandError):
                self.commands.create(changed, origin=self.origin())

    def test_revoked_pending_is_cancelled_before_offline_adapter_returns(self):
        request = self.request()
        self.adapter.ready = False
        self.commands.create(request, origin=self.origin())
        self.active = False
        self.commands.tick()
        self.adapter.ready = True
        self.commands.tick()
        self.assertEqual(self.row(request)['state'], 'cancelled')
        self.assertIsNone(self.row(request)['payload'])
        self.assertEqual(self.adapter.calls, [])

    def test_expired_pending_is_cancelled_before_dispatch(self):
        request, origin = self.request(), self.origin()
        self.commands.create(request, origin=origin)
        with patch('commands.time.time', return_value=origin['expires_at'] + 1):
            self.commands.tick()
        self.assertEqual(self.row(request)['detail_code'], 'mobile_command_expired')
        self.assertEqual(self.adapter.calls, [])

    def test_authorizer_failure_never_dispatches(self):
        request = self.request()
        self.commands.create(request, origin=self.origin())
        def broken(*_):
            raise OSError('fixture unavailable')
        self.commands.mobile_authorizer = broken
        self.commands.tick()
        self.assertEqual(self.row(request)['state'], 'cancelled')
        self.assertEqual(self.adapter.calls, [])

    def test_authorization_is_rechecked_after_adapter_probe(self):
        request = self.request()
        self.commands.create(request, origin=self.origin())
        self.adapter.probe = lambda: setattr(self, 'active', False)
        self.commands.tick()
        self.assertEqual(self.row(request)['state'], 'cancelled')
        self.assertEqual(self.adapter.calls, [])

    def test_revocation_waits_for_started_submit_and_does_not_claim_cancelled(self):
        request, origin = self.request(), self.origin()
        self.commands.create(request, origin=origin)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        outcome = []
        def submit(*_):
            entered.set()
            if not release.wait(2):
                raise AssertionError('fixture release timed out')
            return {'native_message_id': 'fake-receipt'}
        def revoke():
            with self.commands.lock:
                self.active = False
                outcome.append(self.commands.revoke_mobile_pending(origin['device_id']))
            finished.set()
        self.adapter.effect = submit
        dispatch = threading.Thread(target=self.commands.tick)
        dispatch.start()
        self.assertTrue(entered.wait(2))
        revoker = threading.Thread(target=revoke)
        revoker.start()
        self.assertFalse(finished.wait(.05))
        release.set()
        dispatch.join(2)
        revoker.join(2)
        self.assertFalse(dispatch.is_alive())
        self.assertFalse(revoker.is_alive())
        self.assertEqual(outcome[0]['cancelled'], 0)
        self.assertEqual(outcome[0]['already_received'], 1)
        self.assertEqual(self.row(request)['state'], 'queued')

    def test_revoke_only_cancels_matching_device_unstarted_work(self):
        a, b, owner = self.request(), self.request(), self.request()
        self.commands.create(a, origin=self.origin())
        self.commands.create(b, origin=self.origin(device_id='phone-fixture-b'))
        self.commands.create(owner)
        result = self.commands.revoke_mobile_pending('phone-fixture-a')
        self.assertEqual(result, {'cancelled': 1, 'blocked_before_start': 0, 'already_received': 0})
        self.assertEqual(self.row(a)['state'], 'cancelled')
        self.assertEqual(self.row(b)['state'], 'pending')
        self.assertEqual(self.row(owner)['state'], 'pending')

    def test_phone_cannot_cancel_owner_or_another_device_command(self):
        request = self.request()
        self.commands.create(request)
        data = {'agent_id': AGENT, 'command_id': request['request_id']}
        with self.assertRaises(CommandError) as error:
            self.commands.cancel_pending(data, origin=self.origin())
        self.assertEqual(error.exception.status, 404)
        own = self.request()
        self.commands.create(own, origin=self.origin())
        self.assertEqual(self.commands.cancel_pending({'agent_id': AGENT, 'command_id': own['request_id']},
                          origin=self.origin())['command']['state'], 'cancelled')

    def test_mobile_view_does_not_read_native_approvals(self):
        owner, mine = self.request(), self.request()
        self.commands.create(owner)
        self.commands.create(mine, origin=self.origin())
        result = self.commands.list_mobile(AGENT, self.origin())
        self.assertEqual(result['approvals'], [])
        items = {row['id']: row for row in result['commands']}
        self.assertFalse(items[owner['request_id']]['can_cancel'])
        self.assertTrue(items[mine['request_id']]['can_cancel'])
        self.assertFalse(any(row['can_cancel'] for row in self.commands.list_mobile_view(AGENT)['commands']))

    def test_restart_retains_origin_and_missing_authorizer_blocks_pending(self):
        request = self.request()
        self.commands.create(request, origin=self.origin())
        self.commands.close()
        self.commands = Commands(self.config, self.history, adapters={'codex_app': self.adapter})
        self.commands.tick()
        self.assertEqual(self.row(request)['state'], 'cancelled')
        self.assertEqual(self.adapter.calls, [])

    def test_old_command_schema_migrates_without_changing_owner_dedupe(self):
        request = self.request()
        self.commands.create(request)
        old_fingerprint = self.row(request)['fingerprint']
        self.commands.close()
        db = sqlite3.connect(self.root / 'commands.sqlite3')
        db.execute('DROP INDEX command_mobile_source')
        for column in ('source_device', 'source_revision', 'source_expires_at', 'source_membership'):
            db.execute('ALTER TABLE commands DROP COLUMN ' + column)
        db.commit()
        db.close()
        self.config = upgrade_security(self.config)
        self.commands = Commands(self.config, self.history, adapters={'codex_app': self.adapter})
        self.assertIsNone(self.row(request)['source_device'])
        self.assertIsNone(self.row(request)['source_membership'])
        self.assertTrue(self.commands.create(request)['duplicate'])
        self.assertEqual(self.row(request)['fingerprint'], old_fingerprint)
        self.commands.tick()
        body = self.adapter.calls[0][1]
        self.assertEqual(self.commands.observe_prompt(AGENT, 'fixture-run', body), request['text'])
        self.assertEqual(self.row(request)['state'], 'running')

    def test_revoked_running_work_keeps_real_completion_observation(self):
        request = self.request()
        self.commands.create(request, origin=self.origin())
        self.commands.tick()
        self.commands.observe_prompt(AGENT, 'live-fixture', self.adapter.calls[0][1])
        self.active = False
        result = self.commands.revoke_mobile_pending('phone-fixture-a')
        self.assertEqual(result['already_received'], 1)
        self.assertEqual(self.row(request)['state'], 'running')
        self.commands.observe_run(AGENT, 'live-fixture', 'done')
        self.assertEqual(self.row(request)['state'], 'completed')

    def test_inbox_revocation_before_receive_never_releases_payload(self):
        inbox, request = self.inbox(), self.request(OTHER)
        self.commands.create(request, origin=self.origin())
        self.active = False
        result = inbox.receive(OTHER, self.receive_data())
        self.assertEqual(result['state'], 'waiting')
        self.assertEqual(self.row(request)['state'], 'cancelled')
        self.assertIsNone(self.row(request)['payload'])

    def test_inbox_revocation_after_receipt_blocks_start_and_retry_body(self):
        inbox, request = self.inbox(), self.request(OTHER)
        self.commands.create(request, origin=self.origin())
        receive = self.receive_data()
        delivered = inbox.receive(OTHER, receive)
        self.assertFalse(delivered['execute'])
        self.active = False
        repeated = inbox.receive(OTHER, receive)
        self.assertNotIn('text', repeated)
        result = inbox.event(OTHER, {'action': 'start', 'command_id': request['request_id'],
                                   'receipt_id': delivered['receipt_id']})
        self.assertFalse(result['execute'])
        self.assertEqual(result['state'], 'cancelled')

    def test_inbox_start_rechecks_expiry_without_tick_or_repeat(self):
        inbox, request, origin = self.inbox(), self.request(OTHER), self.origin()
        self.commands.create(request, origin=origin)
        delivered = inbox.receive(OTHER, self.receive_data())
        with patch('commands.time.time', return_value=origin['expires_at'] + 1):
            result = inbox.event(OTHER, {'action': 'start', 'command_id': request['request_id'],
                                       'receipt_id': delivered['receipt_id']})
        self.assertFalse(result['execute'])
        self.assertEqual(self.row(request)['state'], 'cancelled')

    def test_revoke_reports_inbox_received_but_unstarted_separately(self):
        inbox, request = self.inbox(), self.request(OTHER)
        self.commands.create(request, origin=self.origin())
        inbox.receive(OTHER, self.receive_data())
        result = self.commands.revoke_mobile_pending('phone-fixture-a')
        self.assertEqual(result, {'cancelled': 0, 'blocked_before_start': 1, 'already_received': 0})
        self.assertEqual(self.row(request)['state'], 'cancelled')

    def test_inbox_event_duplicate_survives_integrity_key_migration(self):
        inbox, request = self.inbox(), self.request(OTHER)
        self.commands.create(request)
        delivery = inbox.receive(OTHER, self.receive_data())
        base = {'command_id': request['request_id'], 'receipt_id': delivery['receipt_id']}
        self.assertTrue(inbox.event(OTHER, {**base, 'action': 'start'})['execute'])
        final = {**base, 'action': 'complete', 'event_id': str(uuid.uuid4()), 'text': 'Fixture delivery'}
        self.assertFalse(inbox.event(OTHER, final)['duplicate'])
        self.config = upgrade_security(self.config)
        self.commands.config = self.config
        self.assertTrue(inbox.event(OTHER, final)['duplicate'])
        self.assertEqual(self.history.db.execute("SELECT COUNT(*) FROM messages WHERE agent_id=? AND phase='final'", (OTHER,)).fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
