"""Bounded host check, interactive desktop launch, and local owner contract."""
import concurrent.futures
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import mobile_host as host
import test_invitations as fixtures

BASE = '/api/office/mobile/'


class MobileHostTests(unittest.TestCase):
    def setUp(self):
        self.assistant = host.MobileHostAssistant({})
        self.launch = patch.object(self.assistant, 'launch_command', return_value=None)
        self.launch.start()
        self.addCleanup(self.launch.stop)

    def test_public_observations_distinguish_every_action_without_peer_data(self):
        for native, expected in [('Running', 'running'), ('NeedsLogin', 'login'),
                                 ('NeedsMachineAuth', 'approve'), ('Stopped', 'connect'),
                                 ('Starting', 'unknown'), (None, 'unknown')]:
            self.assistant.check_expires = 0
            with self.subTest(native=native), patch.object(host.tailscale, 'detect_binary', return_value='fixture-cli'), \
                    patch.object(host.tailscale, '_probe', return_value={
                        'BackendState': native, 'AuthURL': 'private', 'Peer': {'private-peer': {}}}) as probe:
                result = self.assistant.check()
                self.assertEqual(set(result), {'state', 'platform', 'launch_available', 'custom_config'})
                self.assertEqual(result['state'], expected)
                self.assertNotIn('private', json.dumps(result))
                probe.assert_called_once_with('fixture-cli', 'status', 2.0, host.sys.platform)
        self.assistant.check_expires = 0
        with patch.object(host.tailscale, 'detect_binary', return_value=None), \
                patch.object(host.tailscale, '_probe') as probe:
            self.assertEqual(self.assistant.check()['state'], 'install')
            probe.assert_not_called()
        self.assistant.check_expires = 0
        with patch.object(host.tailscale, 'detect_binary', return_value='fixture-cli'), \
                patch.object(host.tailscale, '_probe', side_effect=host.tailscale.ProbeError('private')):
            self.assertEqual(self.assistant.check()['state'], 'unknown')

    def test_manual_launch_can_identify_a_custom_installation_without_exposing_paths(self):
        with patch.object(host.MobileHostAssistant, 'launch_command', return_value=None):
            standard = host.MobileHostAssistant({'config_path': str(host.ROOT / '.office/config.json')})
            custom = host.MobileHostAssistant({'config_path': str(host.ROOT / 'private-custom/config.json')})
            self.assertFalse(standard.open()['custom_config'])
            self.assertTrue(custom.open()['custom_config'])
            self.assertNotIn('private-custom', json.dumps(custom.open()))

    def test_simultaneous_checks_share_one_process(self):
        with patch.object(host.tailscale, 'detect_binary', return_value='fixture-cli'), \
                patch.object(host.tailscale, '_probe', return_value={'BackendState': 'Running'}) as probe, \
                concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.assistant.check(), range(3)))
        self.assertEqual([r['state'] for r in results], ['running'] * 3)
        self.assertEqual(probe.call_count, 1)

    def test_one_window_and_bounded_relaunch_through_rapid_clicks(self):
        self.assistant.launch_command.return_value = (['fixture-helper'], {})
        window = Mock()
        window.wait.side_effect = subprocess.TimeoutExpired('fixture-helper', .2)
        window.poll.return_value = None
        with patch.object(host.subprocess, 'Popen', return_value=window) as launch, \
                concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.assistant.open(), range(3)))
            self.assertEqual([r['state'] for r in results], ['opened'] * 3)
            self.assertEqual(launch.call_count, 1)
            window.poll.return_value = 0
            self.assertEqual(self.assistant.open()['state'], 'manual')
            self.assertEqual(launch.call_count, 1)

    def test_os_permission_or_immediate_window_failure_is_actionable(self):
        self.assertEqual(self.assistant.open()['state'], 'manual')
        self.assistant.launch_command.return_value = (['fixture-helper'], {})
        with patch.object(host.subprocess, 'Popen', side_effect=OSError('private path')):
            self.assistant.next_launch = 0
            self.assertEqual(self.assistant.open()['state'], 'manual')
        with patch.object(host.subprocess, 'Popen', return_value=Mock()):
            self.assistant.next_launch = 0
            self.assertEqual(self.assistant.open()['state'], 'manual')

    def test_mac_accepts_open_receipt_and_allows_later_retry_without_quitting_terminal(self):
        self.assistant.launch_command.return_value = (['/usr/bin/open', 'fixture'], {})
        dispatch = Mock(returncode=0)
        dispatch.poll.return_value = dispatch.wait.return_value = 0
        with patch.object(host.sys, 'platform', 'darwin'), \
                patch.object(host.subprocess, 'Popen', return_value=dispatch) as launch:
            self.assertEqual(self.assistant.open()['state'], 'opened')
            self.assertEqual(self.assistant.open()['state'], 'opened')
            self.assertEqual(launch.call_count, 1)
            self.assistant.next_launch = 0
            self.assertEqual(self.assistant.open()['state'], 'opened')
            self.assertEqual(launch.call_count, 2)

    def test_mac_terminal_receives_its_selected_config_and_exact_python(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'phone_setup_assistant.py').touch()
            wrapper = root / 'Connect Phone.command'
            wrapper.touch(mode=0o700)
            python = root / 'office python'
            python.touch()
            config = {'config_path': str(root / 'custom OFFICE/config.json')}
            original_stat = host.os.stat
            def stat(path, *args, **kwargs):
                if str(path) == '/dev/console':
                    return Mock(st_uid=42)
                return original_stat(path, *args, **kwargs)
            with patch.object(host, 'ROOT', root), patch.object(host.sys, 'platform', 'darwin'), \
                    patch.object(host.sys, 'executable', str(python)), \
                    patch.object(host.os, 'geteuid', return_value=42, create=True), \
                    patch.object(host.os, 'stat', side_effect=stat), \
                    patch.object(Path, 'is_dir', return_value=True):
                argv, options = host._launch_command(config)
                self.assertEqual(argv[0], '/usr/bin/open')
                self.assertEqual(argv[-1], str(wrapper))
                self.assertIn('OFFICE_CONFIG=' + config['config_path'], argv)
                self.assertIn('OFFICE_PHONE_PYTHON=' + str(python), argv)
                self.assertEqual(options, {'start_new_session': True})

    def test_windows_terminal_has_a_fixed_helper_and_its_exact_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'phone_setup_assistant.py').touch()
            (root / 'python.exe').touch()
            config = {'config_path': str(root / 'custom config.json')}
            with patch.object(host, 'ROOT', root), patch.object(host.sys, 'platform', 'win32'), \
                    patch.object(host.sys, 'executable', str(root / 'pythonw.exe')), \
                    patch.object(host, '_windows_desktop', return_value=True), \
                    patch.object(host.subprocess, 'CREATE_NEW_CONSOLE', 16, create=True):
                argv, flags = host._launch_command(config)
                self.assertEqual(argv, [str(root / 'python.exe'), str(root / 'phone_setup_assistant.py'),
                                        '--config', config['config_path'], '--desktop'])
                self.assertEqual(flags, {'creationflags': 16})
                with patch.object(host, '_windows_desktop', return_value=False):
                    self.assertIsNone(host._launch_command(config))


