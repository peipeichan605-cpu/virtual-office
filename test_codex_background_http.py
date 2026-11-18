"""Owner HTTP routes use stub registration; no CLI process or model inference."""
from security_keys import owner_key
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer

from configuration import initialize, load_config
from connections import ConnectionError
from dashboard import Bridge, proxy_handler


STATUS = '/api/office/codex/background/status'
CREATE = '/api/office/codex/background/create'


class CodexBackgroundHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        path = self.root / 'config.json'
        initialize(path)
        config = load_config(path)
        config.update(run_id='background-http-' + uuid.uuid4().hex, run_dir=str(self.root / 'run'))
        self.registration = Mock(spec=['create', 'status', 'update_model'])
        self.registration.status.return_value = {
            'mode': 'codex_background', 'available': True,
            'conversation_scope': 'office_owned_session', 'tool_policy': 'read_only',
        }
        self.registration.create.return_value = {
            'agent_id': 'codex:bg-00000000-0000-4000-8000-000000000032',
            'registration_state': 'pending_first_instruction',
            'native_target_verified': False, 'execution_verified': False,
        }
        # Route coverage must not discover an installed CLI or instantiate a
        # real session lifecycle. The constructor receives this same stub.
        with patch('commands.make_adapters', return_value={}), \
                patch('codex_background_connections.CodexBackgroundConnections', return_value=self.registration):
            self.bridge = Bridge(config)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), proxy_handler(self.bridge))
        self.bridge.config['port'] = self.server.server_address[1]
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.bridge.commands.close()
        self.bridge.history.close()
        self.temp.cleanup()

    def request(self, route, body=None, *, owner=True, headers=None, raw=None, method=None):
        outgoing = {'Content-Type': 'application/json'}
        if owner:
            outgoing['X-Office-Key'] = owner_key(self.bridge.config)
        outgoing.update(headers or {})
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + route, data=data, headers=outgoing, method=method)
        try:
            with urllib.request.urlopen(request, timeout=4) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def body(self):
        return {'request_id': str(uuid.uuid4()), 'name': 'Background fixture', 'workspace': str(self.root)}

    def test_owner_and_same_origin_required_before_registration_or_status(self):
        denied = [
            {'owner': False},
            {'headers': {'X-Office-Key': 'wrong-owner-key'}},
            {'headers': {'Origin': 'https://untrusted.example'}},
            {'headers': {'Origin': 'null'}},
            {'owner': False, 'headers': {'X-Agent-Id': 'codex:fixture',
                                         'Authorization': 'Bearer agent-only-fixture'}},
        ]
        for options in denied:
            with self.subTest(options=options):
                self.assertEqual(self.request(STATUS, **options)[0], 403)
                self.assertEqual(self.request(CREATE, self.body(), **options)[0], 403)
                self.assertEqual(self.request('/api/office/codex/background/model', self.body(), **options)[0], 403)
        self.registration.status.assert_not_called()
        self.registration.create.assert_not_called()
        self.assertEqual(self.bridge.commands.db.execute('SELECT COUNT(*) FROM commands').fetchone()[0], 0)

    def test_model_settings_forward_only_owner_requests_and_optional_agent_status(self):
        agent = 'codex:bg-' + str(uuid.uuid4())
        body = {'request_id': str(uuid.uuid4()), 'agent_id': agent, 'model': 'fixture-model'}
        self.registration.update_model.return_value = {'updated': True, 'execution_verified': False}
        code, result = self.request('/api/office/codex/background/model', body)
        self.assertEqual(code, 200)
        self.assertFalse(result['execution_verified'])
        self.registration.update_model.assert_called_once_with(body)
        self.assertEqual(self.request(STATUS + '?agent_id=' + agent)[0], 200)
        self.registration.status.assert_called_with(None, agent_id=agent)
        self.registration.status.reset_mock()
        self.assertEqual(self.request(STATUS + '?agent_id=' + agent + '&agent_id=' + agent)[0], 400)
        self.registration.status.assert_not_called()

    def test_status_and_create_forward_exact_request_and_do_not_claim_execution(self):
        for origin in (self.base, 'http://localhost:' + str(self.server.server_address[1])):
            with self.subTest(origin=origin):
                code, status = self.request(STATUS, headers={'Origin': origin})
                self.assertEqual(code, 200)
                self.assertEqual(status, self.registration.status.return_value)
        self.registration.status.assert_called_with(None)
        request_id = str(uuid.uuid4())
        self.assertEqual(self.request(STATUS + '?request_id=' + request_id)[0], 200)
        self.registration.status.assert_called_with(request_id)
        body = self.body()
        code, receipt = self.request(CREATE, body, headers={'Origin': self.base})
        self.assertEqual(code, 200)
        self.registration.create.assert_called_once_with(body)
        self.assertFalse(receipt['native_target_verified'])
        self.assertFalse(receipt['execution_verified'])
        self.assertEqual(receipt['registration_state'], 'pending_first_instruction')
        self.assertEqual(self.bridge.commands.db.execute('SELECT COUNT(*) FROM commands').fetchone()[0], 0)

    def test_invalid_json_content_type_size_and_transfer_encoding_never_reach_create(self):
        invalid = [
            {'raw': b'{not valid JSON'},
            {'raw': b''},
            {'raw': b' ' * 16001},
            {'body': self.body(), 'headers': {'Content-Type': 'text/plain'}},
            {'body': self.body(), 'headers': {'Transfer-Encoding': 'chunked'}},
            {'body': self.body(), 'headers': {'Content-Length': 'invalid'}},
        ]
        for options in invalid:
            with self.subTest(options=options.keys()):
                self.assertEqual(self.request(CREATE, method='POST', **options)[0], 400)
        self.registration.create.assert_not_called()

    def test_unexpected_or_repeated_query_keys_never_reach_status(self):
        request_id = str(uuid.uuid4())
        for query in ('?agent=other', '?request_id=' + request_id + '&extra=true',
                      '?request_id=' + request_id + '&request_id=' + request_id):
            with self.subTest(query=query):
                self.assertEqual(self.request(STATUS + query)[0], 400)
        self.registration.status.assert_not_called()

    def test_registration_validation_errors_are_bounded_http_errors(self):
        self.registration.create.side_effect = ConnectionError('invalid_workspace', 400)
        self.assertEqual(self.request(CREATE, self.body()), (400, {'error': 'invalid_workspace'}))
        self.registration.status.side_effect = ConnectionError('invalid_request_id', 400)
        self.assertEqual(self.request(STATUS + '?request_id=not-a-uuid'),
                         (400, {'error': 'invalid_request_id'}))

    def test_os_errors_do_not_leak_native_details(self):
        self.registration.create.side_effect = OSError('private-native-error-fixture')
        self.registration.status.side_effect = OSError('private-native-error-fixture')
        for route, body in ((CREATE, self.body()), (STATUS, None)):
            with self.subTest(route=route):
                code, value = self.request(route, body)
                self.assertEqual(code, 503)
                self.assertEqual(value, {'error': 'connection_unavailable'})
                self.assertNotIn('private-native-error-fixture', json.dumps(value))


if __name__ == '__main__':
    unittest.main()
