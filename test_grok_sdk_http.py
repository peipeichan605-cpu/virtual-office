"""Grok SDK owner HTTP boundaries, with no account or SDK process access."""
from security_keys import owner_key
from http.server import ThreadingHTTPServer
import json
import sqlite3
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import urllib.error
import urllib.request
import uuid

from connections import ConnectionError
from dashboard import ORIGIN, proxy_handler


BASE = '/api/office/grok/sdk'
BOT = '00000000-0000-4000-8000-000000000041'
LOGIN = '00000000-0000-4000-8000-000000000042'


class GrokSdkHTTPTests(unittest.TestCase):
    def setUp(self):
        self.service = Mock(spec=['status', 'start_login', 'login_status',
                                 'cancel_login', 'bots', 'sessions', 'connect'])
        self.service.status.return_value = {
            'mode': 'grok_sdk', 'experimental': True,
            'auth': {'state': 'logged_out'}, 'execution_verified': False,
        }
        self.service.start_login.return_value = {
            'login_id': LOGIN, 'state': 'pending',
            'login_url': 'https://cursor.com/loginDeepControl?challenge=fixture',
        }
        self.service.login_status.return_value = {'login_id': LOGIN, 'state': 'pending'}
        self.service.cancel_login.return_value = {'login_id': LOGIN, 'state': 'cancelled'}
        self.service.bots.return_value = {'bots': [{'id': BOT, 'name': 'Selected fixture'}],
                                          'truncated': False}
        self.service.sessions.return_value = {'sessions': [{'session_id': '', 'kind': 'main'}]}
        self.service.connect.return_value = {
            'agent_id': 'grok_bot:sdk-00000000-0000-4000-8000-000000000043',
            'mode': 'grok_sdk', 'same_session': True,
            'registration_state': 'configured', 'execution_verified': False,
        }
        # Exercise the real HTTP handler, while removing all account/runtime
        # discovery and model execution from this transport-boundary test.
        self.bridge = SimpleNamespace(
            config={'topic': 'fixture-office-owner-key', 'port': 4318},
            grok_sdk_connections=self.service, agents={},
            commands=Mock(spec=['create']),
        )
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), proxy_handler(self.bridge))
        self.server.daemon_threads = True
        self.bridge.config['port'] = self.server.server_address[1]
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, route, body=None, *, owner=True, headers=None, raw=None, method=None):
        outgoing = {'Content-Type': 'application/json'}
        if owner:
            outgoing['X-Office-Key'] = owner_key(self.bridge.config)
        outgoing.update(headers or {})
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + route, data=data, headers=outgoing, method=method)
        try:
            with urllib.request.urlopen(request, timeout=4) as response:
                return response.status, json.load(response), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error), dict(error.headers)

    def connect_body(self):
        # Empty string is a real, explicitly selected native main-chat ID.
        return {'request_id': str(uuid.uuid4()), 'bot_id': BOT,
                'session_id': '', 'name': 'Selected fixture'}

    def all_routes(self):
        return [
            (BASE + '/status', None),
            (BASE + '/login/status?login_id=' + LOGIN, None),
            (BASE + '/bots', None),
            (BASE + '/sessions?bot_id=' + BOT, None),
            (BASE + '/login', {'request_id': str(uuid.uuid4())}),
            (BASE + '/login/cancel', {'login_id': LOGIN}),
            (BASE + '/connect', self.connect_body()),
        ]

    def test_all_account_and_connection_routes_require_owner_and_local_origin(self):
        denied = [
            {'owner': False},
            {'headers': {'X-Office-Key': 'wrong-owner-key'}},
            {'headers': {'Origin': 'https://untrusted.example'}},
            {'headers': {'Origin': 'null'}},
            # Public wallpaper origin may read a feed, never account setup.
            {'headers': {'Origin': ORIGIN}},
            {'headers': {'Host': 'untrusted.example'}},
            {'owner': False, 'headers': {'X-Agent-Id': 'grok_bot:fixture',
                                         'Authorization': 'Bearer agent-only-fixture'}},
        ]
        for route, body in self.all_routes():
            for options in denied:
                with self.subTest(route=route, options=options):
                    self.assertEqual(self.request(route, body, **options)[0], 403)
        self.assertEqual(self.service.mock_calls, [])
        self.bridge.commands.create.assert_not_called()

    def test_status_and_recovery_reads_forward_only_the_named_request_ids(self):
        for origin in (self.base, 'http://localhost:' + str(self.bridge.config['port'])):
            with self.subTest(origin=origin):
                code, result, headers = self.request(BASE + '/status', headers={'Origin': origin})
                self.assertEqual((code, result), (200, self.service.status.return_value))
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
        self.service.status.assert_called_with(request_id=None, login_request_id=None)
        request_id, login_request = str(uuid.uuid4()), str(uuid.uuid4())
        code, _, _ = self.request(BASE + '/status?request_id=' + request_id)
        self.assertEqual(code, 200)
        self.service.status.assert_called_with(request_id=request_id, login_request_id=None)
        code, _, _ = self.request(BASE + '/status?login_request_id=' + login_request)
        self.assertEqual(code, 200)
        self.service.status.assert_called_with(request_id=None, login_request_id=login_request)
        self.service.start_login.assert_not_called()
        self.service.connect.assert_not_called()

    def test_login_begin_poll_and_cancel_have_separate_explicit_routes(self):
        body = {'request_id': str(uuid.uuid4())}
        code, login, _ = self.request(BASE + '/login', body)
        self.assertEqual(code, 200)
        self.assertEqual(login, self.service.start_login.return_value)
        self.service.start_login.assert_called_once_with(body)
        code, state, _ = self.request(BASE + '/login/status?login_id=' + LOGIN)
        self.assertEqual((code, state['state']), (200, 'pending'))
        self.service.login_status.assert_called_once_with(LOGIN)
        body = {'login_id': LOGIN}
        code, state, _ = self.request(BASE + '/login/cancel', body)
        self.assertEqual((code, state['state']), (200, 'cancelled'))
        self.service.cancel_login.assert_called_once_with(body)
        self.service.connect.assert_not_called()
        self.bridge.commands.create.assert_not_called()

    def test_roster_and_session_reads_do_not_enroll_or_send(self):
        code, roster, _ = self.request(BASE + '/bots')
        self.assertEqual((code, roster), (200, self.service.bots.return_value))
        self.service.bots.assert_called_once_with()
        code, sessions, _ = self.request(BASE + '/sessions?bot_id=' + BOT)
        self.assertEqual((code, sessions), (200, self.service.sessions.return_value))
        self.service.sessions.assert_called_once_with(BOT)
        self.service.connect.assert_not_called()
        self.service.start_login.assert_not_called()
        self.bridge.commands.create.assert_not_called()

    def test_explicit_main_chat_selection_is_forwarded_without_claiming_execution(self):
        body = self.connect_body()
        code, result, _ = self.request(BASE + '/connect', body)
        self.assertEqual(code, 200)
        self.service.connect.assert_called_once_with(body)
        self.assertEqual(body['session_id'], '')
        self.assertEqual(result['registration_state'], 'configured')
        self.assertFalse(result['execution_verified'])
        self.bridge.commands.create.assert_not_called()

    def test_invalid_json_content_type_size_and_transfer_encoding_are_rejected_before_service(self):
        malformed = [
            {'raw': b'{invalid'}, {'raw': b''}, {'raw': b' ' * 16001},
            {'body': {}, 'headers': {'Content-Type': 'text/plain'}},
            # An explicit length prevents urllib from racing a second chunk
            # write against the server's immediate framing rejection.
            {'body': {}, 'headers': {'Transfer-Encoding': 'chunked', 'Content-Length': '2'}},
            {'body': {}, 'headers': {'Content-Length': 'invalid'}},
            {'body': {}, 'headers': {'Content-Length': '-1'}},
        ]
        for suffix in ('/login', '/login/cancel', '/connect'):
            for options in malformed:
                with self.subTest(suffix=suffix, options=list(options)):
                    code, result, _ = self.request(BASE + suffix, method='POST', **options)
                    self.assertEqual(code, 400)
                    self.assertEqual(result, {'error': 'invalid_grok_sdk_request'})
        self.assertEqual(self.service.mock_calls, [])

    def test_missing_extra_and_duplicate_query_arguments_never_reach_service(self):
        queries = [
            '/status?unexpected=1', '/status?request_id=a&request_id=b',
            '/status?login_request_id=a&login_request_id=b',
            '/login/status', '/login/status?bot_id=' + BOT,
            '/login/status?login_id=a&login_id=b', '/login/status?login_id=a&extra=1',
            '/bots?bot_id=' + BOT, '/bots?extra=',
            '/sessions', '/sessions?session_id=a',
            '/sessions?bot_id=a&bot_id=b', '/sessions?bot_id=a&extra=1',
        ]
        for query in queries:
            with self.subTest(query=query):
                self.assertEqual(self.request(BASE + query)[0], 400)
        self.assertEqual(self.service.mock_calls, [])
        self.assertEqual(self.request(BASE + '/not-a-route')[0], 404)
        self.assertEqual(self.service.mock_calls, [])

    def test_backend_validation_errors_preserve_bounded_codes(self):
        self.service.sessions.side_effect = ConnectionError('invalid_grok_sdk_bot', 400)
        code, result, _ = self.request(BASE + '/sessions?bot_id=invalid')
        self.assertEqual((code, result), (400, {'error': 'invalid_grok_sdk_bot'}))
        self.service.connect.side_effect = ConnectionError('native_session_already_selected', 409)
        code, result, _ = self.request(BASE + '/connect', self.connect_body())
        self.assertEqual((code, result), (409, {'error': 'native_session_already_selected'}))

    def test_os_and_database_failures_do_not_expose_account_details(self):
        for error in (OSError('fixture-account-token-must-not-leak'),
                      sqlite3.OperationalError('fixture-private-path-must-not-leak')):
            for method in ('status', 'start_login', 'login_status', 'cancel_login',
                           'bots', 'sessions', 'connect'):
                getattr(self.service, method).side_effect = error
            for route, body in self.all_routes():
                with self.subTest(error=type(error).__name__, route=route):
                    code, result, _ = self.request(route, body)
                    self.assertEqual((code, result), (503, {'error': 'grok_sdk_unavailable'}))
                    self.assertNotIn('fixture-', json.dumps(result))

    def test_run_detail_preserves_sdk_history_gap_warning(self):
        agent = 'grok_bot:sdk-00000000-0000-4000-8000-000000000043'
        item = {'id': 'run-fixture', 'agent_id': agent, 'run_id': 'native-fixture',
                'status': 'done', 'delivery': 'A bounded public result'}
        self.bridge.commands.lock = threading.RLock()
        self.bridge.history = SimpleNamespace(lock=threading.RLock(),
                                              detail=Mock(return_value=item))
        self.bridge.history_agents = lambda: {agent: {'id': agent, 'name': 'Selected fixture'}}
        self.bridge.cursor_cloud_sync = Mock(spec=['decorate_run'])
        self.bridge.cursor_cloud_sync.decorate_run.side_effect = lambda run: run
        self.bridge.grok_sdk_sync = Mock(spec=['decorate_run'])
        self.bridge.grok_sdk_sync.decorate_run.side_effect = lambda run: dict(run, sync_warning='public_body_missing')
        code, result, _ = self.request('/api/office/run?id=run-fixture')
        self.assertEqual(code, 200)
        self.assertEqual(result['delivery'], item['delivery'])
        self.assertEqual(result.get('sync_warning'), 'public_body_missing')
        self.bridge.grok_sdk_sync.decorate_run.assert_called_once_with(item)
        self.bridge.cursor_cloud_sync.decorate_run.assert_called_once_with(item)
        self.bridge.history.detail.return_value = None
        self.bridge.grok_sdk_sync.decorate_run.reset_mock()
        self.assertEqual(self.request('/api/office/run?id=absent')[0], 404)
        self.bridge.grok_sdk_sync.decorate_run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