class MobileHostHTTPTests(unittest.TestCase):
    setUp = fixtures.InvitationTests.setUp
    request = fixtures.InvitationTests.request

    tearDown = fixtures.InvitationTests.tearDown

    def test_only_same_origin_owner_click_with_empty_input_dispatches_local_action(self):
        self.bridge.mobile_host = Mock()
        self.bridge.mobile_host.check.return_value = {'state': 'install'}
        self.bridge.mobile_host.open.return_value = {'state': 'manual'}
        for route, action in [('host-check', self.bridge.mobile_host.check),
                              ('host-assistant', self.bridge.mobile_host.open)]:
            url = BASE + route
            for headers in ({}, {'Origin': self.base.replace('127.0.0.1', 'localhost')},
                            {'Origin': 'https://example.test'}, {'Origin': self.base, 'X-Office-Key': ''}):
                self.assertEqual(self.request(url, {}, headers=headers)[0], 403)
            self.assertEqual(self.request(url, {'config': 'anything'}, headers={'Origin': self.base})[0], 400)
            self.assertEqual(self.request(url + '?path=unused', {}, headers={'Origin': self.base})[0], 400)
            action.assert_not_called()
            self.assertEqual(self.request(url, {}, headers={'Origin': self.base}), (200, action.return_value))
            action.assert_called_once_with()

    def test_legacy_status_shows_only_platform_and_configuration_kind_without_actions(self):
        for name in ('owner_key', 'internal_hmac_key'):
            self.bridge.config.pop(name, None)
        with patch.object(host.tailscale, 'detect_binary') as binary, \
                patch.object(host, '_launch_command') as launch, \
                patch.object(host.sys, 'platform', 'win32'):
            code, result = self.request('/api/office/mobile')
            self.assertEqual(code, 200)
            self.assertFalse(result['credentials_separated'])
            self.assertEqual(result['host_assistant'], {'platform': 'windows', 'custom_config': True})
            binary.assert_not_called()
            launch.assert_not_called()
        self.assertEqual(self.request('/api/office/mobile', key=False)[0], 403)

    def test_shared_legacy_feed_credential_cannot_start_a_desktop_action(self):
        for name in ('owner_key', 'internal_hmac_key'):
            self.bridge.config.pop(name, None)
        self.bridge.mobile_host = Mock()
        self.assertEqual(self.request(BASE + 'host-assistant', {}, headers={'Origin': self.base}),
                         (403, {'error': 'mobile_owner_upgrade_required'}))
        self.bridge.mobile_host.open.assert_not_called()


if __name__ == '__main__':
    unittest.main()
