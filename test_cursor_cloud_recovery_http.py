"""Owner HTTP gate for exact legacy Cloud receipt recovery; all native IO is fake."""
from security_keys import owner_key
import json
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from cursor_cloud_recovery import RecoveryError
from dashboard import proxy_handler
import test_cursor_cloud_recovery as recovery_fixtures
import test_platforms as fixtures


class CursorCloudRecoveryHTTPTests(unittest.TestCase):
    connect = fixtures.PlatformTests.connect
    submit = fixtures.PlatformTests.submit
    row = recovery_fixtures.CursorCloudRecoveryTests.row
    attempt = recovery_fixtures.CursorCloudRecoveryTests.attempt

    def setUp(self):
        fixtures.PlatformTests.setUp(self)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), proxy_handler(self.bridge))
        self.bridge.config['port'] = self.server.server_address[1]
        self.url = 'http://127.0.0.1:%s/api/office/connections/cursor/recover' % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        fixtures.PlatformTests.tearDown(self)

    def request(self, body=None, *, owner=True, headers=None, raw=None):
        outgoing = {'Content-Type': 'application/json'}
        if owner:
            outgoing['X-Office-Key'] = owner_key(self.bridge.config)
        outgoing.update(headers or {})
        data = raw if raw is not None else json.dumps(body).encode()
        request = urllib.request.Request(self.url, data=data, headers=outgoing, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=4) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def body(self, apply=False):
        return {'agent_id': fixtures.AGENT, 'native_run_id': recovery_fixtures.NATIVE, 'apply': apply}

    def test_owner_and_origin_required_before_helper_or_observer(self):
        with patch('cursor_cloud_recovery.recover_legacy_run') as helper, patch.object(self.bridge.commands, 'restore_observations') as restore:
            for options in ({'owner': False}, {'headers': {'X-Office-Key': 'wrong-key'}},
                            {'headers': {'Origin': 'https://untrusted.example'}},
                            {'owner': False, 'headers': {'X-Agent-Id': fixtures.AGENT, 'Authorization': 'Bearer agent-only-token'}}):
                with self.subTest(options=options):
                    self.assertEqual(self.request(self.body(True), **options)[0], 403)
            helper.assert_not_called()
            restore.assert_not_called()

    def test_strict_body_and_transport_validation(self):
        invalid = [None, [], {}, {'agent_id': fixtures.AGENT, 'native_run_id': recovery_fixtures.NATIVE},
                   {**self.body(), 'extra': True}, {**self.body(), 'apply': 1},
                   {**self.body(), 'apply': 'true'}, {**self.body(), 'agent_id': 12},
                   {**self.body(), 'native_run_id': None}]
        with patch('cursor_cloud_recovery.recover_legacy_run') as helper:
            for body in invalid:
                with self.subTest(body=body):
                    self.assertEqual(self.request(body)[0], 400)
            self.assertEqual(self.request(raw=b'{not valid JSON')[0], 400)
            self.assertEqual(self.request(self.body(), headers={'Content-Type': 'text/plain'})[0], 400)
            self.assertEqual(self.request(self.body(), headers={'Transfer-Encoding': 'chunked'})[0], 400)
            self.assertEqual(self.request(raw=b' ' * 4097)[0], 400)
            helper.assert_not_called()

    def test_real_helper_dry_run_and_apply_share_running_commands(self):
        command, remote = self.attempt()
        # connect_cursor refreshes the Bridge config from the fixture file.
        self.bridge.config['port'] = self.server.server_address[1]
        self.bridge.commands.adapters['cursor_cloud'].transport = remote
        with patch.object(self.bridge.commands, 'restore_observations') as restore:
            status, dry = self.request(self.body())
            self.assertEqual(status, 200)
            self.assertTrue(dry['verified'])
            self.assertFalse(dry['applied'])
            self.assertIsNone(self.row(command)['native_message_id'])
            restore.assert_not_called()
            status, applied = self.request(self.body(True))
            self.assertEqual(status, 200)
            self.assertTrue(applied['applied'])
            self.assertEqual(self.row(command)['native_message_id'], recovery_fixtures.NATIVE)
            self.assertEqual(self.row(command)['state'], 'delivery_unknown')
            restore.assert_called_once_with()
            restore.reset_mock()
            status, duplicate = self.request(self.body())
            self.assertEqual(status, 200)
            self.assertTrue(duplicate['duplicate'])
            restore.assert_not_called()
            self.assertEqual(self.request(self.body(True))[0], 200)
            restore.assert_called_once_with()
        self.assertTrue(all(method == 'GET' for method, _ in remote.calls))
        self.assertNotIn('user prompt', json.dumps(applied))

    def test_fixed_helper_error_does_not_resume_or_expose_internal_error(self):
        with patch('cursor_cloud_recovery.recover_legacy_run', side_effect=RecoveryError('cursor_recovery_prompt_mismatch')) as helper, patch.object(self.bridge.commands, 'restore_observations') as restore:
            self.assertEqual(self.request(self.body(True)), (409, {'error': 'cursor_recovery_prompt_mismatch'}))
            helper.assert_called_once_with(self.bridge.commands, fixtures.AGENT, recovery_fixtures.NATIVE, apply=True)
            restore.assert_not_called()


if __name__ == '__main__':
    unittest.main()
