"""Synthetic registration and rollback contracts; no model or App is contacted."""
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from codex_background_connections import CodexBackgroundConnections
from codex_background_rollback import _durable_json, park, parked_configuration
from configuration import codex_targets, initialize, load_config, save_config
from connections import ConnectionError, Connections
from history import HistoryStore
from public_sync import capability
from workspaces import Workspaces


class FixtureBridge:
    def __init__(self, path):
        self.config = initialize(path, 48417)
        self.history = HistoryStore(path.parent / 'history.sqlite3')
        self.commands = SimpleNamespace(lock=threading.RLock())
        self.workspaces = Workspaces(self.history)
        self.connections = Connections.__new__(Connections)
        self.connections.bridge = self
        self.refreshes = 0
        with self.history.db:
            self.history.db.execute('CREATE TABLE office_membership_events (event_id TEXT PRIMARY KEY,agent_id TEXT,kind TEXT,at TEXT)')

    def refresh_connections(self, config):
        self.config = config
        self.refreshes += 1


class BackgroundRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / 'config.json'
        self.bridge = FixtureBridge(self.path)
        self.probes = []
        self.probe_result = {'available': True, 'logged_in': True, 'version': '0.145.0', 'model': 'configured-model'}
        self.connections = CodexBackgroundConnections(self.bridge, self.probe)

    def tearDown(self):
        self.bridge.history.close()
        self.temp.cleanup()

    def probe(self, settings):
        self.probes.append(copy.deepcopy(settings))
        return self.probe_result

    def request(self, **extra):
        return {'request_id': str(uuid.uuid4()), 'name': 'Background fixture', 'workspace': str(self.root), **extra}

    def create(self, **extra):
        data = self.request(**extra)
        return data, self.connections.create(data)

    def config_session(self, response):
        config = load_config(self.path)
        return config['command_adapters']['codex_background']['sessions'][response['agent_id'].split(':', 1)[1]]

    def assertError(self, expected, callback):
        with self.assertRaises(ConnectionError) as error:
            callback()
        self.assertEqual(error.exception.code, expected)

    def test_registration_reserves_only_private_lifecycle_and_preserves_default_model(self):
        data, response = self.create()
        session = self.config_session(response)
        self.assertNotIn('model', session, 'The CLI configured model must remain its default')
        self.assertEqual(session['sandbox'], 'read-only')
        self.assertEqual(session['approval_policy'], 'on-request')
        lifecycle = Path(session['lifecycle_file'])
        state = json.loads(lifecycle.read_text())
        self.assertEqual(state['phase'], 'reserved')
        self.assertIsNone(state['native_thread_id'])
        self.assertIsNone(state['native_turn_id'])
        self.assertEqual(state['session_id'], 'bg-' + data['request_id'])
        self.assertFalse(response['native_target_verified'])
        self.assertFalse(response['execution_verified'])
        self.assertEqual(response['registration_state'], 'pending_first_instruction')
        self.assertEqual(response['conversation_scope'], 'office_owned_session')
        self.assertEqual(lifecycle.stat().st_mode & 0o777, 0o600)
        self.assertEqual(lifecycle.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(len(self.probes), 1, 'Registration only calls the metadata probe')

    def test_explicit_model_and_selected_workspace_are_preserved(self):
        workspace = self.root / 'new workspace'
        _, response = self.create(workspace=str(workspace), create_folder=True, model='chosen-model')
        self.assertTrue(workspace.is_dir())
        self.assertEqual(self.config_session(response)['model'], 'chosen-model')
        grouping = self.bridge.workspaces.for_agent(response['agent_id'])
        self.assertEqual(grouping['name'], 'new workspace')
        self.assertEqual(grouping['mode'], 'manual')

    def test_registration_is_idempotent_and_status_recovers_exact_receipt(self):
        data, response = self.create()
        before = Path(self.config_session(response)['lifecycle_file']).read_bytes()
        self.assertEqual(self.connections.create(data), response)
        proof = self.connections.status(data['request_id'])['request']
        self.assertEqual(proof['state'], 'completed')
        self.assertEqual(proof['response'], response)
        self.assertEqual(Path(self.config_session(response)['lifecycle_file']).read_bytes(), before)
        self.assertEqual(len(load_config(self.path)['agents']), 1)
        self.assertError('request_id_conflict', lambda: self.connections.create({**data, 'name': 'Another name'}))

    def test_completed_receipt_does_not_require_current_login(self):
        data, response = self.create()
        self.connections = CodexBackgroundConnections(self.bridge, lambda _: {'available': False})
        self.assertEqual(self.connections.create(data), response)

    def test_status_probe_is_cached_and_does_not_forward_auth_metadata(self):
        self.probe_result.update(email='secret@example.test', api_key='PRIVATE_AUTH_MARKER', account={'accessToken': 'PRIVATE_AUTH_MARKER'})
        for _ in range(5):
            value = self.connections.status()
        self.assertEqual(len(self.probes), 1)
        self.assertEqual(value['model'], 'configured-model')
        self.assertNotIn('PRIVATE_AUTH_MARKER', json.dumps(value))
        self.assertNotIn('secret@example', json.dumps(value))
        self.assertNotIn(self.bridge.config['topic'], json.dumps(value))
        self.connections._probe_cache = None
        self.connections.status()
        self.assertEqual(len(self.probes), 2)

    def test_catalog_metadata_never_implicitly_replaces_configured_model(self):
        self.probe_result.update(model='configured-unlisted-model', model_catalog_status='available', models=[
            {'id': 'listed-default-model', 'name': 'Listed model', 'is_default': True, 'support': 'catalog_listed'}])
        status = self.connections.status()
        self.assertEqual(status['model'], 'configured-unlisted-model')
        self.assertEqual(status['configured_model'], 'configured-unlisted-model')
        self.assertFalse(status['configured_model_listed'])
        self.assertEqual(status['default_model'], 'listed-default-model')
        _, inherited = self.create()
        self.assertNotIn('model', self.config_session(inherited), 'Catalog default must not silently override CLI configuration')
        _, explicit = self.create(name='Explicit model fixture', model='listed-default-model')
        self.assertEqual(self.config_session(explicit)['model'], 'listed-default-model')
        self.assertNotIn('model', self.config_session(inherited), 'Selecting a new session model must not modify an existing session')
        self.assertEqual(self.connections.status()['configured_model'], 'configured-unlisted-model')

    def test_catalog_metadata_is_bounded_validated_and_excludes_auth_details(self):
        self.probe_result.update(model_catalog_status='available', models=[
            {'id': 'safe-model', 'name': 'Safe model', 'is_default': True,
             'api_key': 'PRIVATE_CATALOG_MARKER', 'account': {'secret': 'PRIVATE_CATALOG_MARKER'},
             'description': 'PRIVATE_CATALOG_MARKER', 'support': 'execution_verified'},
            {'id': 'safe-model', 'name': 'Duplicate', 'is_default': True},
            {'id': 'https://example.test/?key=PRIVATE_CATALOG_MARKER', 'name': 'Invalid identifier'},
            {'id': 'second-model', 'name': 'Bad\nname', 'is_default': 'true'},
            {'id': {'token': 'PRIVATE_CATALOG_MARKER'}, 'name': 'Invalid type'},
            'PRIVATE_CATALOG_MARKER'])
        value = self.connections.status()
        self.assertEqual(value['models'], [
            {'id': 'safe-model', 'name': 'Safe model', 'is_default': True, 'support': 'catalog_listed'},
            {'id': 'second-model', 'name': 'second-model', 'is_default': False, 'support': 'catalog_listed'}])
        self.assertNotIn('PRIVATE_CATALOG_MARKER', json.dumps(value))
        self.assertNotIn('execution_verified', json.dumps(value))
        value['models'][0]['name'] = 'Caller mutation'
        self.assertEqual(self.connections.status()['models'][0]['name'], 'Safe model')
        self.probe_result['models'] = [{'id': 'fixture-' + str(n), 'name': 'Fixture', 'is_default': True} for n in range(110)]
        self.connections._probe_cache = None
        value = self.connections.status()
        self.assertEqual(len(value['models']), 100)
        self.assertIsNone(value['default_model'], 'Conflicting default flags must not select a model')

    def test_unavailable_model_catalog_does_not_turn_login_into_runtime_failure(self):
        self.probe_result.update(model_catalog_status='unavailable', models=[], configured_model_listed=False)
        value = self.connections.status()
        self.assertTrue(value['available'])
        self.assertTrue(value['logged_in'])
        self.assertEqual(value['model'], 'configured-model')
        self.assertEqual(value['models'], [])
        self.assertEqual(value['model_catalog_status'], 'unavailable')
        self.assertIsNone(value['configured_model_listed'])
        self.assertIsNone(value['default_model'])

    def test_probe_failure_is_safe_metadata_and_never_persists_partial_member(self):
        def broken(_):
            raise OSError('PRIVATE_AUTH_MARKER')
        self.connections.runtime_probe = broken
        self.assertError('codex_probe_failed', lambda: self.connections.create(self.request()))
        self.assertNotIn('PRIVATE_AUTH_MARKER', json.dumps(self.connections.status()))
        self.assertFalse((self.root / 'codex-background').exists())
        self.assertEqual(load_config(self.path)['agents'], [])

    def test_unavailable_or_unconfirmed_login_does_not_create_requested_folder(self):
        for result, error in [({'available': False}, 'codex_runtime_required'),
                              ({'available': False, 'logged_in': False, 'reason': 'codex_login_required'}, 'codex_login_required'),
                              ({'available': False, 'reason': 'codex_rpc_timeout'}, 'codex_rpc_timeout'),
                              ({'available': True, 'logged_in': False}, 'codex_login_required'),
                              ({'available': True}, 'codex_login_unknown')]:
            self.connections = CodexBackgroundConnections(self.bridge, lambda _, result=result: result)
            folder = self.root / ('folder-' + uuid.uuid4().hex)
            self.assertError(error, lambda: self.connections.create(self.request(workspace=str(folder), create_folder=True)))
            self.assertFalse(folder.exists())

    def test_inflight_creation_status_and_same_uuid_do_not_duplicate(self):
        data = self.request()
        entered, release = threading.Event(), threading.Event()
        from codex_background import durable_state
        outcome = []
        def reserved(path, value):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('fixture release timed out')
            durable_state(path, value)
        with patch('codex_background.durable_state', side_effect=reserved):
            worker = threading.Thread(target=lambda: outcome.append(self.connections.create(data)))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                proof = self.connections.status(data['request_id'])['request']
                self.assertEqual(proof['state'], 'pending')
                self.assertError('codex_creation_pending', lambda: self.connections.create(data))
            finally:
                release.set()
                worker.join(3)
        self.assertEqual(len(outcome), 1)
        self.assertEqual(len(load_config(self.path)['agents']), 1)

    def test_invalid_request_inputs_and_policy_broadening_are_rejected(self):
        for ident in [None, '', str(uuid.uuid4()).upper(), uuid.uuid4().hex, '../escape', 1]:
            self.assertError('invalid_request_id', lambda ident=ident: self.connections.create(self.request(request_id=ident)))
        for extra in [{'sandbox': 'danger-full-access'}, {'approval_policy': 'never'},
                      {'api_key': 'PRIVATE'}, {'binary': '/bin/sh'}, {'session_id': str(uuid.uuid4())}]:
            self.assertError('invalid_connection', lambda extra=extra: self.connections.create(self.request(**extra)))
        for model in [None, '', 'model\nsecret', 'https://example.test', {}, 'a' * 129]:
            self.assertError('codex_model_invalid', lambda model=model: self.connections.create(self.request(model=model)))
        for workspace in ['relative', '', str(self.root / 'absent'), str(self.root / 'absent' / 'child')]:
            self.assertError('invalid_workspace', lambda workspace=workspace: self.connections.create(self.request(workspace=workspace)))
        self.assertError('invalid_workspace', lambda: self.connections.create(self.request(create_folder=1)))
        self.assertError('invalid_connection', lambda: self.connections.create(self.request(name='\n')))

    def test_unknown_request_and_invalid_status_id_are_distinguished(self):
        request_id = str(uuid.uuid4())
        self.assertEqual(self.connections.status(request_id)['request'], {'request_id': request_id, 'state': 'not_found'})
        self.assertError('invalid_request_id', lambda: self.connections.status('wrong'))

    def test_lost_completion_receipt_reconciles_saved_config_after_restart(self):
        data = self.request()
        with patch.object(self.connections, '_finish', side_effect=OSError('simulated receipt write failure')):
            self.assertError('codex_creation_unknown', lambda: self.connections.create(data))
        saved = load_config(self.path)
        self.assertEqual(len(saved['agents']), 1)
        sid = saved['agents'][0]['source_id']
        before = (self.root / 'codex-background' / (sid + '.json')).read_bytes()
        restarted = CodexBackgroundConnections(self.bridge, self.probe)
        status = restarted.status(data['request_id'])['request']
        self.assertEqual(status['state'], 'completed')
        self.assertEqual(restarted.create(data), status['response'])
        self.assertEqual((self.root / 'codex-background' / (sid + '.json')).read_bytes(), before)

    def test_uncertain_reservation_does_not_repeat_after_restart(self):
        data = self.request()
        with patch.object(self.bridge.connections, 'save', side_effect=OSError('simulated precommit write failure')):
            self.assertError('codex_creation_unknown', lambda: self.connections.create(data))
        restarted = CodexBackgroundConnections(self.bridge, self.probe)
        self.assertEqual(restarted.status(data['request_id'])['request']['state'], 'unknown')
        self.assertError('codex_creation_unknown', lambda: restarted.create(data))
        self.assertEqual(load_config(self.path)['agents'], [])
        self.assertEqual(len(list((self.root / 'codex-background').iterdir())), 1)

    def test_logout_and_same_identity_rejoin_do_not_reactivate_old_receipt(self):
        data, response = self.create()
        original = load_config(self.path)
        parked, _ = parked_configuration(original)
        save_config(self.path, parked)
        self.bridge.refresh_connections(load_config(self.path))
        with self.bridge.history.db:
            self.bridge.history.db.execute('INSERT INTO office_membership_events VALUES (?,?,?,?)',
                (uuid.uuid4().hex, response['agent_id'], 'logged_out', 'fixture'))
        # Even an exact config-level rejoin cannot erase the logout generation.
        save_config(self.path, original)
        self.bridge.refresh_connections(load_config(self.path))
        proof = self.connections.status(data['request_id'])['request']
        self.assertEqual(proof['state'], 'revoked')
        self.assertEqual(proof['reason'], 'agent_membership_changed')
        self.assertError('agent_membership_changed', lambda: self.connections.create(data))

    def test_name_collision_preserves_existing_agent_and_has_no_new_lifecycle(self):
        self.create()
        self.assertError('name_in_use', lambda: self.connections.create(self.request()))
        self.assertEqual(len(list((self.root / 'codex-background').iterdir())), 1)

    def test_unrelated_app_codex_targets_and_public_sync_are_unchanged(self):
        native_sid, alias_sid = str(uuid.uuid4()), str(uuid.uuid4())
        config = load_config(self.path)
        native = {'provider': 'codex', 'source_id': native_sid, 'driver': 'codex', 'name': 'Native App fixture'}
        alias = {'provider': 'codex', 'source_id': 'inv-fixture', 'driver': 'push', 'name': 'Invited App fixture', 'token_sha256': 'a' * 64}
        config['agents'].extend([native, alias])
        config['command_bindings'] = {'codex:inv-fixture': {'adapter': 'codex_app', 'session_id': alias_sid}}
        save_config(self.path, config)
        self.bridge.refresh_connections(load_config(self.path))
        before = codex_targets(self.bridge.config)
        _, response = self.create()
        config = load_config(self.path)
        self.assertEqual(codex_targets(config), before)
        public = capability(config, response['agent_id'], config['command_bindings'][response['agent_id']])
        self.assertEqual(public['mode'], 'native_rpc_observer')
        self.assertEqual(public['scope'], 'office_dispatched_runs')
        self.assertFalse(public['manual_app_automatic'])
        self.assertTrue(public['public_only'])
        self.assertTrue(capability(config, 'codex:' + native_sid)['manual_app_automatic'])

    def test_configuration_rejects_wrong_drivers_ids_policies_and_private_paths(self):
        _, response = self.create()
        valid = load_config(self.path)
        sid = response['agent_id'].split(':', 1)[1]
        def invalid(mutator):
            value = copy.deepcopy(valid)
            mutator(value)
            save_config(self.path, value)
            with self.assertRaises(ValueError):
                load_config(self.path)
        invalid(lambda c: c['agents'][0].update(provider='cursor'))
        invalid(lambda c: c['agents'][0].update(source_id='not-a-background-id'))
        invalid(lambda c: c['agents'][0].update(driver='codex'))
        invalid(lambda c: c['command_bindings'][response['agent_id']].update(adapter='codex_app'))
        invalid(lambda c: c['command_adapters']['codex_background']['sessions'][sid].update(sandbox='danger-full-access'))
        invalid(lambda c: c['command_adapters']['codex_background']['sessions'][sid].update(approval_policy='never'))
        invalid(lambda c: c['command_adapters']['codex_background']['sessions'][sid].update(lifecycle_file=str(self.root / 'outside.json')))
        invalid(lambda c: c['command_adapters']['codex_background']['sessions'][sid].update(credential_file='/tmp/auth.json'))
        invalid(lambda c: c['command_adapters']['codex_background']['sessions'][sid].update(cwd='relative'))
        invalid(lambda c: c['command_adapters']['codex_background'].update(binary='codex'))
        save_config(self.path, valid)

    def test_private_directory_symlink_is_rejected_without_touching_target(self):
        target = self.root / 'other-dir'
        target.mkdir()
        (self.root / 'codex-background').symlink_to(target, target_is_directory=True)
        self.assertError('codex_private_state_conflict', lambda: self.connections.create(self.request()))
        self.assertEqual(list(target.iterdir()), [])

    def test_rollback_defaults_dry_run_and_preserves_history_and_unrelated_updates(self):
        _, response = self.create()
        config = load_config(self.path)
        other_sid = str(uuid.uuid4())
        other = {'provider': 'codex', 'source_id': other_sid, 'driver': 'codex', 'name': 'Existing App'}
        config['agents'].append(other)
        config['command_bindings']['codex:' + other_sid] = {'adapter': 'codex_app', 'session_id': other_sid}
        config['disabled_command_agents'] = [response['agent_id'], 'codex:' + other_sid]
        config['brand'] = 'A newer user edit'
        config['graphics_fixture'] = {'fps': 30, 'idle_saving': True}
        save_config(self.path, config)
        self.bridge.history.message(response['agent_id'], 'run-fixture', 'assistant', 'final', 'Keep this delivery')
        history_before = self.bridge.history.path.read_bytes()
        lifecycle = Path(self.config_session(response)['lifecycle_file'])
        lifecycle_before = lifecycle.read_bytes()
        before = self.path.read_bytes()
        plan = park(self.path)
        self.assertFalse(plan['applied'])
        self.assertTrue(plan['changed'])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.root / 'codex-background-exports').exists())
        result = park(self.path, apply=True)
        after = load_config(self.path)
        self.assertTrue(result['applied'])
        self.assertNotIn('codex_background', after['command_adapters'])
        self.assertEqual([a['source_id'] for a in after['agents']], [other_sid])
        self.assertEqual(after['brand'], 'A newer user edit')
        self.assertEqual(after['graphics_fixture'], config['graphics_fixture'])
        self.assertEqual(after['disabled_command_agents'], ['codex:' + other_sid])
        self.assertIn(response['agent_id'], after['logged_out_agent_ids'])
        self.assertEqual(self.bridge.history.path.read_bytes(), history_before)
        self.assertEqual(lifecycle.read_bytes(), lifecycle_before)
        exported = Path(result['export_path'])
        self.assertEqual(exported.stat().st_mode & 0o777, 0o600)
        self.assertEqual(exported.parent.stat().st_mode & 0o777, 0o700)
        self.assertNotIn(config['topic'], exported.read_text())
        self.assertFalse(park(self.path, apply=True)['changed'])

    def test_rollback_non_posix_keeps_file_fsync_without_opening_directory(self):
        fake_os = SimpleNamespace(**{key: getattr(os, key) for key in
            ('O_CREAT', 'O_EXCL', 'O_WRONLY', 'O_RDONLY', 'fdopen', 'replace', 'close')})
        fake_os.name = 'nt'
        fake_os.open = Mock(wraps=os.open)
        fake_os.fsync = Mock(wraps=os.fsync)
        output = self.root / 'non-posix-fixture.json'
        with patch('codex_background_rollback.os', fake_os):
            _durable_json(output, {'preserved': True})
        self.assertEqual(json.loads(output.read_text()), {'preserved': True})
        self.assertEqual(fake_os.open.call_count, 1, 'Only the private temporary file is opened')
        self.assertEqual(fake_os.fsync.call_count, 1, 'File durability remains enabled')


if __name__ == '__main__':
    unittest.main()
