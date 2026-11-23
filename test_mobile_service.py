import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from types import SimpleNamespace

from commands import Commands, CommandError
from history import HistoryStore
from mobile_auth import MobileAuthError
from mobile_service import MobileService, MobileServiceError, public_origin
from office_members import OfficeMembers


A = 'codex:00000000-0000-4000-8000-000000000001'
B = 'codex:00000000-0000-4000-8000-000000000002'
CHILD = 'codex:child-one'
HIDDEN_CHILD = 'codex:child-hidden'
IDENTITY = 'fixture@example.test'


class Adapter:
    reason = None
    def __init__(self): self.calls = []
    def available(self): return True
    def submit(self, session, text):
        self.calls.append((session, text))
        return {'native_message_id': 'fixture-receipt'}
    def approvals(self, session):
        raise AssertionError('Mobile must not query native approvals')


class Bridge:
    def __init__(self, root):
        agents = [{'id': ident, 'provider': 'codex', 'driver': 'codex', 'source_id': ident.split(':', 1)[1],
                   'name': name} for ident, name in ((A, 'Visible'), (B, 'Hidden'))]
        self.config = {'data_dir': str(root), 'topic': 't' * 32, 'owner_key': 'o' * 43,
                       'internal_hmac_key': 'i' * 43, 'agents': agents,
                       'mobile': {'enabled': True, 'public_origin': 'https://office.example.ts.net', 'identity': IDENTITY},
                       'command_bindings': {a['id']: {'adapter': 'codex_app', 'session_id': a['source_id']} for a in agents}}
        self.definitions = {a['id']: a for a in agents}
        self.agents = {a['id']: dict(a, native_url='codex://private-native-url') for a in agents}
        self.stop = threading.Event()
        self.health = {}
        self.sources = {a['name']: {'status': 'done'} for a in agents}
        self.child_descriptors = {ident: {'id': ident, 'name': name, 'provider': 'codex', 'is_subagent': True,
                                       'parent_agent_id': parent, 'root_agent_id': parent, 'depth': 1,
                                       'evidence': 'native_metadata', 'source_kind': 'codex_native'}
                                  for ident, name, parent in ((CHILD, 'Visible child', A), (HIDDEN_CHILD, 'Hidden child', B))}
        self.children = SimpleNamespace(decorate_runs=self.decorate)
        self.history = HistoryStore(root / 'history.sqlite3')
        self.adapter = Adapter()
        self.commands = Commands(self.config, self.history, adapters={'codex_app': self.adapter})
        for ident in (A, B, CHILD, HIDDEN_CHILD):
            self.history.record(ident, 'codex', self.history_agents()[ident]['name'], 'run-1', 'done',
                                prompt='original prompt', delivery='finished', source_ref='/private/secret-path')
            self.history.message(ident, 'run-1', 'assistant', 'final', 'public response')

    def history_agents(self): return {**self.agents, **self.child_descriptors}
    def decorate(self, rows):
        return [dict(row, **{key: value for key, value in self.child_descriptors.get(row['agent_id'], {}).items()
                            if key not in {'id', 'name', 'provider'}}) for row in rows]
    def work_snapshot(self):
        rows = []
        for row in self.history_agents().values():
            rows.append(dict(row, state='done', state_changed_at='2026-11-15T00:00:00Z',
                             step='done', workspace={'id': 'ws', 'name': 'Workspace', 'path': '/private/workspace'},
                             ground_desk_id='g0-1', token='hidden-token', source_path='/private/source',
                             command_capability=self.commands.capability(row['id']) if row['id'] in self.agents else {},
                             latest_message={'text': 'Hello ' + self.config['owner_key'], 'role': 'assistant',
                                             'phase': 'final', 'at': '2026-11-15T00:00:00Z', 'private': 'hidden'}))
        return {'agents': rows, 'finished': self.decorate(self.history.list(self.history_agents(), terminal=True)),
                'brand': 'OFFICE', 'local_ok': True, 'history_ok': True, 'subagents_ok': True,
                'seating_revision': 900, 'private_key': self.config['owner_key']}


class MobileServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bridge = Bridge(Path(self.temp.name))
        self.service = MobileService(self.bridge)
        self.paired, self.read_principal, self.write_principal = self.pair([A], [A])

    def tearDown(self):
        self.service.close()
        self.bridge.commands.close()
        self.bridge.history.close()
        self.temp.cleanup()

    def pair(self, view, send, **modes):
        invitation = self.service.owner_create_pairing({'identity': IDENTITY, 'label': 'Fixture phone',
                                                       'view_agents': view, 'send_agents': send, **modes})
        result = self.service.auth.pair(invitation['pairing_token'], IDENTITY)
        read = self.service.auth.authenticate(result['session_token'], IDENTITY)
        write = self.service.auth.authenticate(result['session_token'], IDENTITY, csrf=result['csrf_token'], write=True)
        return result, read, write

    def request(self, agent=A):
        return {'request_id': str(uuid.uuid4()), 'agent_id': agent, 'text': 'bounded fixture message'}

    def test_owner_setup_keeps_configuration_and_phone_proof_separate(self):
        before = set(Path(self.temp.name).iterdir())
        result = self.service.owner_status()
        self.assertTrue(result['configured'])
        self.assertTrue(result['setup']['configuration_ready'])
        self.assertFalse(result['setup']['phone_verified'])
        self.assertFalse(result['setup']['end_to_end_verified'])
        self.assertEqual(result['setup']['transport']['kind'], 'unverified')
        self.assertEqual(before, set(Path(self.temp.name).iterdir()))
        self.assertNotIn(self.bridge.config['owner_key'], json.dumps(result))

    def test_incomplete_assistant_setup_blocks_a_stale_owner_pairing_request(self):
        from mobile_setup import mark_assistant_pending, clear_assistant_pending
        config = self.bridge.config
        config['data_dir'] = str(Path(config['data_dir']).resolve())  # Production load_config resolves it.
        self.assertTrue(self.service.owner_status()['setup']['pairing_available'])
        data = {'identity': IDENTITY, 'label': 'Another phone', 'view_agents': [A], 'send_agents': []}
        mark_assistant_pending(config, config['mobile'])
        with self.assertRaises(MobileServiceError) as error:
            self.service.owner_create_pairing(data)
        self.assertEqual((error.exception.status, error.exception.code), (409, 'mobile_setup_pending'))
        clear_assistant_pending(config, config['mobile'])
        self.assertIn('pairing_token', self.service.owner_create_pairing(data))

    def test_pairing_roster_uses_registered_bindings_without_starting_provider_checks(self):
        self.bridge.adapter.available = lambda: self.fail('Setup must not contact a provider')
        self.bridge.config['agents'] = [a for a in self.bridge.config['agents'] if a['id'] != B]
        roster = self.service.owner_status()['agents']
        self.assertEqual({a['id']: a['connected'] for a in roster}, {A: True, B: False})
        request = {'identity': IDENTITY, 'label': 'Test phone', 'view_agents': [A, B], 'send_agents': [B]}
        with self.assertRaises(MobileServiceError) as error:
            self.service.owner_create_pairing(request)
        self.assertEqual(error.exception.code, 'mobile_agent_not_connected')
        request['send_agents'] = [A]
        self.assertIn('pairing_token', self.service.owner_create_pairing(request))

    def test_remembered_logout_revokes_expired_access_without_widening_identity(self):
        invitation = self.service.owner_create_pairing({'identity': IDENTITY, 'label': 'Remember fixture',
                                                       'view_agents': [A], 'send_agents': [A]})
        paired = self.service.auth.pair(invitation['pairing_token'], IDENTITY, remember_device=True)
        token = paired['renewal_token']
        now = self.service.auth.clock()
        self.service.auth.clock = lambda: now + 3601
        self.assertEqual(self.service.auth.renewal_device(token, IDENTITY)['state'], 'renewal_required')
        with self.assertRaises(MobileServiceError):
            self.service.logout_renewal(token, 'other@example.test')
        result = self.service.logout_renewal(token, IDENTITY)
        self.assertEqual(result['device']['state'], 'revoked')
        with self.assertRaises(MobileAuthError):
            self.service.auth.renew(token, IDENTITY)

    def test_work_projection_retains_3d_and_children_without_ungranted_data(self):
        result = self.service.read('work', {}, self.read_principal)
        self.assertEqual({row['id'] for row in result['agents']}, {A, CHILD})
        self.assertEqual(result['subagent_count'], 1)
        self.assertEqual(result['agents'][0]['child_count'], 1)
        self.assertTrue(result['agents'][0]['command_capability']['can_send'])
        self.assertFalse(result['feed_enabled'])
        text = json.dumps(result)
        for forbidden in (B, HIDDEN_CHILD, '/private', 'hidden-token', self.bridge.config['owner_key'],
                          self.bridge.config['topic'], 'native_url', 'seating_revision', 'source_id'):
            self.assertNotIn(forbidden, text)
        self.assertEqual(result['agents'][0]['workspace'], {'id': 'ws', 'name': 'Workspace'})

    def test_history_messages_and_run_are_scoped_including_children(self):
        for agent in (A, CHILD):
            history = self.service.read('history', {'agent': [agent]}, self.read_principal)
            detail = self.service.read('run', {'id': history['runs'][0]['id']}, self.read_principal)
            self.assertEqual(detail['delivery'], 'finished')
            self.assertNotIn('source_ref', detail)
            self.assertEqual(self.service.read('messages', {'agent': agent}, self.read_principal)['messages'][0]['text'], 'public response')
        hidden_run = self.bridge.history.list(self.bridge.history_agents(), agent=B)[0]['id']
        for route, query in [('history', {'agent': B}), ('messages', {'agent': HIDDEN_CHILD}), ('run', {'id': hidden_run})]:
            with self.assertRaises(MobileServiceError) as error:
                self.service.read(route, query, self.read_principal)
            self.assertEqual(error.exception.status, 404)

    def test_health_tracks_office_observation_not_native_or_legacy_feed_availability(self):
        self.bridge.sources.pop('Hidden')
        self.bridge.health['last_publish_error'] = 'private publisher details'
        self.bridge.adapter.available = lambda: False
        self.assertEqual(self.service.read('health', {}, self.read_principal),
                         {'ok': True, 'local_ok': True, 'sync_ok': True})
        self.assertTrue(self.service.read('work', {}, self.read_principal)['local_ok'])
        self.bridge.sources.pop('Visible')
        # An old state_changed_at in a cached snapshot does not establish health.
        self.assertEqual(self.service.read('health', {}, self.read_principal),
                         {'ok': True, 'local_ok': False, 'sync_ok': False})
        self.assertFalse(self.service.read('work', {}, self.read_principal)['local_ok'])

    def test_recorded_observation_errors_and_shutdown_are_not_reported_live(self):
        for key in ('source_lookup_error', 'history_error', 'subagent_lookup_error', 'child_observer_error'):
            with self.subTest(key=key):
                self.bridge.health[key] = 'private error details'
                self.assertEqual(self.service.read('health', {}, self.read_principal),
                                 {'ok': True, 'local_ok': False, 'sync_ok': False})
                self.assertFalse(self.service.read('work', {}, self.read_principal)['local_ok'])
                self.bridge.health.pop(key)
        self.bridge.stop.set()
        self.assertEqual(self.service.read('health', {}, self.read_principal),
                         {'ok': False, 'local_ok': False, 'sync_ok': False})

    def test_old_history_text_gets_current_secret_redaction(self):
        secret = self.bridge.config['owner_key']
        self.bridge.history.record(A, 'codex', 'Visible', 'old-run', 'done', prompt=secret, delivery=secret)
        self.bridge.history.message(A, 'old-run', 'assistant', 'final', secret)
        for route, query in [('history', {'agent': A}), ('messages', {'agent': A})]:
            self.assertNotIn(secret, json.dumps(self.service.read(route, query, self.read_principal)))

    def test_query_allowlist_rejects_duplicates_unknowns_and_bad_cursors(self):
        for route, query in [('work', {'key': 'x'}), ('messages', {'agent': [A, B]}),
                             ('messages', {'agent': A, 'after': '-1'}), ('messages', {'agent': A, 'before': 'no'}),
                             ('history', {}), ('run', {'id': 'not-a-run', 'owner': 'x'})]:
            with self.assertRaises(MobileServiceError):
                self.service.read(route, query, self.read_principal)
        with self.assertRaises(MobileServiceError) as error:
            self.service.read('/api/mobile/grok/sdk/login/status', {}, self.read_principal)
        self.assertEqual(error.exception.status, 404)

    def test_view_only_cannot_send_or_cancel_and_never_fetches_approvals(self):
        _, read, write = self.pair([A], [])
        result = self.service.read('commands', {'agent': A}, read)
        self.assertEqual(result['approvals'], [])
        self.assertFalse(result['capability']['can_send'])
        with self.assertRaises(MobileAuthError): self.service.send(self.request(), write)
        with self.assertRaises(MobileAuthError): self.service.cancel({'agent_id': A, 'command_id': str(uuid.uuid4())}, write)

    def test_send_requires_csrf_and_cannot_target_other_agent_or_child(self):
        with self.assertRaises(MobileAuthError): self.service.send(self.request(), self.read_principal)
        for agent in (B, CHILD):
            with self.assertRaises(MobileAuthError): self.service.send(self.request(agent), self.write_principal)
        self.assertEqual(self.bridge.adapter.calls, [])

    def test_send_records_device_retries_once_and_syncs_public_delivery(self):
        request = self.request()
        created = self.service.send(request, self.write_principal)
        self.assertFalse(created['duplicate'])
        self.assertTrue(self.service.send(request, self.write_principal)['duplicate'])
        self.bridge.commands.tick()
        self.assertEqual(len(self.bridge.adapter.calls), 1)
        body = self.bridge.adapter.calls[0][1]
        self.bridge.commands.observe_prompt(A, 'mobile-run', body)
        self.bridge.history.record(A, 'codex', 'Visible', 'mobile-run', 'done', prompt=request['text'], delivery='phone answer')
        self.bridge.history.message(A, 'mobile-run', 'assistant', 'final', 'phone answer')
        self.bridge.commands.observe_run(A, 'mobile-run', 'done')
        commands = self.service.read('commands', {'agent': A}, self.read_principal)
        self.assertEqual(commands['commands'][0]['state'], 'completed')
        self.assertIn('phone answer', json.dumps(self.service.read('messages', {'agent': A}, self.read_principal)))

    def test_revoke_blocks_future_reads_and_cancels_unreceived_work(self):
        request = self.request(); self.service.send(request, self.write_principal)
        result = self.service.owner_revoke(self.paired['device']['device_id'])
        self.assertEqual(result['cancelled'], 1)
        self.assertEqual(result['already_received'], 0)
        self.bridge.commands.tick()
        self.assertEqual(self.bridge.adapter.calls, [])
        with self.assertRaises(MobileAuthError): self.service.read('health', {}, self.read_principal)

    def test_other_device_cannot_cancel_pending_command(self):
        created = self.service.send(self.request(), self.write_principal)
        _, read, write = self.pair([A], [A])
        commands = self.service.read('commands', {'agent': A}, read)
        self.assertFalse(commands['commands'][0]['can_cancel'])
        with self.assertRaises(CommandError) as error:
            self.service.cancel({'agent_id': A, 'command_id': created['command']['id']}, write)
        self.assertEqual(error.exception.status, 404)

    def test_disabled_changed_identity_and_origin_block_queued_dispatch(self):
        for key, value in [('enabled', False), ('identity', 'changed@example.test'),
                           ('public_origin', 'https://changed.example.ts.net')]:
            with self.subTest(key=key):
                request = self.request(); self.service.send(request, self.write_principal)
                previous = self.bridge.config['mobile'][key]
                self.bridge.config['mobile'][key] = value
                self.bridge.commands.tick()
                self.assertEqual(self.bridge.adapter.calls, [])
                row = self.bridge.commands.db.execute('SELECT state FROM commands WHERE id=?', (request['request_id'],)).fetchone()
                self.assertEqual(row['state'], 'cancelled')
                self.bridge.config['mobile'][key] = previous

    def test_membership_change_removes_old_scope_and_pairing(self):
        self.bridge.definitions[A]['generation'] = 'new-membership'
        self.assertEqual(self.service.read('work', {}, self.read_principal)['agents'], [])
        with self.assertRaises(MobileAuthError): self.service.send(self.request(), self.write_principal)

    def _assert_interrupted_logout_stays_revoked_after_identical_rejoin(self, *, restart):
        invitation = self.service.owner_create_pairing({'identity': IDENTITY, 'label': 'Unclaimed phone',
                                                       'view_agents': [A], 'send_agents': [A]})
        queued = self.service.send(self.request(), self.write_principal)['command']['id']
        definition = copy.deepcopy(self.bridge.definitions[A])
        descriptor = copy.deepcopy(self.bridge.agents[A])
        # Simulate the durable config retirement having committed, followed by
        # a crash before mobile.invalidate_agent could commit its own database.
        self.bridge.config['agents'] = [row for row in self.bridge.config['agents'] if row['id'] != A]
        self.bridge.config['logged_out_agent_ids'] = [A]
        self.bridge.definitions.pop(A)
        self.bridge.agents.pop(A)
        if restart:
            self.service.close()
            self.service = MobileService(self.bridge)
        else:
            self.bridge.mobile = self.service
            # The fake Bridge does not construct Invitations or Inbox; only
            # their membership cleanup tables are needed for this repair path.
            self.bridge.history.db.execute('CREATE TABLE IF NOT EXISTS invitations (provider TEXT,source_id TEXT,name TEXT)')
            self.bridge.commands.db.execute('CREATE TABLE IF NOT EXISTS inbox_credentials (agent_id TEXT PRIMARY KEY)')
            members = OfficeMembers(self.bridge)
            members.sync(self.bridge.config)
        # Reusing the exact definition and native binding must not resurrect a
        # grant just because the membership fingerprint once matched it.
        self.bridge.config['agents'].append(definition)
        self.bridge.config['logged_out_agent_ids'] = []
        self.bridge.definitions[A] = definition
        self.bridge.agents[A] = descriptor
        fresh = self.service.auth.authenticate(self.paired['session_token'], IDENTITY,
                                              csrf=self.paired['csrf_token'], write=True)
        self.assertNotIn(A, fresh['view_agents'])
        self.assertNotIn(A, fresh['send_agents'])
        self.assertGreater(fresh['revision'], self.paired['device']['revision'])
        self.assertEqual(self.service.read('work', {}, fresh)['agents'], [])
        with self.assertRaises(MobileAuthError): self.service.send(self.request(), fresh)
        with self.assertRaises(MobileAuthError): self.service.auth.pair(invitation['pairing_token'], IDENTITY)
        self.bridge.commands.tick()
        self.assertEqual(self.bridge.adapter.calls, [])
        self.assertEqual(self.bridge.commands.db.execute('SELECT state FROM commands WHERE id=?', (queued,)).fetchone()['state'], 'cancelled')

    def test_startup_repairs_interrupted_mobile_scope_revocation_before_rejoin(self):
        self._assert_interrupted_logout_stays_revoked_after_identical_rejoin(restart=True)

    def test_membership_sync_repairs_interrupted_mobile_scope_revocation_before_rejoin(self):
        self._assert_interrupted_logout_stays_revoked_after_identical_rejoin(restart=False)

    def test_owner_pairing_exact_identity_and_connected_targets(self):
        data = {'identity': 'other@example.test', 'label': 'phone', 'view_agents': [A], 'send_agents': [A]}
        with self.assertRaises(MobileServiceError): self.service.owner_create_pairing(data)
        data['identity'] = IDENTITY; data['owner_key'] = 'not-allowed'
        with self.assertRaises(MobileServiceError): self.service.owner_create_pairing(data)
        status = self.service.owner_status()
        self.assertTrue(status['credentials_separated'])
        text = json.dumps(status)
        for key in ('session_token', 'csrf_token', 'pairing_token', self.bridge.config['owner_key']):
            self.assertNotIn(key, text)

    def test_enrolled_view_projects_all_registered_workspaces_and_only_their_children(self):
        _, read, write = self.pair([], [A], view_mode='enrolled')
        work_snapshot = self.bridge.work_snapshot
        def floors():
            result = work_snapshot()
            for row in result['agents']:
                if row['id'] in (B, HIDDEN_CHILD):
                    row['workspace'] = {'id': 'another-floor', 'name': 'Other project', 'path': '/private/other'}
            return result
        self.bridge.work_snapshot = floors
        projected = self.service.read('work', {}, read)
        self.assertEqual({a['id'] for a in projected['agents']}, {A, B, CHILD, HIDDEN_CHILD})
        self.assertEqual({a['workspace']['id'] for a in projected['agents']}, {'ws', 'another-floor'})
        self.assertNotIn('/private', json.dumps(projected))
        self.assertFalse(next(a for a in projected['agents'] if a['id'] == B)['command_capability']['can_send'])
        with self.assertRaises(MobileAuthError): self.service.send(self.request(B), write)
        # Logged-out parents and their retained child/history descriptors stop
        # appearing immediately, without changing the phone's selected sender.
        self.bridge.definitions.pop(B)
        self.assertEqual({a['id'] for a in self.service.read('work', {}, read)['agents']}, {A, CHILD})
        for route, query in [('history', {'agent': B}), ('messages', {'agent': HIDDEN_CHILD})]:
            with self.assertRaises(MobileServiceError): self.service.read(route, query, read)

    def test_enrolled_scope_follows_new_registration_and_removal_without_repairing(self):
        definition = self.bridge.definitions.pop(B)
        _, read, write = self.pair([], [], view_mode='enrolled', send_mode='enrolled')
        self.assertEqual({a['id'] for a in self.service.read('work', {}, read)['agents']}, {A, CHILD})
        self.bridge.definitions[B] = definition
        self.assertEqual({a['id'] for a in self.service.read('work', {}, read)['agents']}, {A, B, CHILD, HIDDEN_CHILD})
        old = self.service.send(self.request(B), write)['command']['id']
        self.bridge.definitions.pop(B)
        self.service.invalidate_agent(B)
        self.bridge.definitions[B] = definition
        self.bridge.commands.tick()
        self.assertEqual(self.bridge.adapter.calls, [])
        row = self.bridge.commands.db.execute('SELECT state,detail_code FROM commands WHERE id=?', (old,)).fetchone()
        self.assertEqual(row['state'], 'cancelled')
        self.assertEqual(row['detail_code'], 'mobile_authorization_revoked')
        self.service.send(self.request(B), write)
        self.bridge.commands.tick()
        self.assertEqual(len(self.bridge.adapter.calls), 1)

    def test_scope_update_retains_session_cancels_pending_and_preserves_received_work(self):
        running = self.service.send(self.request(), self.write_principal)['command']['id']
        self.bridge.commands.tick()
        self.bridge.commands.observe_prompt(A, 'scope-running', self.bridge.adapter.calls[0][1])
        pending = self.service.send(self.request(), self.write_principal)['command']['id']
        policy = {'device_id': self.paired['device']['device_id'], 'view_agents': [], 'send_agents': [A],
                  'view_mode': 'enrolled', 'send_mode': 'selected'}
        changed = self.service.owner_update_scopes(policy)
        self.assertTrue(changed['changed'])
        self.assertEqual(changed['cancelled'], 1)
        self.assertEqual(changed['already_received'], 1)
        self.assertEqual(self.bridge.commands.db.execute('SELECT state FROM commands WHERE id=?', (pending,)).fetchone()[0], 'cancelled')
        self.assertEqual(self.bridge.commands.db.execute('SELECT state FROM commands WHERE id=?', (running,)).fetchone()[0], 'running')
        fresh = self.service.auth.authenticate(self.paired['session_token'], IDENTITY,
                                              csrf=self.paired['csrf_token'], write=True)
        self.assertEqual({a['id'] for a in self.service.read('work', {}, fresh)['agents']}, {A, B, CHILD, HIDDEN_CHILD})
        self.assertEqual(fresh['send_agents'], [A])
        unchanged = self.service.owner_update_scopes(policy)
        self.assertFalse(unchanged['changed'])
        self.assertNotIn('cancelled', unchanged)
        for invalid in ({**policy, 'identity': IDENTITY}, {'device_id': policy['device_id']}):
            with self.assertRaises(MobileServiceError): self.service.owner_update_scopes(invalid)
        self.service.owner_revoke(policy['device_id'])
        with self.assertRaises(MobileAuthError): self.service.owner_update_scopes(policy)

    def test_scope_update_racing_authenticated_read_does_not_revoke_phone_cookie(self):
        self.service.owner_update_scopes({'device_id': self.paired['device']['device_id'],
            'view_agents': [], 'send_agents': [A], 'view_mode': 'enrolled', 'send_mode': 'selected'})
        with self.assertRaises(MobileAuthError) as error:
            self.service.read('work', {}, self.read_principal)
        self.assertEqual((error.exception.status, error.exception.code), (409, 'mobile_scope_changed'))
        fresh = self.service.auth.authenticate(self.paired['session_token'], IDENTITY)
        self.assertEqual({a['id'] for a in self.service.read('work', {}, fresh)['agents']}, {A, B, CHILD, HIDDEN_CHILD})

    def test_scope_change_between_request_refresh_and_send_lock_blocks_without_revoking_cookie(self):
        original = self.service._principal
        def racing(principal):
            current = original(principal)
            self.service.owner_update_scopes({'device_id': self.paired['device']['device_id'],
                'view_agents': [], 'send_agents': [], 'view_mode': 'enrolled', 'send_mode': 'selected'})
            return current
        self.service._principal = racing
        with self.assertRaises(MobileAuthError) as error:
            self.service.send(self.request(), self.write_principal)
        self.assertEqual((error.exception.status, error.exception.code), (409, 'mobile_scope_changed'))
        self.assertEqual(self.bridge.adapter.calls, [])
        self.assertEqual(self.bridge.commands.db.execute('SELECT COUNT(*) FROM commands').fetchone()[0], 0)
        self.service._principal = original
        fresh = self.service.auth.authenticate(self.paired['session_token'], IDENTITY,
                                              csrf=self.paired['csrf_token'], write=True)
        self.assertEqual(fresh['send_agents'], [])
        self.assertEqual(fresh['state'], 'ready')

    def test_enrolled_pairing_does_not_require_redundant_roster_to_be_online(self):
        self.bridge.adapter.available = lambda: False
        paired, _, _ = self.pair([A, B], [A, B], view_mode='enrolled', send_mode='enrolled')
        self.assertEqual(paired['device']['view_agents'], [A, B])
        self.assertEqual(paired['device']['send_agents'], [A, B])

    def test_no_mobile_routes_before_credential_upgrade(self):
        del self.bridge.config['owner_key']; del self.bridge.config['internal_hmac_key']
        self.assertFalse(self.service.owner_status()['configured'])
        with self.assertRaises(MobileServiceError): self.service.read('work', {}, self.read_principal)

    def test_origin_is_private_canonical_https_only(self):
        for value in ['http://office.example.ts.net', 'https://example.com', 'https://office.example.ts.net:443',
                      'https://user@office.example.ts.net', 'https://office.example.ts.net/',
                      'https://office.example.ts.net?x=1', 'https://office.example.ts.net#private']:
            self.assertIsNone(public_origin({'mobile': {'public_origin': value}}))
        self.assertEqual(public_origin(self.bridge.config), 'https://office.example.ts.net')


if __name__ == '__main__': unittest.main()
