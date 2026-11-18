"""Codex background command integration; fake native receipts, no CLI/model IO."""
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from command_adapters import DeliveryError
from commands import CommandError, Commands
from history import HistoryStore


SESSION = 'bg-00000000-0000-4000-8000-000000000031'
AGENT = 'codex:' + SESSION
NATIVE = 'turn-background-fixture'


class BackgroundAdapter:
    mode, serial, same_session = 'codex_background', True, True
    reason = None

    def __init__(self):
        self.calls = []
        self.effect = None
        self.ready = True
        self.verified = False
        self.pending_approvals = []
        self.decisions = []

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
        return {'native_message_id': NATIVE, 'delivery_mode': self.mode}

    def approvals(self, session):
        return list(self.pending_approvals) if session == SESSION else []

    def decide(self, session, approval, decision):
        if session != SESSION or not any(item['id'] == approval for item in self.pending_approvals):
            return False
        self.decisions.append((approval, decision))
        self.pending_approvals = [item for item in self.pending_approvals if item['id'] != approval]
        return True


class CodexBackgroundCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.history = HistoryStore(self.root / 'history.sqlite3', secrets=('fixture-private-value',))
        self.config = {
            'data_dir': str(self.root), 'topic': 'fixture-office-owner-key',
            'agents': [{'id': AGENT, 'provider': 'codex', 'driver': 'codex_background',
                        'source_id': SESSION, 'name': 'Background fixture'}],
            'command_bindings': {AGENT: {'adapter': 'codex_background', 'session_id': SESSION}},
        }
        self.adapter = BackgroundAdapter()
        self.status_events = []
        self.open_commands()

    def open_commands(self):
        self.commands = Commands(self.config, self.history,
            adapters={'codex_background': self.adapter},
            on_status=lambda agent, event: self.status_events.append((agent, event)))

    def tearDown(self):
        self.commands.close()
        self.history.close()
        self.temp.cleanup()

    def request(self, text='Calculate 17 * 23'):
        return {'request_id': str(uuid.uuid4()), 'agent_id': AGENT, 'text': text}

    def row(self, request):
        return self.commands.db.execute('SELECT * FROM commands WHERE id=?',
                                       (request['request_id'],)).fetchone()

    def event(self, request, kind, **fields):
        agent = fields.pop('agent', AGENT)
        data = {'adapter': 'codex_background', 'session_id': SESSION,
                'run_id': request['request_id'], 'native_run_id': NATIVE, 'type': kind,
                **fields}
        self.commands.observe_native(agent, data)

    def dispatch(self, request):
        self.commands.create(request)
        self.commands.tick()
        return self.adapter.calls[-1][1]

    def messages(self):
        return self.history.messages({AGENT}, AGENT)

    def history_run(self, request):
        return self.history.db.execute('SELECT * FROM runs WHERE agent_id=? AND run_id=?',
                                      (AGENT, request['request_id'])).fetchone()

    def test_capability_is_office_owned_read_only_and_not_original_app_observation(self):
        capability = self.commands.capability(AGENT)
        self.assertEqual(capability['mode'], 'codex_background')
        self.assertEqual(capability['conversation_scope'], 'office_owned_session')
        self.assertEqual(capability['tool_policy'], 'read_only')
        self.assertEqual(capability['approval_policy'], 'on-request')
        self.assertTrue(capability['native_ready'])
        self.assertTrue(capability['persistent'])
        self.assertFalse(capability['requires_active_turn'])
        self.assertFalse(capability['native_target_verified'])
        self.assertEqual(capability['background_child_activity'], 'unsupported')
        self.assertEqual(capability['public_sync']['scope'], 'office_dispatched_runs')
        self.assertFalse(capability['public_sync']['manual_app_automatic'])
        self.assertEqual(self.adapter.calls, [], 'Capability polling must not dispatch work')
        self.adapter.verified = True
        self.assertTrue(self.commands.capability(AGENT)['native_target_verified'])
        self.adapter.ready = False
        self.assertFalse(self.commands.capability(AGENT)['native_ready'])

    def test_receipt_alone_is_queued_and_cannot_claim_public_reply_or_delivery(self):
        request = self.request()
        self.dispatch(request)
        self.assertEqual(self.row(request)['state'], 'queued')
        self.assertEqual(self.row(request)['native_message_id'], NATIVE)
        self.assertIsNone(self.row(request)['payload'])
        self.assertIsNone(self.history_run(request))
        self.assertEqual(self.messages(), [])

    def test_native_events_require_matching_agent_binding_command_and_receipt(self):
        request = self.request()
        self.commands.create(request)
        self.event(request, 'finished', state='completed', text='Forged before receipt')
        self.assertEqual(self.row(request)['state'], 'pending')
        prompt = '[OFFICE:' + request['request_id'] + ']\n' + request['text']
        self.commands.tick()
        invalid = [
            {'agent': 'codex:bg-unselected'}, {'session_id': 'bg-another-session'},
            {'adapter': 'codex_app'}, {'run_id': str(uuid.uuid4())},
            {'native_run_id': 'turn-wrong'}, {'native_run_id': None},
        ]
        for fields in invalid:
            with self.subTest(fields=fields):
                self.event(request, 'started', text=prompt, **fields)
                self.event(request, 'text', segment='item-1', text='Forged public text', **fields)
                self.event(request, 'finished', state='completed', text='Forged delivery', **fields)
                self.assertEqual(self.row(request)['state'], 'queued')
                self.assertEqual(self.messages(), [])
                self.assertIsNone(self.history_run(request))
        self.event(request, 'accepted', native_run_id='turn-wrong')
        self.assertEqual(self.row(request)['native_message_id'], NATIVE)
        self.event(request, 'started', text=prompt)
        self.assertEqual(self.row(request)['state'], 'running')

    def test_public_snapshots_update_one_bubble_and_terminal_delivery_survives_restart(self):
        request = self.request('Calculate 17 * 23; fixture-private-value')
        prompt = self.dispatch(request)
        self.event(request, 'started', text=prompt)
        self.event(request, 'text', segment='native-item-1', text='The answer is')
        first = self.messages()[-1]
        self.event(request, 'text', segment='native-item-1', text='The answer is 391.', final=True)
        self.event(request, 'text', segment='native-item-1', text='The answer is 391.', final=True)
        messages = self.messages()
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]['role'], 'user')
        self.assertNotIn('[OFFICE:', messages[0]['text'])
        self.assertNotIn('fixture-private-value', json.dumps(messages))
        self.assertEqual(messages[-1]['event_id'], first['event_id'])
        self.assertGreater(messages[-1]['seq'], first['seq'])
        self.assertEqual(messages[-1]['phase'], 'final')
        self.assertEqual(self.row(request)['state'], 'running', 'A final bubble is not terminal proof')
        self.event(request, 'finished', state='completed', text='The answer is 391.')
        self.assertEqual(self.row(request)['state'], 'completed')
        self.assertEqual(self.history_run(request)['status'], 'done')
        self.assertEqual(self.history_run(request)['delivery'], 'The answer is 391.')
        self.assertEqual(self.history_run(request)['source_kind'], 'codex_background')
        self.assertEqual(self.history_run(request)['source_ref'], NATIVE)
        self.event(request, 'status', waiting=True)
        self.event(request, 'finished', state='failed', text='Late stale failure')
        self.assertEqual(self.row(request)['state'], 'completed')
        self.commands.close()
        self.open_commands()
        self.assertTrue(self.commands.create(request)['duplicate'])
        self.commands.tick()
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.history_run(request)['delivery'], 'The answer is 391.')
        self.assertEqual(self.messages()[-1]['text'], 'The answer is 391.')

    def test_accepted_started_and_final_can_arrive_before_submit_returns(self):
        request = self.request()
        self.commands.create(request)
        def complete_early(session, text):
            self.assertEqual(self.row(request)['state'], 'sending')
            self.event(request, 'accepted')
            self.assertEqual(self.row(request)['native_message_id'], NATIVE)
            self.event(request, 'started', text=text)
            self.event(request, 'text', segment='native-item-1', text='391', final=True)
            self.event(request, 'finished', state='completed', text='391')
            return {'native_message_id': NATIVE}
        self.adapter.effect = complete_early
        self.commands.tick()
        self.assertEqual(self.row(request)['state'], 'completed')
        self.assertEqual(self.history_run(request)['delivery'], '391')
        self.assertEqual(self.messages()[-1]['text'], '391')

    def test_interrupted_native_states_are_unknown_after_restart_and_never_resent(self):
        request = self.request()
        self.dispatch(request)
        for state in ('sending', 'queued', 'running', 'waiting_user'):
            with self.subTest(state=state):
                self.commands.db.execute('UPDATE commands SET state=? WHERE id=?',
                                         (state, request['request_id']))
                self.commands.db.commit()
                self.commands.close()
                self.open_commands()
                self.assertEqual(self.row(request)['state'], 'delivery_unknown')
                self.assertIsNone(self.row(request)['payload'])
                self.assertTrue(self.commands.create(request)['duplicate'])
                self.commands.tick()
                self.assertEqual(len(self.adapter.calls), 1)
        next_request = self.request('Do not overtake the uncertain native turn')
        self.commands.create(next_request)
        self.commands.tick()
        self.assertEqual(self.row(next_request)['state'], 'pending')
        self.assertEqual(len(self.adapter.calls), 1)

    def test_uncertain_submit_retains_audit_history_without_replaying(self):
        request = self.request('Keep this request in the audit history')
        def unknown(*args):
            raise DeliveryError('codex_background_delivery_unknown', uncertain=True)
        self.adapter.effect = unknown
        self.dispatch(request)
        for _ in range(3):
            self.commands.tick()
        self.assertEqual(self.row(request)['state'], 'delivery_unknown')
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.history_run(request)['status'], 'unknown')
        self.assertEqual(self.history_run(request)['prompt'], request['text'])
        self.assertEqual(self.history_run(request)['source_kind'], 'codex_background_dispatch')
        self.assertEqual(self.messages(), [], 'A dispatcher failure must not fabricate an agent message')

    def test_rebinding_or_logout_rejects_late_native_events(self):
        request = self.request()
        self.dispatch(request)
        self.commands.bindings[AGENT] = {'adapter': 'codex_background', 'session_id': 'bg-replacement'}
        self.event(request, 'finished', state='completed', text='Old session delivery')
        self.assertEqual(self.row(request)['state'], 'queued')
        self.commands.bindings[AGENT] = {'adapter': 'codex_background', 'session_id': SESSION}
        self.config['agents'] = []
        self.event(request, 'finished', state='completed', text='After logout')
        self.assertEqual(self.row(request)['state'], 'queued')
        self.assertIsNone(self.history_run(request))

    def test_waiting_approval_requires_one_time_decision_and_rejects_replay(self):
        request = self.request()
        self.event(request, 'status', waiting=True)
        prompt = self.dispatch(request)
        self.event(request, 'started', text=prompt)
        self.adapter.pending_approvals = [{'id': 'approval-native-request', 'preview': 'Read selected file', 'can_allow': True}]
        self.event(request, 'status', waiting=True)
        self.assertEqual(self.row(request)['state'], 'waiting_user')
        self.assertEqual(len(self.commands.list(AGENT)['approvals']), 1)
        self.assertEqual(self.adapter.decisions, [])
        body = {'agent_id': AGENT, 'approval_id': 'approval-native-request', 'decision': 'allow_once'}
        self.assertTrue(self.commands.decide(body)['accepted'])
        with self.assertRaises(CommandError):
            self.commands.decide(body)
        with self.assertRaises(CommandError):
            self.commands.decide({**body, 'decision': 'allow_always'})
        self.assertEqual(self.adapter.decisions, [('approval-native-request', 'allow_once')])
        self.event(request, 'status', waiting=False)
        self.assertEqual(self.row(request)['state'], 'running')


if __name__ == '__main__':
    unittest.main()
