"""Model repair contracts using temporary stores and fake native RPC only."""
import copy
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from codex_background import _Lease, durable_state
from codex_background_connections import CodexBackgroundConnections
from command_adapters import DeliveryError
from commands import Commands
from configuration import load_config, save_config
from connections import ConnectionError
from history import timestamp
import test_codex_background as native
import test_codex_background_connections as registration


class BackgroundModelRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / 'office.json'
        self.bridge = registration.FixtureBridge(self.path)
        self.bridge.commands = Commands(self.bridge.config, self.bridge.history, adapters={})
        self.probe_calls = 0
        self.probe_version = '0.145.0'
        self.connections = CodexBackgroundConnections(self.bridge, self.probe)
        self.creation = {'request_id': str(uuid.uuid4()), 'name': 'Background repair fixture',
                         'workspace': str(self.root)}
        self.joined = self.connections.create(self.creation)
        self.ident = self.joined['agent_id']
        self.sid = self.ident.split(':', 1)[1]
        self.lifecycle = Path(self.settings()['lifecycle_file'])

    def tearDown(self):
        self.bridge.commands.close()
        self.bridge.history.close()
        self.temp.cleanup()

    def probe(self, settings):
        self.probe_calls += 1
        return {'available': True, 'logged_in': True, 'version': self.probe_version,
                'model': 'configured-model', 'model_catalog_status': 'available',
                'models': [{'id': 'selected-model', 'name': 'Selected model',
                            'is_default': True, 'support': 'catalog_listed'}]}

    def settings(self):
        return load_config(self.path)['command_adapters']['codex_background']['sessions'][self.sid]

    def ready(self, **extra):
        state = json.loads(self.lifecycle.read_text())
        state.update(phase='ready', native_thread_id='native-model-repair-fixture',
                     native_target_verified=True, model='configured-model', model_provider='openai',
                     last_outcome='failed', detail_code='codex_model_unavailable', **extra)
        durable_state(self.lifecycle, state)
        return state

    def command(self, state):
        ident = str(uuid.uuid4())
        now = timestamp()
        with self.bridge.commands.db:
            self.bridge.commands.db.execute('''INSERT INTO commands
                (id,agent_id,session_id,adapter,fingerprint,preview,payload,state,
                 native_message_id,run_id,created_at,updated_at,detail_code)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (ident, self.ident, self.sid, 'codex_background', 'fixture-fingerprint',
                 'Original fixture prompt', 'Original fixture prompt' if state == 'pending' else None,
                 state, 'native-fixture-turn' if state != 'pending' else None,
                 ident if state == 'failed' else None, now, now,
                 'codex_model_unavailable' if state == 'failed' else None))
        return ident

    def request(self, **changes):
        return {'request_id': str(uuid.uuid4()), 'agent_id': self.ident,
                'model': 'selected-model', **changes}

    def assert_error(self, code, call, status=409):
        with self.assertRaises(ConnectionError) as error:
            call()
        self.assertEqual(error.exception.code, code)
        self.assertEqual(error.exception.status, status)

    def test_change_preserves_native_identity_history_and_failed_command_until_next_instruction(self):
        self.ready()
        failed = self.command('failed')
        self.bridge.history.record(self.ident, 'codex', self.joined['name'], failed, 'error',
                                   prompt='Original fixture prompt', delivery='Model unavailable')
        self.bridge.history.message(self.ident, failed, 'assistant', 'final', 'Model unavailable')
        old_lifecycle = self.lifecycle.read_bytes()
        old_config = load_config(self.path)
        old_history = self.bridge.history.db.iterdump()
        old_history = [line for line in old_history if '"runs"' in line or '"messages"' in line]
        probes = self.probe_calls
        request = self.request()
        result = self.connections.update_model(request)
        self.assertTrue(result['model_saved'])
        self.assertTrue(result['same_session'])
        self.assertTrue(result['native_thread_preserved'])
        self.assertTrue(result['pending_next_instruction'])
        self.assertFalse(result['execution_verified'])
        self.assertEqual(result['agent_id'], self.ident)
        self.assertEqual(result['request_id'], request['request_id'])
        self.assertEqual(self.settings()['model'], 'selected-model')
        self.assertEqual(self.lifecycle.read_bytes(), old_lifecycle)
        new_config = load_config(self.path)
        del new_config['command_adapters']['codex_background']['sessions'][self.sid]['model']
        self.assertEqual(new_config, old_config)
        self.assertEqual(self.bridge.commands.db.execute('SELECT state FROM commands WHERE id=?', (failed,)).fetchone()[0], 'failed')
        self.assertEqual([line for line in self.bridge.history.db.iterdump() if '"runs"' in line or '"messages"' in line], old_history)
        self.assertEqual(self.probe_calls, probes, 'Saving a model is local and makes no native/account call')
        current = self.connections.status(agent_id=self.ident)['current_agent']
        self.assertEqual(current['current_model'], 'selected-model')
        self.assertEqual(current['requested_model'], 'selected-model')
        self.assertEqual(current['actual_model'], 'configured-model')
        self.assertTrue(current['pending_model_change'])

    def test_reserved_agent_can_choose_model_without_creating_thread(self):
        before = self.lifecycle.read_bytes()
        self.connections.update_model(self.request())
        self.assertEqual(self.lifecycle.read_bytes(), before)
        self.assertIsNone(json.loads(before)['native_thread_id'])

    def test_exact_replay_is_idempotent_and_request_conflict_does_not_modify_model(self):
        self.ready()
        request = self.request()
        response = self.connections.update_model(request)
        config = self.path.read_bytes()
        self.assertEqual(self.connections.update_model(request), response)
        self.assertEqual(self.path.read_bytes(), config)
        self.assert_error('request_id_conflict', lambda: self.connections.update_model({**request, 'model': 'another-model'}))
        proof = self.connections.status(request_id=request['request_id'])['request']
        self.assertEqual(proof['state'], 'completed')
        self.assertEqual(proof['operation'], 'model_change')
        self.assertEqual(proof['response'], response)

    def test_request_ids_cannot_be_reused_between_creation_and_model_change(self):
        self.ready()
        self.assert_error('request_id_conflict', lambda: self.connections.update_model(
            self.request(request_id=self.creation['request_id'])))
        change = self.request()
        self.connections.update_model(change)
        self.assert_error('request_id_conflict', lambda: self.connections.create({
            'request_id': change['request_id'], 'name': 'Different operation', 'workspace': str(self.root)}))
        self.assertEqual(len(load_config(self.path)['agents']), 1)

    def test_foreign_native_lease_prevents_configuration_change(self):
        self.ready()
        old = self.path.read_bytes()
        lease = _Lease(self.lifecycle)
        try:
            self.assert_error('codex_session_locked', lambda: self.connections.update_model(self.request()))
        finally:
            lease.close()
        self.assertEqual(self.path.read_bytes(), old)

    def test_all_unfinished_command_states_prevent_model_change(self):
        self.ready()
        original = self.path.read_bytes()
        for state in ('pending', 'sending', 'queued', 'running', 'waiting_user', 'delivery_unknown'):
            with self.subTest(state=state):
                ident = self.command(state)
                code = 'codex_background_recovery_required' if state == 'delivery_unknown' else 'codex_model_change_busy'
                self.assert_error(code, lambda: self.connections.update_model(self.request()))
                self.assertEqual(self.path.read_bytes(), original)
                current = self.connections.status(agent_id=self.ident)['current_agent']
                self.assertFalse(current['can_change'])
                self.assertEqual(current['reason'], code)
                with self.bridge.commands.db:
                    self.bridge.commands.db.execute('DELETE FROM commands WHERE id=?', (ident,))

    def test_unknown_lifecycle_blocks_change_even_without_command_row(self):
        self.ready()
        state = json.loads(self.lifecycle.read_text()); state['phase'] = 'unknown'
        durable_state(self.lifecycle, state)
        original = self.path.read_bytes()
        self.assert_error('codex_background_recovery_required', lambda: self.connections.update_model(self.request()))
        self.assertEqual(self.path.read_bytes(), original)

    def test_lost_response_after_atomic_config_commit_recovers_without_reapplying(self):
        self.ready()
        request = self.request()
        actual_save = self.bridge.connections.save

        def commit_then_disconnect(config):
            actual_save(config)
            raise OSError('fixture lost connection after config commit')

        with patch.object(self.bridge.connections, 'save', side_effect=commit_then_disconnect):
            self.assert_error('codex_model_change_unknown', lambda: self.connections.update_model(request))
        self.assertEqual(self.settings()['model'], 'selected-model')
        self.connections = CodexBackgroundConnections(self.bridge, self.probe)
        with patch.object(self.bridge.connections, 'save', side_effect=AssertionError('Do not repeat saved change')):
            proof = self.connections.status(request_id=request['request_id'])['request']
            self.assertEqual(proof['state'], 'completed')
            self.assertEqual(self.connections.update_model(request), proof['response'])

    def test_failed_config_write_is_not_applied_by_status_or_replay(self):
        self.ready()
        before = self.path.read_bytes()
        request = self.request()
        with patch.object(self.bridge.connections, 'save', side_effect=OSError('fixture no write')):
            self.assert_error('codex_model_change_unknown', lambda: self.connections.update_model(request))
        self.assertEqual(self.path.read_bytes(), before)
        proof = self.connections.status(request_id=request['request_id'])['request']
        self.assertEqual(proof['state'], 'failed')
        self.assertEqual(proof['reason'], 'codex_model_change_not_applied')
        self.assert_error('codex_model_change_not_applied', lambda: self.connections.update_model(request))
        self.assertEqual(self.path.read_bytes(), before)

    def test_old_model_request_cannot_revert_a_newer_choice(self):
        self.ready()
        older = self.request()
        self.connections.update_model(older)
        self.connections.update_model(self.request(model='newer-model'))
        self.assert_error('codex_model_change_superseded', lambda: self.connections.update_model(older))
        self.assertEqual(self.settings()['model'], 'newer-model')

    def test_logout_revokes_model_change_receipt_without_resurrecting_agent(self):
        self.ready()
        request = self.request()
        self.connections.update_model(request)
        config = load_config(self.path)
        config['agents'] = []
        config['command_bindings'].pop(self.ident)
        config['command_adapters']['codex_background']['sessions'].pop(self.sid)
        config['logged_out_agent_ids'] = [self.ident]
        save_config(self.path, config)
        self.bridge.refresh_connections(config)
        with self.bridge.history.db:
            self.bridge.history.db.execute('INSERT INTO office_membership_events VALUES (?,?,?,?)',
                                           (uuid.uuid4().hex, self.ident, 'logged_out', timestamp()))
        self.assert_error('agent_membership_changed', lambda: self.connections.update_model(request))
        self.assertEqual(self.connections.status(request_id=request['request_id'])['request']['state'], 'revoked')
        self.assertEqual(load_config(self.path)['agents'], [])

    def test_original_registration_receipt_remains_valid_after_model_repair(self):
        self.ready()
        self.connections.update_model(self.request())
        self.assertEqual(self.connections.create(self.creation), self.joined)
        self.assertEqual(self.settings()['model'], 'selected-model')
        self.assertEqual(len(load_config(self.path)['agents']), 1)

    def test_known_rejected_implicit_default_requires_explicit_choice(self):
        self.ready(runtime_version='0.145.0')
        self.command('failed')
        status = self.connections.status()
        self.assertTrue(status['configured_model_requires_selection'])
        self.assertIn('configured-model', status['known_unavailable_models'])
        request = {'request_id': str(uuid.uuid4()), 'name': 'Second background', 'workspace': str(self.root)}
        self.assert_error('codex_model_selection_required', lambda: self.connections.create(request))
        self.assertEqual(len(load_config(self.path)['agents']), 1)
        result = self.connections.create({**request, 'model': 'selected-model'})
        config = load_config(self.path)
        self.assertEqual(config['command_adapters']['codex_background']['sessions'][result['agent_id'].split(':', 1)[1]]['model'], 'selected-model')

    def test_runtime_upgrade_does_not_treat_old_model_rejection_as_current_incompatibility(self):
        self.ready(runtime_version='0.145.0')
        self.command('failed')
        self.probe_version = '0.159.0'
        self.connections._probe_cache = None
        status = self.connections.status()
        self.assertFalse(status['configured_model_requires_selection'])
        self.assertTrue(status['configured_model_warning'])
        self.assertNotIn('configured-model', status['known_unavailable_models'])
        request = {'request_id': str(uuid.uuid4()), 'name': 'Upgraded runtime fixture', 'workspace': str(self.root)}
        result = self.connections.create(request)
        settings = load_config(self.path)['command_adapters']['codex_background']['sessions'][result['agent_id'].split(':', 1)[1]]
        self.assertNotIn('model', settings, 'A newer runtime must still honor its configured model')

    def test_legacy_model_rejection_without_runtime_version_is_warning_only(self):
        self.ready()
        self.command('failed')
        status = self.connections.status()
        self.assertTrue(status['configured_model_warning'])
        self.assertFalse(status['configured_model_requires_selection'])
        request = {'request_id': str(uuid.uuid4()), 'name': 'Legacy warning fixture', 'workspace': str(self.root)}
        self.connections.create(request)
        self.assertEqual(len(load_config(self.path)['agents']), 2)

    def test_failure_warning_keeps_original_model_and_runtime_after_new_resume(self):
        self.ready(runtime_version='0.159.0', model_error_model='configured-model',
                   model_error_runtime_version='0.145.0')
        state = json.loads(self.lifecycle.read_text())
        state['model'] = 'new-model'
        durable_state(self.lifecycle, state)
        self.probe_version = '0.159.0'
        self.connections._probe_cache = None
        status = self.connections.status()
        self.assertEqual(status['known_rejected_models'], [
            {'model': 'configured-model', 'runtime_version': '0.145.0',
             'current_runtime_match': False}])
        self.assertTrue(status['configured_model_warning'])
        self.assertFalse(status['configured_model_requires_selection'])
        self.assertEqual(status['known_unavailable_models'], [])

    def test_probe_cache_invalidates_when_selected_runtime_identity_changes(self):
        self.connections._default_runtime_probe = True
        self.connections._probe_cache = None
        calls = self.probe_calls
        with patch('codex_rpc.resolve_binary', return_value='/fixture/codex'), \
                patch('codex_rpc.runtime_info') as runtime:
            runtime.return_value = {'identity': ('/fixture/codex', 1, 100)}
            self.connections._probe()
            self.connections._probe()
            self.assertEqual(self.probe_calls, calls + 1)
            runtime.return_value = {'identity': ('/fixture/codex', 2, 110)}
            self.connections._probe()
            self.assertEqual(self.probe_calls, calls + 2)

    def test_invalid_model_request_cannot_mutate_config(self):
        self.ready()
        before = self.path.read_bytes()
        bad = [({'model': ''}, 'codex_model_invalid', 400),
               ({'model': 'model with spaces'}, 'codex_model_invalid', 400),
               ({'model': None}, 'codex_model_invalid', 400),
               ({'request_id': 'not-a-uuid'}, 'invalid_request_id', 400),
               ({'surprise': True}, 'invalid_connection', 400),
               ({'agent_id': 'cursor:foreign'}, 'codex_background_agent_not_selected', 404)]
        for update, code, status in bad:
            with self.subTest(update=update):
                self.assert_error(code, lambda: self.connections.update_model(self.request(**update)), status)
                self.assertEqual(self.path.read_bytes(), before)


class BackgroundModelRuntimeTests(unittest.TestCase):
    setUp = native.BackgroundTests.setUp
    tearDown = native.BackgroundTests.tearDown
    new_adapter = native.BackgroundTests.new_adapter
    submit = native.BackgroundTests.submit
    state = native.BackgroundTests.state
    finish = native.BackgroundTests.finish

    @contextmanager
    def fake_runtime(self):
        runtime = {'binary': '/fixture/codex-v1', 'mtime': 1}
        with patch('codex_background.resolve_binary', side_effect=lambda _: runtime['binary']), \
                patch('codex_background.runtime_info', side_effect=lambda binary: {
                    'source': 'cli', 'identity': (binary, 1, 2, runtime['mtime'], 100)}):
            yield runtime

    def wait_idle_projection(self):
        native.until(lambda: not self.adapter.workers[self.sid].active
                     and not self.adapter.workers[self.sid].pending_events)

    def assert_resumed_identity(self, client):
        resumes = [params for name, params in client.calls if name == 'thread/resume']
        self.assertEqual(len(resumes), 1)
        self.assertEqual(resumes[0]['threadId'], 'native-thread')
        self.assertEqual(resumes[0]['model'], 'configured-model')
        self.assertEqual(self.native_creates, 1)

    def test_idle_runtime_path_and_file_upgrade_resume_same_native_thread(self):
        with self.fake_runtime() as runtime:
            self.submit(); self.finish(); self.wait_idle_projection()
            old = self.clients[-1]
            runtime['binary'] = '/fixture/codex-v2'
            self.submit(); self.finish(turn='turn-2'); self.wait_idle_projection()
            self.assertTrue(old.closed.is_set())
            self.assertEqual(len(self.clients), 2)
            self.assert_resumed_identity(self.clients[-1])
            old = self.clients[-1]
            runtime['mtime'] = 2
            self.submit(); self.finish(turn='turn-3'); self.wait_idle_projection()
            self.assertTrue(old.closed.is_set())
            self.assertEqual(len(self.clients), 3)
            self.assert_resumed_identity(self.clients[-1])

    def test_unchanged_runtime_reuses_owned_idle_client(self):
        with self.fake_runtime():
            self.submit(); self.finish(); self.wait_idle_projection()
            original = self.clients[-1]
            self.submit(); self.finish(turn='turn-2'); self.wait_idle_projection()
            self.assertEqual(self.clients, [original])
            self.assertFalse(original.closed.is_set())
            self.assert_resumed_identity(original)

    def test_runtime_upgrade_does_not_close_active_native_turn(self):
        with self.fake_runtime() as runtime:
            self.submit()
            original = self.clients[-1]
            runtime['binary'] = '/fixture/codex-v2'
            self.assertFalse(self.adapter.can_dispatch(self.sid))
            with self.assertRaises(DeliveryError) as caught:
                self.submit()
            self.assertEqual(caught.exception.code, 'codex_background_busy')
            self.assertEqual(self.clients, [original])
            self.assertFalse(original.closed.is_set())
            self.assertEqual(self.native_turns, 1)
            self.finish(); self.wait_idle_projection()
            self.submit(); self.finish(turn='turn-2'); self.wait_idle_projection()
            self.assertTrue(original.closed.is_set())
            self.assert_resumed_identity(self.clients[-1])

    def test_pending_terminal_projection_blocks_dispatch_and_runtime_replacement(self):
        entered, release = threading.Event(), threading.Event()

        def emit(agent, event):
            if event['type'] == 'finished' and event['native_run_id'] == 'turn-1':
                entered.set()
                if not release.wait(3):
                    raise AssertionError('bounded fixture projection was not released')
            self.events.append(event)

        self.adapter.emit = emit
        with self.fake_runtime() as runtime:
            self.submit()
            original = self.clients[-1]
            original.complete(items=[{'type': 'agentMessage', 'id': 'final', 'text': 'done'}])
            try:
                self.assertTrue(entered.wait(2))
                native.until(lambda: self.adapter.workers[self.sid].active is None)
                self.assertGreater(self.adapter.workers[self.sid].pending_events, 0)
                runtime['mtime'] = 2
                self.assertFalse(self.adapter.can_dispatch(self.sid))
                with self.assertRaises(DeliveryError) as caught:
                    self.submit()
                self.assertEqual(caught.exception.code, 'codex_background_busy')
                self.assertEqual(self.clients, [original])
                self.assertFalse(original.closed.is_set())
                self.assertEqual(self.native_turns, 1)
            finally:
                release.set()
            self.wait_idle_projection()
            self.assertEqual(len([event for event in self.events if event['type'] == 'finished']), 1)
            self.submit(); self.finish(turn='turn-2'); self.wait_idle_projection()
            self.assertTrue(original.closed.is_set())
            self.assert_resumed_identity(self.clients[-1])

    def test_explicit_model_repair_resumes_same_thread_and_preserves_previous_actual_until_native_reply(self):
        self.submit(); self.finish()
        original_state = self.state()
        config = copy.deepcopy(self.config)
        config['command_adapters']['codex_background']['sessions'][self.sid]['model'] = 'selected-model'
        self.adapter.sync_config(config)
        self.assertEqual(self.state(), original_state)
        self.submit(); self.finish(turn='turn-2')
        resumes = [params for name, params in self.clients[-1].calls if name == 'thread/resume']
        self.assertEqual(resumes[0]['model'], 'selected-model')
        self.assertEqual(resumes[0]['threadId'], 'native-thread')
        self.assertEqual(self.native_creates, 1)
        self.assertEqual(self.state()['model'], 'selected-model')

    def test_native_model_mismatch_blocks_prompt_instead_of_silent_fallback(self):
        self.submit(); self.finish()
        config = copy.deepcopy(self.config)
        config['command_adapters']['codex_background']['sessions'][self.sid]['model'] = 'selected-model'
        self.adapter.sync_config(config)
        actual_call = native.FakeRpc.call

        def return_wrong_model(client, method, params, timeout=20):
            result = actual_call(client, method, params, timeout)
            if method == 'thread/resume':
                result['model'] = 'unexpected-model'
            return result

        with patch.object(native.FakeRpc, 'call', return_wrong_model):
            with self.assertRaises(DeliveryError) as caught:
                self.submit()
        self.assertEqual(caught.exception.code, 'codex_background_model_mismatch')
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(self.native_turns, 1)
        self.assertEqual(self.state()['phase'], 'unknown')
        self.assertEqual(self.state()['model'], 'configured-model')


if __name__ == '__main__':
    unittest.main()
