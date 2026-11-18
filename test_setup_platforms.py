import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cursor_sdk_setup
import setup_platforms as platforms


def ready():
    return platforms._result(True, None, 'review_login_in_office')


class PlatformSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='office-platforms-')
        self.root = Path(self.temporary.name)
        self.config = {'data_dir': str(self.root / 'office'),
                       'agents': [{'id': 'generic:existing', 'name': 'Existing'}]}

    def tearDown(self):
        self.temporary.cleanup()

    def test_check_requires_managed_cursor_environment_even_if_calling_python_has_sdk(self):
        with patch.object(cursor_sdk_setup, 'probe_interpreter') as probe:
            result = platforms.check_cursor(self.config)
        self.assertEqual(result['reason'], 'cursor_sdk_required')
        self.assertFalse(result['ready']); probe.assert_not_called()
        self.assertFalse(Path(self.config['data_dir']).exists())

    def test_cursor_check_probes_selected_managed_environment_and_its_pinned_import(self):
        runtime = cursor_sdk_setup.runtime_root(self.config)
        environment = runtime / 'environments' / ('env-' + 'a' * 32)
        environment.mkdir(parents=True)
        (environment / 'pyvenv.cfg').write_text('fixture')
        (runtime / 'runtime.json').write_text(json.dumps({
            'managed_by': cursor_sdk_setup.MANAGER, 'sdk_version': '1.3.1',
            'environment': environment.relative_to(runtime).as_posix()}))
        with patch.object(cursor_sdk_setup, 'probe_interpreter', return_value={'version': [3, 12, 0]}) as probe:
            result = platforms.check_cursor(self.config)
        probe.assert_called_once_with(cursor_sdk_setup.venv_python(environment), sdk=True)
        self.assertTrue(result['ready']); self.assertEqual(result['sdk_version'], '1.3.1')
        with patch.object(cursor_sdk_setup, 'probe_interpreter', return_value=None):
            self.assertEqual(platforms.check_cursor(self.config)['reason'], 'cursor_sdk_environment_unavailable')

    def test_readonly_check_never_installs_or_mutates_configuration_and_missing_platform_fails_readiness(self):
        before = copy.deepcopy(self.config)
        missing = platforms._result(False, 'grok_sdk_runtime_required', 'install_official_node_22_13_or_newer')
        with patch.object(cursor_sdk_setup, 'setup_environment') as install, \
             patch.object(platforms, 'check_cursor', return_value=ready()), \
             patch.object(platforms, 'check_grok', return_value=missing), \
             patch.object(platforms, 'check_codex', return_value=ready()):
            result = platforms.prepare_platforms(self.config, check=True)
        install.assert_not_called(); self.assertEqual(self.config, before)
        self.assertFalse(result['runtimes_ready']); self.assertEqual(result['mode'], 'check')
        self.assertFalse(result['accounts_checked']); self.assertFalse(result['service_restarted'])
        self.assertFalse(result['native_conversations_created'])
        for platform in result['platforms'].values():
            self.assertEqual(platform['login'], {'status': 'not_checked', 'next_step': 'user_action_required'})

    def test_default_prepares_cursor_before_runtime_checks_and_does_not_restart_or_enroll(self):
        order = []; started = Mock(); before = copy.deepcopy(self.config)
        def install(*args, **kwargs):
            order.append('install'); kwargs['on_started']({'run_id': 'setup-fixture'})
        def check(name):
            def callback(config): order.append(name); return ready()
            return callback
        with patch.object(cursor_sdk_setup, 'setup_environment', side_effect=install) as installer, \
             patch.object(platforms, 'check_cursor', side_effect=check('cursor')), \
             patch.object(platforms, 'check_grok', side_effect=check('grok')), \
             patch.object(platforms, 'check_codex', side_effect=check('codex')):
            result = platforms.prepare_platforms(self.config, python='/existing/python', on_started=started)
        self.assertEqual(order, ['install', 'cursor', 'grok', 'codex'])
        installer.assert_called_once_with(self.config, '/existing/python', False, on_started=started)
        started.assert_called_once_with({'run_id': 'setup-fixture'})
        self.assertTrue(result['runtimes_ready']); self.assertEqual(self.config, before)

    def test_failed_install_still_reports_other_platforms_and_successful_retry_clears_failure(self):
        with patch.object(cursor_sdk_setup, 'setup_environment', side_effect=[RuntimeError('private log /key'), None]), \
             patch.object(platforms, 'check_cursor', return_value=ready()), \
             patch.object(platforms, 'check_grok', return_value=ready()) as grok, \
             patch.object(platforms, 'check_codex', return_value=ready()) as codex:
            failed = platforms.prepare_platforms(self.config)
            completed = platforms.prepare_platforms(self.config)
        self.assertFalse(failed['runtimes_ready'])
        self.assertEqual(failed['platforms']['cursor']['reason'], 'cursor_sdk_setup_failed')
        self.assertNotIn('private log', json.dumps(failed)); self.assertNotIn('/key', json.dumps(failed))
        self.assertEqual(grok.call_count, 2); self.assertEqual(codex.call_count, 2)
        self.assertTrue(completed['runtimes_ready']); self.assertIsNone(completed['platforms']['cursor']['reason'])

    def test_check_rejects_installation_options_before_any_runtime_action(self):
        with patch.object(cursor_sdk_setup, 'setup_environment') as install, \
             patch.object(platforms, 'check_cursor') as cursor:
            for args in ({'python': '/some/python'}, {'download_python': True}):
                with self.assertRaisesRegex(ValueError, '--check cannot'):
                    platforms.prepare_platforms(self.config, check=True, **args)
        install.assert_not_called(); cursor.assert_not_called()

    def test_early_install_failure_has_actionable_reason_without_requiring_a_log(self):
        errors = [
            (ValueError('A healthy Python 3.10+ is needed (Windows requires x64 Python).'), 'cursor_python_required'),
            (ValueError('Install Python 3.10+ or uv, then run setup-cursor-sdk again.'), 'cursor_python_download_tool_required'),
            (ValueError('Cursor SDK runtime metadata is not owned by this installer; existing files preserved.'), 'cursor_sdk_runtime_metadata_conflict'),
            (PermissionError('private path'), 'cursor_sdk_setup_io_failed'),
        ]
        for error, reason in errors:
            with self.subTest(reason=reason), \
                 patch.object(cursor_sdk_setup, 'setup_environment', side_effect=error), \
                 patch.object(platforms, 'check_cursor', return_value=ready()), \
                 patch.object(platforms, 'check_grok', return_value=ready()), \
                 patch.object(platforms, 'check_codex', return_value=ready()):
                result = platforms.prepare_platforms(self.config)
            self.assertFalse(result['runtimes_ready'])
            self.assertEqual(result['platforms']['cursor']['reason'], reason)
            self.assertNotIn('private path', json.dumps(result))

    def test_grok_only_checks_existing_node_and_vendored_public_runtime(self):
        module = SimpleNamespace(runtime_binary=Mock(return_value='/node'), _vendor_valid=Mock(return_value=True),
                                 WORKER=SimpleNamespace(is_file=lambda: True), SDK_VERSION='0.78.1')
        self.config['command_adapters'] = {'grok_sdk': {'node_binary': '/configured/node'}}
        with patch.dict(sys.modules, {'grok_sdk': module}):
            result = platforms.check_grok(self.config)
            module.runtime_binary.assert_called_once_with({'node_binary': '/configured/node'})
            self.assertTrue(result['ready'])
            module._vendor_valid.return_value = False
            self.assertEqual(platforms.check_grok(self.config)['reason'], 'grok_sdk_package_incomplete')
            error = RuntimeError('discard this private diagnostic'); error.code = 'grok_sdk_runtime_required'
            module.runtime_binary.side_effect = error
            self.assertEqual(platforms.check_grok(self.config)['reason'], 'grok_sdk_runtime_required')

    def test_codex_checks_stdio_help_without_starting_account_or_session_rpc(self):
        import codex_rpc
        self.config['command_adapters'] = {'codex_background': {'binary': '/selected/codex'}}
        with patch.object(codex_rpc, 'resolve_binary', return_value='/selected/codex') as resolve, \
             patch.object(codex_rpc, '_desktop_server_available', return_value=True) as capability, \
             patch.object(codex_rpc, 'runtime_info', return_value={'source': 'cli', 'identity': 'discard'}), \
             patch.object(codex_rpc, 'StdioRpc') as native:
            result = platforms.check_codex(self.config)
        self.assertTrue(result['ready']); self.assertEqual(result['runtime_source'], 'cli')
        self.assertEqual(resolve.call_args.args, ({'binary': '/selected/codex'},))
        self.assertEqual(capability.call_args.args, ('/selected/codex',))
        self.assertEqual(resolve.call_args.kwargs['deadline'], capability.call_args.kwargs['deadline'])
        native.assert_not_called(); self.assertNotIn('identity', result)

    def test_missing_or_incompatible_codex_remains_missing_with_only_known_diagnostics(self):
        import codex_rpc
        with patch.object(codex_rpc, 'resolve_binary', return_value='/codex'), \
             patch.object(codex_rpc, '_desktop_server_available', return_value=False):
            self.assertEqual(platforms.check_codex(self.config)['reason'], 'codex_app_server_unavailable')
        for error, reason in [(codex_rpc.RpcError('codex_binary_unavailable'), 'codex_binary_unavailable'),
                              (codex_rpc.RpcError('codex_native_executable_required'), 'codex_native_executable_required'),
                              (codex_rpc.RpcError('codex_process_unavailable'), 'codex_process_unavailable'),
                              (RuntimeError('private exception'), 'codex_runtime_check_failed')]:
            with patch.object(codex_rpc, 'resolve_binary', side_effect=error):
                result = platforms.check_codex(self.config)
            self.assertFalse(result['ready']); self.assertEqual(result['reason'], reason)
            self.assertNotIn('private exception', json.dumps(result))

    def test_cli_reports_json_and_exits_one_until_all_runtimes_are_ready(self):
        import office
        for complete in (False, True):
            report = {'runtimes_ready': complete, 'platforms': {}}
            output = io.StringIO()
            with patch.object(sys, 'argv', ['office.py', '--config', str(self.root / 'config.json'), 'setup-platforms', '--check']), \
                 patch.object(office, 'load_config', return_value=self.config), \
                 patch.object(office, 'save_config') as save, \
                 patch.object(office, 'request') as request, \
                 patch.object(platforms, 'prepare_platforms', return_value=report) as prepare, \
                 contextlib.redirect_stdout(output):
                if complete:
                    office.main()
                else:
                    with self.assertRaises(SystemExit) as raised: office.main()
                    self.assertEqual(raised.exception.code, 1)
            self.assertEqual(json.loads(output.getvalue()), report)
            self.assertTrue(prepare.call_args.kwargs['check'])
            save.assert_not_called(); request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
