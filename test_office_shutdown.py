"""Service lifecycle checks using isolated configuration and loopback listeners."""
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from configuration import initialize, load_config, save_config
from dashboard import atomic_json
from security_keys import owner_key


class ShutdownTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.config_path = self.root / 'private' / 'config.json'
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        initialize(self.config_path, self.port)
        self.config = load_config(self.config_path)
        self.run = self.config_path.parent / 'runs' / 'office-test'
        self.config.update(run_id=self.run.name, run_dir=str(self.run))
        save_config(self.config_path, self.config)

    def launch(self, *args, program=None):
        entry = ['-c', program] if program else ['dashboard.py']
        # On Windows the venv redirector can own a different PID from Python.
        # The base interpreter keeps cleanup attached to the actual fixture.
        binary = sys._base_executable if os.name == 'nt' else sys.executable
        self.child = subprocess.Popen(
            [binary, *entry, '--config', str(self.config_path), '--dry-run', '--duration', '45', *args],
            cwd=Path(__file__).resolve().parent,
            env={**os.environ, 'CODEX_HOME': str(self.root / 'codex-fixture')},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.addCleanup(self.close_child, self.child)

    def close_child(self, child):
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)

    def request(self, method, path, key=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=1)
        try:
            headers = {'X-Office-Key': key} if key else {}
            connection.request(method, path, headers=headers)
            response = connection.getresponse()
            return response.status, json.load(response)
        finally:
            connection.close()

    def wait_ready(self):
        deadline = time.monotonic() + 30
        while self.child.poll() is None and time.monotonic() < deadline:
            try:
                code, health = self.request('GET', '/health', owner_key(self.config))
            except OSError:
                time.sleep(.05)
                continue
            if code == 200 and health.get('run_id') == self.run.name:
                return health
        self.fail('The fixture dashboard did not become ready')

    def assert_result(self, reason, exit_code=0):
        output = self.child.communicate(timeout=15)[0]
        self.assertEqual(self.child.returncode, exit_code, output)
        result = json.loads((self.run / 'result.json').read_text(encoding='utf-8'))
        status = json.loads((self.run / 'status.json').read_text(encoding='utf-8'))
        self.assertEqual(result, status)
        process = json.loads((self.run / 'pid.json').read_text(encoding='utf-8'))
        self.assertEqual((result['run_id'], result['pid']), (self.run.name, process['pid']))
        self.assertEqual((result['state'], result['stop_reason']), ('stopped', reason))
        return output

    def test_owner_stop_and_private_health_identity(self):
        self.launch()
        health = self.wait_ready()
        for key in (None, 'wrong-key'):
            self.assertNotIn('run_id', self.request('GET', '/health', key)[1])
            self.assertEqual(self.request('POST', '/api/office/shutdown', key)[0], 403)
        status, receipt = self.request('POST', '/api/office/shutdown', owner_key(self.config))
        self.assertEqual((status, receipt['pid']), (200, health['pid']))
        self.assert_result('owner_api')

    def test_duration_elapsed(self):
        self.launch('--duration', '1')
        self.assert_result('duration_elapsed')

    @unittest.skipIf(os.name == 'nt', 'Windows process termination does not deliver POSIX signals')
    def test_signal_handlers(self):
        for number in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=number.name):
                self.launch()
                self.wait_ready()
                self.child.send_signal(number)
                self.assert_result('signal_' + number.name.lower())

    def test_observer_exception_has_a_final_result_without_private_error_text(self):
        self.launch(program=(
            "import dashboard\n"
            "def fail(self): raise RuntimeError('private-fixture-message')\n"
            "dashboard.Bridge.observe_loop = fail\n"
            "raise SystemExit(dashboard.main())"))
        output = self.assert_result('observer_failed', exit_code=1)
        self.assertNotIn(b'private-fixture-message', output)
        self.assertNotIn(b'private-fixture-message', (self.run / 'result.json').read_bytes())

    def test_unexpected_observer_return(self):
        self.launch(program=(
            'import dashboard\n'
            'dashboard.Bridge.observe_loop = lambda self: None\n'
            'raise SystemExit(dashboard.main())'))
        self.assert_result('observer_exited', exit_code=1)

    def test_status_json_with_non_ascii_names_uses_utf8(self):
        real_fdopen = os.fdopen

        def legacy_default(fd, mode, encoding='cp1252'):
            return real_fdopen(fd, mode, encoding=encoding)

        target = self.root / 'status.json'
        value = {'name': '測試🌿'}
        with patch('dashboard.os.fdopen', side_effect=legacy_default):
            atomic_json(target, value)
        self.assertEqual(json.loads(target.read_text(encoding='utf-8')), value)


if __name__ == '__main__':
    unittest.main()
