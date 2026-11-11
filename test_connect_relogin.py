"""A revoked or missing inbox credential must never be silently revived."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.error
from unittest.mock import Mock, patch


CONNECT_PATH = Path(__file__).parent / 'adapters/connect.py'
SPEC = importlib.util.spec_from_file_location('_office_connect_relogin', CONNECT_PATH)
connect = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(connect)


class ConnectReloginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.path = self.folder / 'credential.json'

    def tearDown(self):
        self.temp.cleanup()

    def credential(self, provider='cursor'):
        return {'agent_id': provider + ':fixture',
                'endpoint': 'http://127.0.0.1:4318/api/agents/inbox',
                'token': 'fixture-private-token-never-printed'}

    def invoke_missing(self, action='auto'):
        arguments = ['connect.py', '--credential', str(self.path), action]
        if action == 'cursor-children':
            arguments += ['--hooks-file', str(self.folder / 'hooks.json')]
        output = io.StringIO()
        with patch.object(sys, 'argv', arguments), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as stopped:
                connect.main()
        self.assertEqual(stopped.exception.code, 1)
        return json.loads(output.getvalue()), output.getvalue()

    def test_logged_out_marker_is_metadata_only_and_never_a_credential_fallback(self):
        retired = self.folder / ('credential.logged-out-' + 'a' * 32 + '.json')
        retired.write_text('RETIRED_PRIVATE_CONTENT_NOT_JSON')
        original_read = Path.read_text
        reads = []

        def selected_read(path, *args, **kwargs):
            reads.append(path)
            if path == retired:
                self.fail('Retired credential content must never be read')
            return original_read(path, *args, **kwargs)

        with patch.object(Path, 'read_text', selected_read), patch.object(connect, 'native_request') as native, patch.object(connect, 'cursor_children') as children:
            result, output = self.invoke_missing()
        self.assertEqual(reads, [self.path])
        self.assertEqual(result['reason'], 'credential_logged_out')
        self.assertTrue(result['requires_rejoin'])
        self.assertFalse(result['connected'])
        self.assertFalse(result['persistent'])
        self.assertFalse(result['child_observer_connected'])
        self.assertNotIn('RETIRED_PRIVATE_CONTENT', output)
        self.assertNotIn(str(self.folder), output)
        self.assertTrue(retired.exists())
        self.assertFalse(self.path.exists())
        native.assert_not_called()
        children.assert_not_called()

    def test_plain_missing_credential_is_distinct_from_logout(self):
        result, _ = self.invoke_missing()
        self.assertEqual(result['reason'], 'credential_missing')
        self.assertTrue(result['requires_rejoin'])

    def test_only_exact_regular_logout_marker_names_count(self):
        (self.folder / 'credential.logged-out-not-a-uuid.json').write_text('ignored')
        (self.folder / ('credential.logged-out-' + 'b' * 32 + '.json')).mkdir()
        (self.folder / ('credential.logged-out-' + 'c' * 32 + '.json')).symlink_to(self.folder / 'elsewhere.json')
        result, _ = self.invoke_missing()
        self.assertEqual(result['reason'], 'credential_missing')

    def test_cursor_setup_with_missing_credential_never_installs_hooks(self):
        with patch.object(connect, 'cursor_children') as installer, patch.object(connect, 'native_request') as native:
            result, _ = self.invoke_missing('cursor-children')
        self.assertEqual(result['reason'], 'credential_missing')
        installer.assert_not_called()
        native.assert_not_called()
        self.assertFalse((self.folder / 'hooks.json').exists())

    def test_cursor_auto_reports_background_limit_without_platform_probes(self):
        with patch.object(connect, 'native_request') as native:
            result = connect.auto_connect(self.credential(), {'CODEX_THREAD_ID': 'b' * 32})
        self.assertFalse(result['connected'])
        self.assertFalse(result['persistent'])
        self.assertFalse(result['requires_rejoin'])
        self.assertEqual(result['reason'], 'cursor_local_background_unavailable')
        self.assertTrue(result['observation_setup_separate'])
        native.assert_not_called()

    def test_valid_codex_flow_keeps_exact_native_session_and_workspace(self):
        sid = '3e4d068d-c266-42d3-8ae5-1b7b85b21037'
        credential = self.credential('codex')
        with patch.object(connect, 'native_request', return_value={'connected': True}) as native:
            result = connect.auto_connect(credential, {'CODEX_THREAD_ID': sid})
        self.assertEqual(result, {'connected': True})
        native.assert_called_once_with(credential, {'adapter': 'codex_app', 'session_id': sid,
                                                   'workspace_path': str(Path.cwd())})

    def test_existing_current_credential_wins_over_old_logout_marker(self):
        self.path.write_text(json.dumps(self.credential()))
        marker = self.folder / ('credential.logged-out-' + 'd' * 32 + '.json')
        marker.write_text('RETIRED_NEVER_READ')
        arguments = ['connect.py', '--credential', str(self.path), 'auto']
        output = io.StringIO()
        with patch.object(sys, 'argv', arguments), patch.object(connect, 'missing_credential_result') as missing, contextlib.redirect_stdout(output):
            connect.main()
        result = json.loads(output.getvalue())
        self.assertEqual(result['reason'], 'cursor_local_background_unavailable')
        self.assertFalse(result['requires_rejoin'])
        self.assertNotIn(self.credential()['token'], output.getvalue())
        missing.assert_not_called()

    def test_missing_cli_reports_structured_stdout_and_exit_one(self):
        result = subprocess.run([sys.executable, str(CONNECT_PATH), '--credential', str(self.path), 'auto'],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, '')
        body = json.loads(result.stdout)
        self.assertEqual(body['reason'], 'credential_missing')
        self.assertTrue(body['requires_rejoin'])
        self.assertNotIn(str(self.folder), result.stdout)

    def test_other_provider_auto_behavior_is_unchanged(self):
        result = connect.auto_connect(self.credential('grok_bot'), {})
        self.assertEqual(result, {'connected': False, 'reason': 'native_setup_required'})

    def http_failure(self, status, reason='invalid_agent_credential'):
        return urllib.error.HTTPError('http://127.0.0.1:4318/api/agents/connect', status,
            'fixture rejection', {}, io.BytesIO(json.dumps({'error': reason}).encode()))

    def test_native_auth_rejections_require_rejoin_and_preserve_server_reason(self):
        for status in (401, 403, 410):
            with self.subTest(status=status):
                opener = Mock()
                opener.open.side_effect = self.http_failure(status)
                with patch.object(connect.urllib.request, 'build_opener', return_value=opener):
                    result = connect.native_request(self.credential('codex'), {'adapter': 'codex_app'})
                self.assertEqual(result['reason'], 'invalid_agent_credential')
                self.assertEqual(result['status'], status)
                self.assertFalse(result['connected'])
                self.assertFalse(result['persistent'])
                self.assertTrue(result['requires_rejoin'])
                self.assertEqual(result['next_step'], connect.missing_credential_result(self.path)['next_step'])
                self.assertNotIn(self.credential()['token'], json.dumps(result))
                opener.open.assert_called_once()

    def test_cursor_auth_rejections_never_install_hooks_or_try_another_route(self):
        credential = dict(self.credential(), endpoint='http://127.0.0.1:4318/api/agents/events')
        installer = types.ModuleType('install_cursor_children')
        installer.install = Mock()
        for status in (401, 403, 410):
            with self.subTest(status=status):
                opener = Mock()
                opener.open.side_effect = self.http_failure(status)
                with patch.object(connect.urllib.request, 'build_opener', return_value=opener), patch.dict(sys.modules, {'install_cursor_children': installer}):
                    result = connect.cursor_children(credential, str(self.folder / 'hooks.json'), str(self.path))
                self.assertEqual(result['reason'], 'observer_setup_rejected')
                self.assertEqual(result['status'], status)
                self.assertFalse(result['child_observer_connected'])
                self.assertFalse(result['persistent'])
                self.assertTrue(result['requires_rejoin'])
                self.assertNotIn(self.credential()['token'], json.dumps(result))
                installer.install.assert_not_called()
                opener.open.assert_called_once()
                request = opener.open.call_args.args[0]
                self.assertTrue(request.full_url.endswith('/api/agents/observers/pending'))
        self.assertFalse((self.folder / 'hooks.json').exists())

    def test_other_http_failures_do_not_infer_revocation_or_rejoin(self):
        credential = dict(self.credential(), endpoint='http://127.0.0.1:4318/api/agents/events')
        for status in (409, 429, 500, 503):
            for action in ('native', 'cursor'):
                with self.subTest(status=status, action=action):
                    opener = Mock()
                    opener.open.side_effect = self.http_failure(status, 'temporary_unavailable')
                    with patch.object(connect.urllib.request, 'build_opener', return_value=opener):
                        result = (connect.native_request(credential, {}) if action == 'native'
                            else connect.cursor_children(credential, str(self.folder / 'hooks.json')))
                    self.assertNotIn('requires_rejoin', result)
                    self.assertNotIn('next_step', result)
                    if action == 'native':
                        self.assertEqual(result, {'connected': False, 'reason': 'temporary_unavailable'})
                    else:
                        self.assertEqual(result, {'child_observer_connected': False, 'reason': 'observer_setup_rejected', 'status': status})
                    opener.open.assert_called_once()

    def test_transport_errors_never_infer_logout_or_probe_another_route(self):
        credential = dict(self.credential(), endpoint='http://127.0.0.1:4318/api/agents/events')
        for action in ('native', 'cursor'):
            with self.subTest(action=action):
                opener = Mock()
                opener.open.side_effect = urllib.error.URLError('fixture offline')
                with patch.object(connect.urllib.request, 'build_opener', return_value=opener), self.assertRaises(urllib.error.URLError):
                    if action == 'native':
                        connect.native_request(credential, {})
                    else:
                        connect.cursor_children(credential, str(self.folder / 'hooks.json'))
                opener.open.assert_called_once()
        self.assertFalse((self.folder / 'hooks.json').exists())

    def test_readable_but_rejected_credential_cli_reports_rejoin_without_secret(self):
        credential = dict(self.credential(), endpoint='http://127.0.0.1:4318/api/agents/events')
        self.path.write_text(json.dumps(credential))
        opener = Mock()
        opener.open.side_effect = self.http_failure(403)
        arguments = ['connect.py', '--credential', str(self.path), 'cursor-children',
                     '--hooks-file', str(self.folder / 'hooks.json')]
        output = io.StringIO()
        with patch.object(sys, 'argv', arguments), patch.object(connect.urllib.request, 'build_opener', return_value=opener), contextlib.redirect_stdout(output):
            connect.main()
        result = json.loads(output.getvalue())
        self.assertEqual(result['reason'], 'observer_setup_rejected')
        self.assertEqual(result['status'], 403)
        self.assertTrue(result['requires_rejoin'])
        self.assertNotIn(credential['token'], output.getvalue())
        self.assertNotIn(str(self.folder), output.getvalue())
        self.assertFalse((self.folder / 'hooks.json').exists())


if __name__ == '__main__':
    unittest.main()
