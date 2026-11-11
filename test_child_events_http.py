"""Real loopback tests for scoped child-observer ingress; synthetic data only."""
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

from configuration import initialize, load_config, save_config
from dashboard import Bridge, proxy_handler
from history import timestamp


ROOT_SOURCE = 'http-fixture-root'
ROOT_ID = 'cursor:' + ROOT_SOURCE
ROOT_TOKEN = 'synthetic-root-report-token-only'
NATIVE_ROOT = '10000000-0000-4000-8000-000000000101'
NATIVE_CHILD = '10000000-0000-4000-8000-000000000201'
OTHER_NATIVE = '10000000-0000-4000-8000-000000000999'


class _BodySignal:
    """Signal that a real handler reached body read after header authentication."""
    def __init__(self, stream, handler, ready):
        self.stream, self.handler, self.ready = stream, handler, ready

    def read(self, *args, **kwargs):
        if self.handler.headers.get('X-Test-Pause-Body') == 'yes':
            self.ready.set()
        return self.stream.read(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.stream, name)


class ChildEventsHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='office child HTTP ')
        self.directory = Path(self.tmp.name).resolve()
        self.config_path = self.directory / 'config.json'
        initialize(self.config_path, 4444)
        config = load_config(self.config_path)
        config.update(run_id='child-http-fixture', run_dir=str(self.directory / 'run'))
        config['agents'] = [{'provider': 'cursor', 'source_id': ROOT_SOURCE,
                             'name': 'HTTP fixture parent', 'driver': 'push',
                             'token_sha256': hashlib.sha256(ROOT_TOKEN.encode()).hexdigest()}]
        save_config(self.config_path, config)
        self.bridge = Bridge(load_config(self.config_path), dry_run=True)
        self.body_ready = threading.Event()
        base_handler = proxy_handler(self.bridge)
        ready = self.body_ready

        class Handler(base_handler):
            def setup(handler):
                super().setup()
                handler.rfile = _BodySignal(handler.rfile, handler, ready)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.bridge.config['port'] = self.server.server_address[1]
        save_config(self.config_path, self.bridge.config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.bridge.config['port'])
        self.observer = self.register(NATIVE_ROOT)
        self.root_start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.bridge.commands.close()
        self.bridge.history.close()
        self.tmp.cleanup()

    def call(self, route, data=None, *, owner=False, observer=None,
             reporter=False, headers=None, method=None):
        request_headers = {'Content-Type': 'application/json'}
        if owner:
            request_headers['X-Office-Key'] = self.bridge.config['topic']
        if observer is not None:
            request_headers.update({'X-Observer-Id': observer['observer_id'],
                                    'Authorization': 'Bearer ' + observer['token']})
        if reporter:
            request_headers.update({'X-Agent-Id': ROOT_ID,
                                    'Authorization': 'Bearer ' + ROOT_TOKEN})
        request_headers.update(headers or {})
        request = urllib.request.Request(self.base + route,
            data=json.dumps(data).encode() if data is not None else None,
            headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def register(self, native):
        code, value = self.call('/api/office/child-observers',
            {'agent_id': ROOT_ID, 'provider': 'cursor', 'native_session_id': native}, owner=True)
        self.assertEqual(code, 200, value)
        credential_path = Path(value['credential_path'])
        self.assertTrue(credential_path.is_relative_to(self.directory))
        self.assertEqual(credential_path.stat().st_mode & 0o077, 0)
        credential = json.loads(credential_path.read_text())
        for key in ('observer_id', 'token', 'generation', 'provider',
                    'native_session_id', 'endpoint', 'root_event_endpoint'):
            self.assertIn(key, credential)
        self.assertEqual(credential['provider'], 'cursor')
        self.assertEqual(credential['native_session_id'], native)
        self.assertTrue(credential['endpoint'].endswith('/api/agents/children/events'))
        self.assertTrue(credential['root_event_endpoint'].endswith('/api/agents/observers/root-events'))
        return credential

    def root_start(self, run='parent-run-one', observer=None):
        observer = observer or self.observer
        event = {'source_id': observer['native_session_id'], 'run_id': run,
                 'event_id': 'root-start:' + run, 'type': 'turn_started',
                 'text': 'Root public task', 'at': timestamp()}
        code, value = self.call('/api/agents/observers/root-events', event, observer=observer)
        self.assertEqual(code, 200, value)
        self.assertTrue(value.get('accepted'))
        return event

    def child_event(self, event_id='child-start-one', kind='started', **changes):
        result = {'event_id': event_id, 'native_child_id': NATIVE_CHILD,
                  'native_parent_id': self.observer['native_session_id'],
                  'parent_run_id': 'parent-run-one', 'child_run_id': 'child-run-one',
                  'type': kind, 'at': timestamp()}
        if kind == 'started':
            result.update(name='Fixture child', text='Delegated public task')
        elif kind == 'message':
            result.update(role='assistant', phase='commentary', text='Child public progress')
        elif kind == 'completed':
            result['text'] = 'Child delivered report'
        result.update(changes)
        return result

    def emit(self, event, observer=None, **kwargs):
        return self.call('/api/agents/children/events', event,
                         observer=observer or self.observer, **kwargs)

    def child(self):
        code, value = self.emit(self.child_event())
        self.assertEqual(code, 200, value)
        code, work = self.call('/api/office/work', owner=True)
        self.assertEqual(code, 200)
        children = [agent for agent in work['agents'] if agent.get('is_subagent')]
        self.assertEqual(len(children), 1, work)
        return children[0]

    def fresh_pending(self):
        """Add a second synthetic selected root without a native observation binding."""
        source = 'http-fixture-pending'
        ident, token = 'cursor:' + source, 'synthetic-pending-report-token-only'
        config = load_config(self.config_path)
        config['agents'].append({'provider': 'cursor', 'source_id': source,
                                 'name': 'HTTP bootstrap parent', 'driver': 'push',
                                 'token_sha256': hashlib.sha256(token.encode()).hexdigest()})
        save_config(self.config_path, config)
        with self.bridge.commands.lock, self.bridge.history.lock:
            self.bridge.refresh_connections(load_config(self.config_path))
        headers = {'X-Agent-Id': ident, 'Authorization': 'Bearer ' + token}
        before = self.logical_root_state(ident)
        code, binding = self.call('/api/agents/observers/pending', {}, headers=headers)
        self.assertEqual(code, 200, binding)
        self.assertTrue(binding['pending'])
        self.assertTrue(binding['activation_marker'].startswith('OFFICE_CURSOR_BIND_'))
        self.assertNotIn('token', binding)
        path = Path(binding['credential_path'])
        self.assertTrue(path.is_relative_to(self.directory))
        self.assertEqual(path.stat().st_mode & 0o077, 0)
        credential = json.loads(path.read_text())
        self.assertEqual(credential['activation_marker'], binding['activation_marker'])
        self.assertTrue(credential['native_session_id'].startswith('pending:'))
        self.assertEqual(before, self.logical_root_state(ident))
        return ident, headers, binding, credential

    def logical_root_state(self, ident):
        agent = self.bridge.definitions[ident]
        live = self.bridge.history.db.execute('SELECT run_id,status FROM live_runs WHERE agent_id=?', (ident,)).fetchone()
        commands = self.bridge.commands.db.execute('SELECT id,state,run_id FROM commands WHERE agent_id=? ORDER BY id', (ident,)).fetchall()
        return {'source_id': agent['source_id'], 'driver': agent['driver'],
                'status': self.bridge.sources.get(agent['name'], {}).get('status'),
                'live': tuple(live) if live else None,
                'commands': [tuple(row) for row in commands]}

    def test_pending_request_reuses_bound_observer_without_downgrading_root(self):
        before = self.logical_root_state(ROOT_ID)
        code, binding = self.call('/api/agents/observers/pending', {}, reporter=True)
        self.assertEqual(code, 200, binding)
        self.assertFalse(binding['pending'])
        saved = json.loads(Path(binding['credential_path']).read_text())
        self.assertEqual(saved['observer_id'], self.observer['observer_id'])
        self.assertEqual(saved['generation'], self.observer['generation'])
        self.assertTrue(saved['token'] == self.observer['token'], 'Bound observer token must remain unchanged')
        self.assertEqual(saved['native_session_id'], NATIVE_ROOT)
        self.assertEqual(before, self.logical_root_state(ROOT_ID))
        self.assertNotIn('token', binding)
        self.assertEqual(self.call('/api/agents/observers/pending', {'agent_id': ROOT_ID}, reporter=True)[0], 400)
        self.assertEqual(self.call('/api/agents/observers/pending', {}, observer=self.observer)[0], 403)

    def test_pending_observer_cannot_emit_native_or_root_events_before_binding(self):
        _, headers, binding, pending = self.fresh_pending()
        self.assertEqual(self.emit(self.child_event(), observer=pending)[0], 403)
        root = {'source_id': OTHER_NATIVE, 'run_id': 'bootstrap-root-run',
                'event_id': 'bootstrap-root-start', 'type': 'turn_started', 'text': 'Not bound yet'}
        self.assertEqual(self.call('/api/agents/observers/root-events', root, observer=pending)[0], 403)
        code, repeated = self.call('/api/agents/observers/pending', {}, headers=headers)
        self.assertEqual(code, 200, repeated)
        self.assertTrue(repeated['pending'])
        self.assertEqual(repeated['observer_id'], binding['observer_id'])
        self.assertEqual(repeated['activation_marker'], binding['activation_marker'])
        self.assertTrue(json.loads(Path(repeated['credential_path']).read_text())['token'] == pending['token'])

    def test_binding_pending_observer_rotates_file_token_and_supports_same_session_idempotence(self):
        ident, _, binding, pending = self.fresh_pending()
        before = self.logical_root_state(ident)
        body = {'native_session_id': OTHER_NATIVE, 'activation_marker': binding['activation_marker']}
        code, active = self.call('/api/agents/observers/bind', body, observer=pending)
        self.assertEqual(code, 200, active)
        self.assertFalse(active['pending'])
        self.assertNotIn('token', active)
        path = Path(active['credential_path'])
        bound = json.loads(path.read_text())
        self.assertEqual(bound['native_session_id'], OTHER_NATIVE)
        self.assertNotEqual(bound['generation'], pending['generation'])
        self.assertTrue(bound['token'] != pending['token'], 'Binding must rotate pending observer token')
        self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertEqual(self.logical_root_state(ident), before)
        self.assertEqual(self.call('/api/agents/observers/bind', body, observer=pending)[0], 403)
        self.assertEqual(self.emit(self.child_event(), observer=pending)[0], 403)
        root = {'source_id': OTHER_NATIVE, 'run_id': 'stale-pending-root',
                'event_id': 'stale-pending-root-start', 'type': 'turn_started'}
        self.assertEqual(self.call('/api/agents/observers/root-events', root, observer=pending)[0], 403)
        prior_bytes = path.read_bytes()
        code, repeated = self.call('/api/agents/observers/bind', body, observer=bound)
        self.assertEqual(code, 200, repeated)
        self.assertFalse(repeated['pending'])
        self.assertEqual(path.read_bytes(), prior_bytes)
        other = {**body, 'native_session_id': NATIVE_CHILD}
        self.assertEqual(self.call('/api/agents/observers/bind', other, observer=bound)[0], 409)
        self.assertEqual(path.read_bytes(), prior_bytes)

    def test_wrong_activation_marker_does_not_activate_or_rotate_pending_observer(self):
        ident, _, binding, pending = self.fresh_pending()
        path = Path(binding['credential_path'])
        before = path.read_bytes()
        body = {'native_session_id': OTHER_NATIVE, 'activation_marker': 'OFFICE_CURSOR_BIND_wrong_fixture_marker'}
        self.assertEqual(self.call('/api/agents/observers/bind', body, observer=pending)[0], 403)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.emit(self.child_event(), observer=pending)[0], 403)
        self.assertEqual(self.bridge.definitions[ident]['source_id'], 'http-fixture-pending')

    def test_owner_setup_and_observer_callback_require_separate_credentials(self):
        body = {'agent_id': ROOT_ID, 'provider': 'cursor', 'native_session_id': NATIVE_ROOT}
        self.assertEqual(self.call('/api/office/child-observers', body)[0], 403)
        self.assertEqual(self.call('/api/office/child-observers', body, observer=self.observer)[0], 403)
        event = self.child_event()
        self.assertEqual(self.call('/api/agents/children/events', event, owner=True)[0], 403)
        self.assertEqual(self.call('/api/agents/children/events', event, reporter=True)[0], 403)
        self.assertEqual(self.emit(event, observer={**self.observer, 'token': 'wrong-token'})[0], 403)
        self.assertEqual(self.emit(event, observer={**self.observer, 'observer_id': 'unknown-observer'})[0], 403)

    def test_native_child_is_read_only_and_completion_does_not_finish_parent(self):
        child = self.child()
        self.assertEqual(child['provider'], 'cursor')
        self.assertEqual(child['root_agent_id'], ROOT_ID)
        self.assertEqual(child['evidence'], 'native_hook')
        self.assertFalse(child['command_capability']['can_send'])
        self.assertNotIn(child['id'], self.bridge.agents)
        self.assertNotIn(child['id'], self.bridge.definitions)
        self.assertEqual(len(load_config(self.config_path)['agents']), 1)
        self.assertEqual(self.emit(self.child_event('child-done', 'completed'))[0], 200)
        code, work = self.call('/api/office/work', owner=True)
        self.assertEqual(code, 200)
        root = next(agent for agent in work['agents'] if agent['id'] == ROOT_ID)
        self.assertEqual(root['state'], 'working')
        self.assertEqual(next(agent for agent in work['agents'] if agent.get('is_subagent'))['state'], 'done')

    def test_unknown_fields_and_hidden_messages_are_rejected(self):
        self.child()
        for extra in ({'root_agent_id': 'cursor:forged'}, {'provider': 'grok_bot'},
                      {'generation': self.observer['generation']}, {'tool_output': 'PRIVATE_TOOL'}):
            event = self.child_event('forged-field', 'message', **extra)
            self.assertEqual(self.emit(event)[0], 400)
        event = self.child_event('hidden-phase', 'message', phase='analysis', text='PRIVATE_REASONING')
        self.assertEqual(self.emit(event)[0], 400)

    def test_forged_native_parent_and_unstarted_parent_run_are_denied(self):
        code, _ = self.emit(self.child_event(native_parent_id=OTHER_NATIVE))
        self.assertIn(code, (403, 409))
        code, _ = self.emit(self.child_event(parent_run_id='never-started-parent'))
        self.assertIn(code, (403, 409))
        self.assertEqual(self.call('/api/office/work', owner=True)[1]['subagent_count'], 0)

    def test_duplicate_callbacks_and_conflicting_replays_preserve_history(self):
        event = self.child_event()
        code, first = self.emit(event)
        self.assertEqual(code, 200, first)
        code, replay = self.emit(event)
        self.assertEqual(code, 200, replay)
        self.assertTrue(replay.get('duplicate'))
        self.assertEqual(self.emit({**event, 'text': 'Different same event'})[0], 409)
        child = next(agent for agent in self.call('/api/office/work', owner=True)[1]['agents'] if agent.get('is_subagent'))
        query = urllib.parse.urlencode({'agent': child['id']})
        messages = self.call('/api/office/messages?' + query, owner=True)[1]['messages']
        self.assertEqual(sum(message['phase'] == 'prompt' for message in messages), 1)

    def test_public_history_is_redacted_and_observer_has_no_read_or_action_authority(self):
        child = self.child()
        event = self.child_event('child-secret-progress', 'message', text='Public progress api_key=syntheticSecret987654321')
        self.assertEqual(self.emit(event)[0], 200)
        self.assertEqual(self.emit(self.child_event('child-secret-done', 'completed', text='Delivered password=syntheticPassword987654321'))[0], 200)
        query = urllib.parse.urlencode({'agent': child['id']})
        route = '/api/office/messages?' + query
        self.assertEqual(self.call(route, observer=self.observer)[0], 403)
        self.assertEqual(self.call(route, reporter=True)[0], 403)
        code, public = self.call(route, owner=True)
        self.assertEqual(code, 200)
        self.assertIn('已遮蓋', json.dumps(public, ensure_ascii=False))
        self.assertNotIn('syntheticSecret', json.dumps(public))
        history_route = '/api/office/history?' + query
        self.assertEqual(self.call(history_route, observer=self.observer)[0], 403)
        code, history = self.call(history_route, owner=True)
        self.assertEqual(code, 200)
        run = history['runs'][0]
        self.assertEqual(run['status'], 'done')
        detail_route = '/api/office/run?' + urllib.parse.urlencode({'id': run['id']})
        self.assertEqual(self.call(detail_route, observer=self.observer)[0], 403)
        code, detail = self.call(detail_route, owner=True)
        self.assertEqual(code, 200)
        self.assertNotIn('syntheticPassword', json.dumps(detail))
        for path, payload in (
            ('/api/office/commands', {'agent_id': child['id'], 'text': 'do not execute'}),
            ('/api/office/seating', {'agent_id': child['id'], 'desk_id': 'gf-main-001'}),
            ('/api/office/agents/logout', {'agent_id': child['id']}),
            ('/api/office/workspaces/assign', {'agent_id': child['id'], 'mode': 'follow'})):
            self.assertEqual(self.call(path, payload, observer=self.observer)[0], 403)
            self.assertEqual(self.call(path, payload, owner=True)[0], 404)

    def test_root_hook_source_is_scoped_and_maps_to_selected_logical_root(self):
        event = {'source_id': OTHER_NATIVE, 'run_id': 'forged-root-run',
                 'event_id': 'forged-root-start', 'type': 'turn_started', 'text': 'Wrong root'}
        self.assertIn(self.call('/api/agents/observers/root-events', event, observer=self.observer)[0], (400, 403, 409))
        run = self.bridge.history.db.execute('SELECT agent_id FROM live_runs WHERE agent_id=?', (ROOT_ID,)).fetchone()
        self.assertIsNotNone(run)
        self.assertEqual(run['agent_id'], ROOT_ID)
        self.assertIsNone(self.bridge.history.db.execute('SELECT 1 FROM live_runs WHERE agent_id=?', ('cursor:' + NATIVE_ROOT,)).fetchone())

    def test_parent_reports_remain_labeled_and_cannot_use_observer_authority(self):
        event = self.child_event('reported-start')
        self.assertEqual(self.call('/api/agents/children/reports', event, observer=self.observer)[0], 403)
        code, receipt = self.call('/api/agents/children/reports', event, reporter=True)
        self.assertEqual(code, 200, receipt)
        children = [agent for agent in self.call('/api/office/work', owner=True)[1]['agents'] if agent.get('is_subagent')]
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]['evidence'], 'parent_reported')

    def test_rebind_invalidates_old_observer_and_old_child_read_access(self):
        old_observer = dict(self.observer)
        child = self.child()
        self.observer = self.register(OTHER_NATIVE)
        self.assertNotEqual(old_observer['generation'], self.observer['generation'])
        self.assertEqual(self.emit(self.child_event('late-old'), observer=old_observer)[0], 403)
        self.assertEqual(self.call('/api/office/messages?' + urllib.parse.urlencode({'agent': child['id']}), owner=True)[0], 404)
        self.root_start('parent-run-two')
        event = self.child_event('fresh-child', parent_run_id='parent-run-two', child_run_id='child-run-two')
        self.assertEqual(self.emit(event)[0], 200)

    def test_logout_revokes_callbacks_and_history_but_keeps_saved_rows(self):
        child = self.child()
        self.assertEqual(self.call('/api/office/agents/logout', {'agent_id': ROOT_ID}, owner=True)[0], 200)
        self.assertEqual(self.emit(self.child_event('late-after-logout', 'completed'))[0], 403)
        self.assertEqual(self.call('/api/agents/children/reports', self.child_event('late-report'), reporter=True)[0], 403)
        self.assertEqual(self.call('/api/office/history?' + urllib.parse.urlencode({'agent': child['id']}), owner=True)[0], 404)
        self.assertIsNotNone(self.bridge.history.db.execute('SELECT 1 FROM runs WHERE agent_id=?', (child['id'],)).fetchone())

    def test_logout_between_header_auth_and_body_commit_rejects_callback(self):
        event = self.child_event('auth-race-child')
        raw = json.dumps(event).encode()
        connection = http.client.HTTPConnection('127.0.0.1', self.bridge.config['port'], timeout=3)
        try:
            connection.putrequest('POST', '/api/agents/children/events')
            for key, value in {'Content-Type': 'application/json', 'Content-Length': str(len(raw)),
                               'X-Observer-Id': self.observer['observer_id'],
                               'Authorization': 'Bearer ' + self.observer['token'],
                               'X-Test-Pause-Body': 'yes'}.items():
                connection.putheader(key, value)
            connection.endheaders()
            self.assertTrue(self.body_ready.wait(2), 'Callback did not reach authenticated body read')
            self.assertEqual(self.call('/api/office/agents/logout', {'agent_id': ROOT_ID}, owner=True)[0], 200)
            connection.send(raw)
            response = connection.getresponse()
            self.assertEqual(response.status, 403, response.read().decode())
            response.read()
            self.assertEqual(self.bridge.history.db.execute('SELECT COUNT(*) FROM observed_agents').fetchone()[0], 0)
        finally:
            connection.close()

    def test_remote_origin_and_invalid_content_are_rejected_without_exposing_text(self):
        event = self.child_event(text='PRIVATE_SYNTHETIC_PAYLOAD')
        self.assertEqual(self.emit(event, headers={'Origin': 'https://attacker.example'})[0], 403)
        code, response = self.emit(event, headers={'Content-Type': 'text/plain'})
        self.assertEqual(code, 400)
        self.assertNotIn('PRIVATE_SYNTHETIC_PAYLOAD', json.dumps(response))


if __name__ == '__main__':
    unittest.main()
