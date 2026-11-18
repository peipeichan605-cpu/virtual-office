"""All process responses are mocked; these tests never invoke Tailscale."""
import copy
import io
import json
import subprocess
import threading
import unittest
from unittest.mock import patch

import tailscale_mobile as tm


ORIGIN = 'https://office.example-tail.ts.net'
IDENTITY = 'owner@example.invalid'
PORT = 4319


class FakeProcess:
    def __init__(self, raw=b'', returncode=0, error=None):
        self.stdout = io.BytesIO(raw)
        self.returncode = None
        self.final_code = returncode
        self.error = error
        self.killed = False

    def wait(self, timeout=None):
        if timeout is not None and not 0 <= timeout <= tm.MAX_TIMEOUT:
            raise AssertionError('unbounded subprocess wait')
        if self.error:
            raise self.error
        self.returncode = self.final_code
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.error = None
        self.final_code = -9


def fixture():
    mount = {'TCP': {'443': {'HTTPS': True}},
             'Web': {'office.example-tail.ts.net:443': {'Handlers': {
                 '/': {'Proxy': 'http://127.0.0.1:4319'}}}}}
    return {
        'version': {'short': '1.104.7', 'long': 'private build details'},
        'status': {'Version': '1.104.7-t123abc-g456def', 'BackendState': 'Running',
                   'Self': {'DNSName': 'office.example-tail.ts.net.', 'UserID': 9},
                   'User': {'9': {'LoginName': IDENTITY}},
                   'CurrentTailnet': {'MagicDNSEnabled': True}, 'Health': [],
                   'AuthURL': 'https://example.invalid/auth/SECRET',
                   'Peer': {'secret-peer': {'TailscaleIPs': ['100.99.99.99']}}},
        'preferences': {'shields-up': False, 'ssh': False, 'accept-routes': False,
                        'advertise-exit-node': False, 'advertise-connector': False,
                        'webclient': False, 'advertise-routes': '', 'exit-node': ''},
        'remote_control': {'remote-config': False},
        'serve': copy.deepcopy(mount), 'funnel': copy.deepcopy(mount),
    }


