"""Grok SDK command/capability contracts with local fake native events only."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from agent_inbox import Inbox, InboxError
from command_adapters import DeliveryError
from commands import Commands
from history import HistoryStore


SESSION = 'sdk-00000000-0000-4000-8000-000000000041'
AGENT = 'grok_bot:' + SESSION


class SdkAdapter:
    mode, serial, same_session = 'grok_sdk', True, True
    reason = None

    def __init__(self):
        self.calls, self.effect = [], None
        self.ready, self.verified = True, True

    def available(self, session):
        return self.ready and session == SESSION

    def can_dispatch(self, session):
        return session == SESSION

    def native_verified(self, session):
        return self.verified and session == SESSION

    def submit(self, session, text):
        self.calls.append((session, text))
        if self.effect:
            return self.effect(session, text)
        message_id = text.split(']', 1)[0][len('[OFFICE:'):]
        return {'native_message_id': message_id, 'delivery_mode': self.mode}


class ScopedObserver:
    def __init__(self):
        self.working = False
        self.snapshot = {'state': 'connecting', 'active': False,
                         'execution_verified': False, 'reason': 'grok_sdk_sync_connecting'}

    def describe(self, agent):
        return dict(self.snapshot) if agent == AGENT else None

    def busy(self, agent):
        return self.working and agent == AGENT

    def close(self):
        pass


class GrokSdkCommandIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.history = HistoryStore(self.root / 'history.sqlite3', secrets=('private-fixture-secret',))
        self.agent = {'id': AGENT, 'provider': 'grok_bot', 'driver': 'grok_sdk',
                      'source_id': SESSION, 'name': 'Grok SDK fixture'}
        self.config = {
            'data_dir': str(self.root), 'topic': 'fixture-office-owner-key',
            'agents': [self.agent],
            'command_bindings': {AGENT: {'adapter': 'grok_sdk', 'session_id': SESSION}},
        }
        self.adapter, self.observer = SdkAdapter(), ScopedObserver()
        self.events = []
        self.open_commands()

    def open_commands(self):
        self.commands = Commands(self.config, self.history, adapters={'grok_sdk': self.adapter},
                                 on_status=lambda agent, event: self.events.append((agent, event)))
        self.commands.grok_sdk_sync = self.observer
        self.commands.public_sync_observer = self.observer.describe

    def tearDown(self):
        self.commands.close()
        self.history.close()
        self.temp.cleanup()

    def request(self, text='Return the result of 17 * 23'):
        return {'request_id': str(uuid.uuid4()), 'agent_id': AGENT, 'text': text}

    def row(self, request):
        return self.commands.db.execute('SELECT * FROM commands WHERE id=?',
                                        (request['request_id'],)).fetchone()

    def run_row(self, request):
        return self.history.db.execute('SELECT * FROM runs WHERE agent_id=? AND run_id=?',
                                       (AGENT, request['request_id'])).fetchone()

    def messages(self):
        return self.history.messages({AGENT}, AGENT)

    def dispatch(self, request):
        self.commands.create(request)
        self.commands.tick()
        return self.adapter.calls[-1][1]

    def event(self, request, kind, **fields):
        agent = fields.pop('agent', AGENT)
        self.commands.observe_native(agent, {
            'adapter': 'grok_sdk', 'session_id': SESSION,
            'run_id': request['request_id'], 'native_run_id': request['request_id'],
            'type': kind, **fields,
        })

    def test_setup_and_live_observation_do_not_imply_verified_execution(self):
        initial = self.commands.capability(AGENT)
        self.assertEqual(initial['mode'], 'grok_sdk')
        self.assertEqual(initial['conversation_scope'], 'selected_native_session')
        self.assertTrue(initial['experimental'])
        self.assertTrue(initial['same_session'])
        self.assertTrue(initial['persistent'])
        self.assertTrue(initial['native_target_verified'])
        self.assertFalse(initial['execution_verified'])
        self.assertNotIn('connection_check', initial, 'SDK must not inherit Webhook setup UI')
        self.assertEqual(initial['public_sync']['state'], 'connecting')
        self.assertFalse(initial['public_sync']['manual_app_automatic'])
        self.observer.snapshot.update(state='configured', active=True, reason=None)
        live = self.commands.capability(AGENT)
        self.assertEqual(live['public_sync']['mode'], 'grok_sdk_observer')
        self.assertEqual(live['public_sync']['scope'], 'selected_native_session')
        self.assertTrue(live['public_sync']['manual_app_automatic'])
        self.assertTrue(live['public_sync']['public_only'])
        self.assertFalse(live['execution_verified'])
        self.observer.snapshot['execution_verified'] = True
        self.assertTrue(self.commands.capability(AGENT)['execution_verified'])
        self.assertEqual(self.adapter.calls, [])

    def test_recovery_and_unavailable_observers_do_not_claim_live_app_sync(self):
        for state in ('connecting', 'recovering', 'unavailable'):
            with self.subTest(state=state):
                self.observer.snapshot.update(state=state, active=True)
                result = self.commands.capability(AGENT)['public_sync']
                self.assertEqual(result['state'], state)
                self.assertFalse(result['automatic'])
                self.assertFalse(result['manual_app_automatic'])
        self.config['agents'] = []
        capability = self.commands.capability(AGENT)
        self.assertFalse(capability['can_send'])
        self.assertEqual(capability['public_sync']['state'], 'unsupported')

    def test_send_ack_is_only_queued_and_has_no_reply_or_delivery_record(self):
        request = self.request()
        self.dispatch(request)
        self.assertEqual(self.row(request)['state'], 'queued')
        self.assertEqual(self.row(request)['native_message_id'], request['request_id'])
        self.assertIsNone(self.row(request)['payload'])
        self.assertIsNone(self.run_row(request))
        self.assertEqual(self.messages(), [])
        self.commands.tick()
        self.assertEqual(len(self.adapter.calls), 1)

    def test_native_events_require_selected_identity_and_exact_send_receipt(self):
        request = self.request()
        self.commands.create(request)
        self.event(request, 'finished', state='completed', text='Before native receipt')
        self.assertEqual(self.row(request)['state'], 'pending')
        self.commands.tick()
        prompt = self.adapter.calls[-1][1]
        invalid = [
            {'agent': 'grok_bot:unselected'}, {'session_id': 'sdk-other'},
            {'adapter': 'grok_webhook'}, {'run_id': str(uuid.uuid4())},
            {'native_run_id': str(uuid.uuid4())}, {'native_run_id': None},
        ]
        for fields in invalid:
            with self.subTest(fields=fields):
                self.event(request, 'started', text=prompt, **fields)
                self.event(request, 'text', segment='native-entry', text='Wrong reply', **fields)
                self.event(request, 'finished', state='completed', text='Wrong delivery', **fields)
                self.assertEqual(self.row(request)['state'], 'queued')
                self.assertEqual(self.messages(), [])
                self.assertIsNone(self.run_row(request))
        self.event(request, 'started', text=prompt)
        self.assertEqual(self.row(request)['state'], 'running')

    def test_public_final_bubble_needs_separate_terminal_evidence_and_survives_restart(self):
        request = self.request('17 * 23; private-fixture-secret')
        prompt = self.dispatch(request)
        self.event(request, 'started', text=prompt)
        self.event(request, 'text', segment='public-native-entry', text='The result is')
        event_id = self.messages()[-1]['event_id']
        self.event(request, 'text', segment='public-native-entry', text='391', final=True)
        self.event(request, 'text', segment='public-native-entry', text='391', final=True)
        self.assertEqual(len(self.messages()), 2)
        self.assertEqual(self.messages()[-1]['event_id'], event_id)
        self.assertEqual(self.messages()[-1]['phase'], 'final')
        self.assertNotIn('private-fixture-secret', json.dumps(self.messages()))
        self.assertNotIn('[OFFICE:', self.messages()[0]['text'])
        self.assertEqual(self.row(request)['state'], 'running')
        self.event(request, 'finished', state='completed', text='391')
        self.assertEqual(self.row(request)['state'], 'completed')
        self.assertEqual(self.run_row(request)['status'], 'done')
        self.assertEqual(self.run_row(request)['source_kind'], 'grok_sdk')
        self.assertEqual(self.run_row(request)['delivery'], '391')
        self.event(request, 'finished', state='failed', text='Late stale failure')
        self.commands.close()
        self.open_commands()
        self.assertTrue(self.commands.create(request)['duplicate'])
        self.commands.tick()
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.row(request)['state'], 'completed')
        self.assertEqual(self.run_row(request)['delivery'], '391')

    def test_accepted_reply_and_terminal_can_arrive_before_submit_returns(self):
        request = self.request()
        def receive_early(session, prompt):
            self.assertEqual(self.row(request)['state'], 'sending')
            self.event(request, 'accepted')
            self.event(request, 'started', text=prompt)
            self.event(request, 'text', segment='native-entry', text='391', final=True)
            self.event(request, 'finished', state='completed', text='391')
            return {'native_message_id': request['request_id']}
        self.adapter.effect = receive_early
        self.dispatch(request)
        self.assertEqual(self.row(request)['state'], 'completed')
        self.assertEqual(self.run_row(request)['delivery'], '391')

    def test_native_app_work_holds_office_queue_without_losing_payload(self):
        request = self.request()
        self.commands.create(request)
        self.observer.working = True
        self.commands.tick()
        self.assertEqual(self.row(request)['state'], 'pending')
        self.assertEqual(self.row(request)['payload'], request['text'])
        self.assertEqual(self.adapter.calls, [])
        self.observer.working = False
        self.commands.tick()
        self.assertEqual(self.row(request)['state'], 'queued')
        self.assertEqual(len(self.adapter.calls), 1)

    def test_uncertain_send_is_not_replayed_or_overtaken_after_restart(self):
        request = self.request()
        def uncertain(*_):
            raise DeliveryError('grok_sdk_send_unconfirmed', uncertain=True)
        self.adapter.effect = uncertain
        self.dispatch(request)
        self.assertEqual(self.row(request)['state'], 'delivery_unknown')
        self.assertIsNone(self.row(request)['payload'])
        self.assertEqual(self.run_row(request)['status'], 'unknown')
        self.commands.close()
        self.open_commands()
        self.assertTrue(self.commands.create(request)['duplicate'])
        later = self.request('Do not overtake an uncertain send')
        self.commands.create(later)
        for _ in range(3):
            self.commands.tick()
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.row(request)['state'], 'delivery_unknown')
        self.assertEqual(self.row(later)['state'], 'pending')

    def test_removed_membership_fences_late_callbacks_and_preserves_history(self):
        request = self.request()
        prompt = self.dispatch(request)
        self.event(request, 'started', text=prompt)
        self.event(request, 'text', segment='existing', text='Before departure')
        before = self.messages()
        self.config['agents'] = []
        self.config['command_bindings'].pop(AGENT)
        self.commands.bindings = self.config['command_bindings']
        self.event(request, 'text', segment='late', text='After departure')
        self.event(request, 'finished', state='completed', text='Late completion')
        self.assertEqual(self.messages(), before)
        self.assertEqual(self.row(request)['state'], 'running')
        self.assertEqual(self.run_row(request)['status'], 'working')

    def test_detached_uncertain_command_cannot_be_revived_by_a_late_callback(self):
        request = self.request()
        self.dispatch(request)
        self.commands.db.execute("UPDATE commands SET state='delivery_unknown' WHERE id=?", (request['request_id'],))
        self.commands.db.commit()
        self.assertEqual(self.commands.detach_unconfirmed(AGENT), 1)
        self.event(request, 'started', text=self.adapter.calls[-1][1])
        self.event(request, 'text', segment='late', text='Late public response')
        self.event(request, 'finished', state='completed', text='Late delivery')
        self.assertEqual(self.row(request)['state'], 'delivery_unknown')
        self.assertIsNone(self.run_row(request))
        self.assertEqual(self.messages(), [])

    def test_sdk_receiver_description_never_creates_an_inbox_or_cooperative_setup(self):
        reporting = Mock(side_effect=AssertionError('SDK must not enter cooperative setup'))
        bridge = SimpleNamespace(config=self.config, commands=self.commands,
                                 history=self.history, definitions={AGENT: self.agent},
                                 get_public_reporting=reporting)
        inbox = Inbox(bridge)
        with patch('agent_inbox.private_write', side_effect=AssertionError('SDK must not write receiver files')):
            for ready in (True, False):
                self.adapter.ready = ready
                result = inbox.ensure(AGENT)
                self.assertEqual(result['reporting'], {'method': 'native_sdk', 'state': 'not_required'})
                self.assertIsNone(result['credential_path'])
                self.assertIsNone(result['guide_path'])
                self.assertEqual(result['capability']['mode'], 'grok_sdk')
                self.assertEqual(result['capability']['native_ready'], ready)
                self.assertFalse(result['capability']['execution_verified'])
                self.assertNotIn('grok_setup', result)
        self.assertFalse(inbox.root.exists())
        self.assertEqual(self.commands.db.execute('SELECT COUNT(*) FROM inbox_credentials').fetchone()[0], 0)
        self.assertEqual(self.commands.db.execute('SELECT COUNT(*) FROM commands').fetchone()[0], 0)
        self.assertEqual(self.adapter.calls, [])
        reporting.assert_not_called()

    def test_webhook_keeps_scoped_receiver_and_cooperative_guide_beside_sdk(self):
        ident = 'grok_bot:webhook-fixture'
        webhook = {'id': ident, 'provider': 'grok_bot', 'driver': 'push',
                   'source_id': 'webhook-fixture', 'name': 'Webhook fixture',
                   'token_sha256': 'a' * 64}
        self.config['port'] = 4318
        self.config['agents'].append(webhook)
        self.config['command_bindings'][ident] = {'adapter': 'grok_webhook', 'session_id': webhook['source_id']}
        adapter = SimpleNamespace(mode='grok_webhook', serial=True, same_session=False,
                                  reason=None, available=lambda _: True)
        self.commands.adapters['grok_webhook'] = adapter
        cooperative = Mock(spec=['ensure', 'status'])
        cooperative.ensure.return_value = {'method': 'cooperative', 'state': 'setup_required',
            'guide_path': str(self.root / 'cooperative-guide.txt'),
            'manifest_path': str(self.root / 'cooperative-manifest.json')}
        bridge = SimpleNamespace(config=self.config, commands=self.commands,
                                 history=self.history, definitions={AGENT: self.agent, ident: webhook},
                                 get_public_reporting=Mock(return_value=cooperative))
        inbox = Inbox(bridge)
        result = inbox.ensure(ident)
        credential, guide = Path(result['credential_path']), Path(result['guide_path'])
        self.assertTrue(credential.is_file())
        self.assertEqual(credential.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(credential.read_text())['agent_id'], ident)
        self.assertEqual(result['reporting']['method'], 'cooperative')
        self.assertEqual(result['capability']['mode'], 'grok_webhook')
        self.assertIn('grok_setup', result)
        self.assertIn('Grok Webhook routine', guide.read_text())
        cooperative.ensure.assert_called_once_with(ident)
        before = credential.read_bytes(), guide.read_bytes()
        sdk = inbox.ensure(AGENT)
        self.assertEqual(sdk['reporting']['method'], 'native_sdk')
        self.assertEqual((credential.read_bytes(), guide.read_bytes()), before)
        self.assertEqual(self.commands.db.execute('SELECT COUNT(*) FROM inbox_credentials').fetchone()[0], 1)
        cooperative.ensure.assert_called_once_with(ident)

    def test_unselected_sdk_identity_cannot_regenerate_a_receiver(self):
        bridge = SimpleNamespace(config=self.config, commands=self.commands,
                                 history=self.history, definitions={},
                                 get_public_reporting=Mock())
        inbox = Inbox(bridge)
        with self.assertRaises(InboxError) as error:
            inbox.ensure(AGENT)
        self.assertEqual((error.exception.status, error.exception.code), (404, 'agent_not_selected'))
        self.assertFalse(inbox.root.exists())
        self.assertEqual(self.commands.db.execute('SELECT COUNT(*) FROM inbox_credentials').fetchone()[0], 0)
        bridge.get_public_reporting.assert_not_called()


if __name__ == '__main__':
    unittest.main()
