"""Scope contracts and queue safety, using only local synthetic events."""
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from commands import Commands
from event_ingest import EventConflict, ingest
from history import HistoryStore
from public_sync import capability


class PublicSyncCapabilityTests(unittest.TestCase):
    def definition(self, provider, driver, source='fixture'):
        return {'id': provider + ':' + source, 'provider': provider, 'driver': driver,
                'source_id': source, 'name': 'Fixture'}

    def describe(self, definition, binding=None, observer=None):
        return capability({'agents': [definition]}, definition['id'], binding, observer)

    def test_native_codex_target_covers_selected_conversation_without_outbound_binding(self):
        value = self.describe(self.definition('codex', 'codex'))
        self.assertEqual((value['mode'], value['scope']), ('native_observer', 'selected_conversation'))
        self.assertTrue(value['manual_app_automatic'])
        push = self.definition('codex', 'push')
        self.assertFalse(self.describe(push)['automatic'])
        native = self.describe(push, {'adapter': 'codex_app', 'session_id': 'native-selected'})
        self.assertTrue(native['manual_app_automatic'])

    def test_cursor_requires_current_scoped_observer_not_merely_a_pending_install(self):
        definition = self.definition('cursor', 'push')
        for observer in [None, lambda _: False, lambda _: None, lambda _: {'active': True, 'pending': True}]:
            value = self.describe(definition, observer=observer)
            self.assertEqual(value['state'], 'setup_required')
            self.assertFalse(value['configured'])
            self.assertFalse(value['automatic'])
        def broken(_):
            raise RuntimeError('private fixture detail must not escape')
        value = self.describe(definition, observer=broken)
        self.assertEqual(value['state'], 'setup_required')
        self.assertNotIn('private fixture detail', json.dumps(value))
        calls = []
        value = self.describe(definition, observer=lambda ident: calls.append(ident) or True)
        self.assertEqual(calls, [definition['id']])
        self.assertEqual(value['mode'], 'native_hook')
        self.assertTrue(value['manual_app_automatic'])

    def test_acp_and_cloud_observers_do_not_claim_arbitrary_native_app_turns(self):
        for provider, driver, adapter in [('hermes', 'hermes', 'hermes_acp'),
                                           ('cursor', 'cursor', 'cursor_cloud')]:
            value = self.describe(self.definition(provider, driver), {'adapter': adapter, 'session_id': 'fixture'})
            self.assertEqual(value['scope'], 'office_dispatched_runs')
            self.assertEqual(value['state'], 'limited')
            self.assertTrue(value['automatic'])
            self.assertFalse(value['manual_app_automatic'])

    def test_grok_webhook_and_generic_reporting_remain_cooperative(self):
        for provider, binding in [('grok_bot', {'adapter': 'grok_webhook', 'session_id': 'fixture'}),
                                   ('generic', None)]:
            value = self.describe(self.definition(provider, 'push'), binding)
            self.assertEqual((value['mode'], value['scope']), ('cooperative', 'reported_runs'))
            self.assertTrue(value['configured'])
            self.assertFalse(value['automatic'])
            self.assertFalse(value['manual_app_automatic'])
            self.assertTrue(value['public_only'])

    def test_retired_or_unselected_identity_has_no_observation_capability(self):
        calls = []
        value = capability({'agents': []}, 'cursor:retired', {'adapter': 'cursor_cloud'},
                           lambda ident: calls.append(ident) or True)
        self.assertEqual(value['state'], 'unsupported')
        self.assertEqual(value['reason'], 'agent_not_selected')
        self.assertFalse(value['automatic'])
        self.assertEqual(calls, [])


class FixtureAdapter:
    mode, serial, same_session, reason = 'grok_webhook', True, False, None
    def __init__(self):
        self.calls = []
        self.ready = True
    def available(self, _):
        return self.ready
    def submit(self, session, text):
        self.calls.append((session, text))
        return {'attention_code': 'awaiting_agent_callback'}


class ReportedWorkDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.history = HistoryStore(self.root / 'history.sqlite3')
        self.agent = {'id': 'grok_bot:fixture', 'provider': 'grok_bot', 'driver': 'push',
                      'source_id': 'fixture', 'name': 'Fixture', 'token_sha256': 'a' * 64}
        self.config = {'data_dir': str(self.root), 'topic': 'fixture-private-owner-key', 'agents': [self.agent],
                       'command_bindings': {self.agent['id']: {'adapter': 'grok_webhook', 'session_id': 'fixture'}}}
        self.adapter = FixtureAdapter()
        self.commands = Commands(self.config, self.history, adapters={'grok_webhook': self.adapter})

    def tearDown(self):
        self.commands.close()
        self.history.close()
        self.temp.cleanup()

    def event(self, run, kind, text='', **extra):
        return ingest(self.history, self.agent, {'event_id': str(uuid.uuid4()), 'run_id': run,
            'source_id': self.agent['source_id'], 'type': kind, 'text': text, **extra})

    def request(self):
        request = {'request_id': str(uuid.uuid4()), 'agent_id': self.agent['id'], 'text': 'Synthetic queued task'}
        self.commands.create(request)
        return request

    def row(self, request):
        return dict(self.commands.db.execute('SELECT * FROM commands WHERE id=?', (request['request_id'],)).fetchone())

    def test_manual_reported_work_holds_pending_and_survives_restart_without_dispatch(self):
        self.event('manual-run', 'turn_started', 'Synthetic direct App task')
        request = self.request()
        self.commands.tick()
        row = self.row(request)
        self.assertEqual(row['state'], 'pending')
        self.assertEqual(row['detail_code'], 'waiting_for_reported_work')
        self.assertIsNotNone(row['payload'])
        self.assertEqual(self.adapter.calls, [])
        self.commands.close()
        self.commands = Commands(self.config, self.history, adapters={'grok_webhook': self.adapter})
        self.commands.tick()
        self.assertEqual(self.row(request)['state'], 'pending')
        self.assertEqual(self.adapter.calls, [])
        self.event('manual-run', 'turn_completed', 'Synthetic manual delivery')
        self.commands.tick()
        self.commands.tick()
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.row(request)['state'], 'queued')
        self.assertEqual(self.row(request)['detail_code'], 'awaiting_agent_callback')
        self.assertTrue(self.commands.create(request)['duplicate'])
        self.assertEqual(len(self.adapter.calls), 1)

    def test_manual_work_remains_guard_after_an_office_command_completes(self):
        first = self.request()
        self.commands.tick()
        self.commands.observe_prompt(self.agent['id'], first['request_id'], self.adapter.calls[0][1])
        self.event(first['request_id'], 'turn_started', 'Synthetic Office task')
        self.event('manual-after-office', 'turn_started', 'Synthetic manual task')
        with self.assertRaises(EventConflict):
            self.event(first['request_id'], 'turn_completed', 'Old terminal cannot replace current manual work')
        # Even if separately authenticated command completion/reconciliation
        # releases the Office serial guard, the current manual run still blocks.
        self.commands.observe_run(self.agent['id'], first['request_id'], 'done')
        second = self.request()
        self.commands.tick()
        self.assertEqual(self.row(first)['state'], 'completed')
        self.assertEqual(self.row(second)['state'], 'pending')
        self.assertEqual(len(self.adapter.calls), 1)
        self.event('manual-after-office', 'turn_failed', 'Synthetic task failed')
        self.commands.tick()
        self.assertEqual(len(self.adapter.calls), 2)

    def test_existing_office_serial_guard_still_blocks_an_unconfirmed_prior_command(self):
        first = self.request()
        self.commands.tick()
        self.commands.observe_prompt(self.agent['id'], first['request_id'], self.adapter.calls[0][1])
        self.event(first['request_id'], 'turn_started', 'Synthetic Office task')
        second = self.request()
        self.commands.tick()
        self.assertEqual(self.row(second)['state'], 'pending')
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertIsNone(self.row(second)['detail_code'])

    def test_unrelated_terminal_does_not_release_a_working_manual_run(self):
        self.event('manual-current', 'turn_started', 'Synthetic manual task')
        request = self.request()
        self.commands.observe_run(self.agent['id'], 'unrelated', 'done')
        with self.assertRaises(EventConflict):
            self.event('unrelated', 'turn_completed', 'Unrelated terminal')
        for _ in range(3):
            self.commands.tick()
        self.assertEqual(self.row(request)['state'], 'pending')
        self.assertEqual(self.adapter.calls, [])

    def test_finished_manual_work_clears_its_wait_reason_even_if_platform_is_unavailable(self):
        self.event('manual-current', 'turn_started', 'Synthetic manual task')
        request = self.request()
        self.commands.tick()
        self.assertEqual(self.row(request)['detail_code'], 'waiting_for_reported_work')
        self.event('manual-current', 'turn_cancelled', 'Synthetic cancellation')
        self.adapter.ready = False
        self.commands.tick()
        self.assertEqual(self.row(request)['state'], 'pending')
        self.assertIsNone(self.row(request)['detail_code'])
        self.assertEqual(self.adapter.calls, [])

    def test_outbound_disabled_does_not_disable_public_observation_scope(self):
        self.config['agents'] = [{'id': 'codex:selected', 'provider': 'codex', 'driver': 'codex',
                                  'source_id': 'selected', 'name': 'Fixture'}]
        self.config['disabled_command_agents'] = ['codex:selected']
        value = self.commands.capability('codex:selected')
        self.assertFalse(value['can_send'])
        self.assertTrue(value['public_sync']['manual_app_automatic'])
        self.config['agents'] = []
        self.assertEqual(self.commands.capability('codex:selected')['public_sync']['state'], 'unsupported')


if __name__ == '__main__':
    unittest.main()