class PreflightTests(unittest.TestCase):
    def run_fake(self, data=None, error=None, platform_name='darwin'):
        data = data or fixture()

        def run(argv, **kwargs):
            self.assertFalse(kwargs['shell'])
            self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
            self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
            if platform_name == 'darwin':
                self.assertEqual(kwargs['env']['TAILSCALE_BE_CLI'], '1')
            name = next(name for name, command in tm.PROBES.items() if tuple(argv[1:]) == command)
            if isinstance(error, OSError):
                raise error
            return FakeProcess(json.dumps(data[name]).encode(), error=error)

        with patch.object(tm, 'detect_binary', return_value='/mock/tailscale'), patch.object(tm.subprocess, 'Popen', side_effect=run) as mocked:
            result = tm.inspect_tailscale(ORIGIN, IDENTITY, PORT, platform_name=platform_name)
        return result, mocked

    def test_valid_transport_never_attests_policy_or_delivery(self):
        result, mocked = self.run_fake()
        self.assertTrue(result['transport_ready'])
        self.assertEqual(mocked.call_count, 6)
        self.assertFalse(result['policy_verified'])
        self.assertFalse(result['end_to_end_verified'])
        self.assertTrue(result['requires_policy_review'])
        self.assertEqual(result['state'], 'transport_observed_policy_and_phone_test_required')
        output = json.dumps(result)
        for private in ('SECRET', IDENTITY, '100.99.99.99', 'private build details', 'secret-peer'):
            self.assertNotIn(private, output)

    def test_missing_binary_is_setup_required_without_process(self):
        with patch.object(tm, 'detect_binary', return_value=None), patch.object(tm.subprocess, 'Popen') as mocked:
            result = tm.inspect_tailscale(ORIGIN, IDENTITY, PORT)
        mocked.assert_not_called()
        self.assertEqual(result['issues'], ['tailscale_not_installed_or_binary_unavailable'])

    def test_timeout_stops_all_followup_probes_and_redacts_exception(self):
        result, mocked = self.run_fake(error=subprocess.TimeoutExpired(['SECRET'], 5, output=b'SECRET'))
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(result['issues'], ['version_cli_timeout'])
        self.assertNotIn('SECRET', json.dumps(result))

    def test_os_error_is_redacted(self):
        result, mocked = self.run_fake(error=OSError('SECRET credential location'))
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(result['issues'], ['version_cli_unavailable'])

    def test_login_pending_returns_without_prefs_or_serve(self):
        data = fixture()
        data['status']['BackendState'] = 'NeedsLogin'
        result, mocked = self.run_fake(data)
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(result['issues'], ['tailscale_login_required'])
        self.assertNotIn('SECRET', json.dumps(result))

    def test_approval_pending_does_not_attempt_login(self):
        data = fixture()
        data['status']['BackendState'] = 'NeedsMachineAuth'
        result, mocked = self.run_fake(data)
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(result['issues'], ['tailscale_device_approval_required'])

    def test_old_or_unrecognized_client_version_stops(self):
        for version in ('1.104.3', '1.104.4', '1.105.0', 'SECRET', '1.104.7-beta', None):
            with self.subTest(version=version):
                data = fixture()
                data['version']['short'] = version
                result, mocked = self.run_fake(data)
                self.assertFalse(result['transport_ready'])
                self.assertEqual(mocked.call_count, 1)

    def test_old_daemon_not_masked_by_current_cli(self):
        data = fixture()
        data['status']['Version'] = '1.104.3'
        result, _ = self.run_fake(data)
        self.assertIn('daemon_security_update_or_version_review_required', result['issues'])

    def test_dns_and_identity_mismatch_do_not_leak_values(self):
        data = fixture()
        data['status']['Self']['DNSName'] = 'private-secret.ts.net.'
        data['status']['User']['9']['LoginName'] = 'private-secret@example.invalid'
        result, _ = self.run_fake(data)
        self.assertIn('origin_dns_mismatch', result['issues'])
        self.assertIn('expected_identity_not_observed', result['issues'])
        self.assertNotIn('private-secret', json.dumps(result))

    def test_phone_account_can_differ_from_host_account(self):
        data = fixture()
        data['status']['User']['9']['LoginName'] = 'host@example.invalid'
        data['status']['User']['10'] = {'LoginName': IDENTITY}
        result, _ = self.run_fake(data)
        self.assertTrue(result['transport_ready'])
        self.assertFalse(result['checks']['host_identity_matches'])
        self.assertTrue(result['checks']['expected_identity_visible'])

    def test_unknown_or_unsafe_network_preferences_fail_closed(self):
        for name, value in (('ssh', True), ('accept-routes', True),
                            ('advertise-routes', '10.0.0.0/8'), ('exit-node', '100.1.1.1'),
                            ('webclient', True), ('shields-up', True),
                            ('advertise-connector', True), ('advertise-exit-node', True),
                            ('ssh', 0), ('ssh', None)):
            with self.subTest(name=name, value=value):
                data = fixture()
                data['preferences'][name] = value
                result, _ = self.run_fake(data)
                self.assertIn('network_preferences_review_required', result['issues'])
        data = fixture()
        del data['preferences']['ssh']
        self.assertFalse(self.run_fake(data)[0]['transport_ready'])

    def test_hidden_remote_control_must_be_explicitly_false(self):
        for value in (True, None, 'false', 0):
            data = fixture()
            data['remote_control']['remote-config'] = value
            result, _ = self.run_fake(data)
            self.assertIn('remote_control_review_required', result['issues'])

    def test_funnel_in_foreground_or_second_snapshot_is_not_missed(self):
        for probe in ('serve', 'funnel'):
            data = fixture()
            data[probe]['Foreground'] = {'private-session': {'AllowFunnel': {'private-host:443': True}}}
            result, _ = self.run_fake(data)
            self.assertIn('funnel_enabled_or_unrecognized', result['issues'])
            self.assertNotIn('private-', json.dumps(result))

    def test_empty_serve_config_requires_explicit_setup(self):
        data = fixture()
        data['serve'] = {}
        data['funnel'] = {}
        result, _ = self.run_fake(data)
        self.assertIn('serve_setup_or_conflict_review_required', result['issues'])

    def test_extra_mount_file_raw_tcp_and_service_configs_require_review(self):
        variants = [
            {'Web': {'office.example-tail.ts.net:443': {'Handlers': {
                '/': {'Proxy': 'http://127.0.0.1:4319'}, '/private': {'Path': '/private'}}}}},
            {'TCP': {'443': {'TCPForward': '127.0.0.1:4319'}}},
            {'TCP': {'443': {'HTTPS': 1}}},
            {'Services': {'svc:other': {'Tun': True}}},
            {'Foreground': ''},
            {'UnexpectedFutureForwarding': True},
        ]
        for changes in variants:
            with self.subTest(changes=changes):
                data = fixture()
                data['serve'].update(changes)
                result, _ = self.run_fake(data)
                self.assertIn('serve_setup_or_conflict_review_required', result['issues'])

    def test_wrong_loopback_backend_including_core_is_rejected(self):
        for target in ('http://127.0.0.1:4318', 'http://192.168.1.1:4319',
                       'http://localhost:4319', 'unix:/var/run/tailscaled.sock'):
            data = fixture()
            data['serve']['Web']['office.example-tail.ts.net:443']['Handlers']['/']['Proxy'] = target
            self.assertFalse(self.run_fake(data)[0]['transport_ready'])

    def test_invalid_expected_configuration_never_starts_process(self):
        with patch.object(tm.subprocess, 'Popen') as mocked:
            for origin in ('http://office.example-tail.ts.net', 'https://example.com',
                           ORIGIN + '/#SECRET', ORIGIN + ':444', ORIGIN + '?SECRET'):
                self.assertEqual(tm.inspect_tailscale(origin, IDENTITY, PORT)['issues'], ['invalid_expected_configuration'])
            self.assertEqual(tm.inspect_tailscale(ORIGIN, IDENTITY, True)['issues'], ['invalid_expected_configuration'])
            self.assertEqual(tm.inspect_tailscale(ORIGIN, IDENTITY, PORT, timeout=6)['issues'], ['invalid_expected_configuration'])
        mocked.assert_not_called()

    def test_output_limit_invalid_json_duplicates_and_errors_are_redacted(self):
        for raw, code in ((b'x' * (tm.MAX_OUTPUT_BYTES + 1), 'cli_output_limit'),
                          (b'{SECRET', 'cli_invalid_json'),
                          (b'{"short":"1.104.7","short":"1.104.3"}', 'cli_invalid_json'),
                          (b'[]', 'cli_unexpected_schema')):
            process = FakeProcess(raw)
            with patch.object(tm, 'detect_binary', return_value='/mock/tailscale'), patch.object(tm.subprocess, 'Popen', return_value=process):
                result = tm.inspect_tailscale(ORIGIN, IDENTITY, PORT)
            self.assertEqual(result['issues'], ['version_' + code])
            self.assertNotIn('SECRET', json.dumps(result))
            if code == 'cli_output_limit':
                self.assertTrue(process.killed)
        with patch.object(tm, 'detect_binary', return_value='/mock/tailscale'), patch.object(tm.subprocess, 'Popen', return_value=FakeProcess(b'SECRET', 1)):
            self.assertEqual(tm.inspect_tailscale(ORIGIN, IDENTITY, PORT)['issues'], ['version_cli_command_failed'])

    def test_infinite_stdout_is_stopped_at_limit_without_full_capture(self):
        class Stream:
            received = 0
            closed = False

            def read1(self, size):
                self.received += size
                return b'x' * size

            def close(self):
                self.closed = True

        process = FakeProcess()
        process.stdout = Stream()
        with patch.object(tm, 'detect_binary', return_value='/mock/tailscale'), patch.object(tm.subprocess, 'Popen', return_value=process):
            result = tm.inspect_tailscale(ORIGIN, IDENTITY, PORT)
        self.assertEqual(process.stdout.received, tm.MAX_OUTPUT_BYTES + 1)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.killed)
        self.assertEqual(result['issues'], ['version_cli_output_limit'])

    def test_blocked_stdout_times_out_and_kills_only_owned_child(self):
        stopped = threading.Event()

        class Stream:
            def read1(self, size):
                stopped.wait(1)
                return b''

            def close(self):
                pass

        process = FakeProcess()
        process.stdout = Stream()
        original_kill = process.kill

        def kill():
            original_kill()
            stopped.set()

        process.kill = kill
        with patch.object(tm, 'detect_binary', return_value='/mock/tailscale'), patch.object(tm.subprocess, 'Popen', return_value=process) as mocked:
            result = tm.inspect_tailscale(ORIGIN, IDENTITY, PORT, timeout=0.01)
        self.assertTrue(process.killed)
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(result['issues'], ['version_cli_timeout'])

    def test_named_mac_fallback_and_no_home_scan(self):
        with patch.object(tm.shutil, 'which', return_value=None), patch.object(tm.Path, 'is_file', return_value=True), patch.object(tm.os, 'access', return_value=True):
            self.assertEqual(tm.detect_binary(platform_name='darwin'), tm.MAC_BINARY)
            self.assertIsNone(tm.detect_binary(platform_name='linux'))
            self.assertIsNone(tm.detect_binary('relative-tailscale', 'darwin'))

    def test_cli_exit_zero_means_only_transport_observed(self):
        with patch.object(tm, 'inspect_tailscale', return_value={'transport_ready': False}), patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(tm.main(['--origin', ORIGIN, '--identity', IDENTITY, '--port', str(PORT)]), 2)


if __name__ == '__main__':
    unittest.main()
