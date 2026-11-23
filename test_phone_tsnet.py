"""No real Tailscale, account, VPN, network or Office configuration is used."""
import json
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import phone_tsnet as frontend


class FrontendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # macOS /var aliases /private/var; use its canonical private directory.
        self.base = Path(self.temp.name).resolve()
        self.base.chmod(0o700)
        self.config = {'data_dir': str(self.base), 'port': 4318, 'mobile': {'port': 4319}}
        self.root = frontend._root(self.config)
        self.children = []

    def tearDown(self):
        active = self.root / 'active.json'
        if active.exists():
            record = frontend._read_json(active)
            if frontend._is_running(record):
                frontend.stop_frontend(self.config, run_id=record['run_id'])
        # Some tests deliberately make recovered identity unavailable. Cleanup
        # still owns the exact local fake Popen handle; never recover by PID.
        for run_id, child in list(frontend._LOCAL_CHILDREN.items()):
            if child.pid in self.children:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=3)
                frontend._LOCAL_CHILDREN.pop(run_id, None)
        for pid in self.children:
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
        self.temp.cleanup()

    def assertCode(self, code, fn, *args, **kwargs):
        with self.assertRaisesRegex(frontend.FrontendError, '^' + code + '$'):
            fn(*args, **kwargs)

    def test_environment_is_allowlisted_and_does_not_change_parent(self):
        environment = {'HOME': '/unchanged', 'PATH': '/bin', 'LANG': 'C', 'HTTPS_PROXY': 'http://local-proxy',
                       'TS_AUTHKEY': 'secret', 'TS_AUTH_KEY': 'secret', 'TS_CONTROL_URL': 'https://evil',
                       'TS_CLIENT_SECRET': 'secret', 'TS_ID_TOKEN': 'secret', 'TSNET_FORCE_LOGIN': '1',
                       'OPENAI_API_KEY': 'secret', 'GOFLAGS': '-toolexec=evil', 'GOTOOLCHAIN': 'auto',
                       'TS_DISABLE_PORTMAPPER': 'false', 'TS_NO_LOGS_NO_SUPPORT': 'false'}
        with mock.patch.dict(os.environ, environment, clear=True):
            child = frontend.child_environment(build=True, cache_root=self.root)
            self.assertEqual(dict(os.environ), environment)
        self.assertEqual(child['HOME'], '/unchanged')
        self.assertEqual(child['HTTPS_PROXY'], 'http://local-proxy')
        self.assertEqual(child['TS_DISABLE_PORTMAPPER'], 'true')
        self.assertEqual(child['TS_NO_LOGS_NO_SUPPORT'], 'true')
        self.assertEqual(child['GOTOOLCHAIN'], 'local')
        self.assertEqual(child['GOMAXPROCS'], '2')
        self.assertEqual(child['GOENV'], 'off')
        self.assertEqual(child['GOWORK'], 'off')
        self.assertNotIn('secret', child.values())
        self.assertNotIn('TS_CONTROL_URL', child)
        self.assertNotIn('TSNET_FORCE_LOGIN', child)
        self.assertNotIn('GOFLAGS', child)

    def test_login_contains_no_listener_approval_fields(self):
        settings = frontend.validate_settings(self.config, mode='login', hostname='office-phone')
        self.assertEqual(settings, {'mode': 'login', 'hostname': 'office-phone', 'backend_port': 4319, 'core_port': 4318})
        for kwargs in ({'origin': 'https://office.tail.ts.net'}, {'identity': 'owner@example.invalid'}, {'node_ids': ['nPhone']}):
            self.assertCode('login_mode_has_no_remote_listener', frontend.validate_settings,
                            self.config, mode='login', hostname='office-phone', **kwargs)

    def test_serve_approval_is_exact_and_requires_phone_ids(self):
        kwargs = dict(mode='serve', hostname='office-phone', origin='https://office.tail.ts.net',
                      identity='owner@example.invalid', node_ids=['nPhone123'])
        actual = frontend.validate_settings(self.config, **kwargs)
        self.assertEqual(actual['allowed_node_ids'], ['nPhone123'])
        for origin in ('http://office.tail.ts.net', 'https://office.tail.ts.net/', 'https://office.tail.ts.net:443',
                       'https://office.tail.ts.net@evil.test', 'https://office.tail.ts.net?key=secret',
                       'https://evil.test', 'https://office.tail.ts.net/#secret'):
            with self.subTest(origin=origin):
                self.assertCode('invalid_expected_origin', frontend.validate_settings, self.config, **{**kwargs, 'origin': origin})
        for nodes in (None, [], ['*'], ['nPhone', 'nPhone'], ['bad node'], ['x'] * 17):
            with self.subTest(nodes=nodes):
                self.assertCode('invalid_allowed_nodes', frontend.validate_settings, self.config, **{**kwargs, 'node_ids': nodes})
        for identity in ('', 'owner\nsecret', ' owner', None):
            self.assertCode('invalid_expected_identity', frontend.validate_settings, self.config, **{**kwargs, 'identity': identity})

    def test_core_and_backend_ports_are_distinct_and_bounded(self):
        for port, backend in ((4318, 4318), (True, 4319), (4318, True), (1023, 4319), (65536, 4319), (4318, '4319')):
            with self.subTest(port=port, backend=backend):
                config = {**self.config, 'port': port, 'mobile': {'port': backend}}
                self.assertCode('invalid_frontend_ports', frontend.validate_settings, config, mode='login', hostname='office')
        for hostname in ('Office', '../office', 'a.b', 'office_', 'x' * 64):
            self.assertCode('invalid_frontend_hostname', frontend.validate_settings, self.config, mode='login', hostname=hostname)

    def test_private_paths_reject_symlinks_permissions_and_traversal(self):
        link = self.base / 'alias'
        link.symlink_to(self.root, target_is_directory=True)
        self.assertCode('private_path_symlink', frontend._directory, link)
        self.assertCode('private_path_symlink', frontend._read_json, link / 'record.json')
        self.assertCode('invalid_private_path', frontend._path, self.root / '..' / 'record')
        public = self.base / 'public'
        public.mkdir(mode=0o755)
        self.assertCode('private_directory_required', frontend._directory, public)
        record = self.root / 'record.json'
        frontend._write_json(record, {'hello': 'world'})
        self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)
        record.chmod(0o644)
        self.assertCode('private_file_required', frontend._read_json, record)
        record.unlink()
        record.symlink_to(self.base / 'other')
        self.assertCode('private_path_symlink', frontend._write_json, record, {'x': 1})

    def test_lifecycle_lock_rejects_concurrent_mutation(self):
        with frontend._lock(self.root):
            with self.assertRaisesRegex(frontend.FrontendError, 'frontend_lifecycle_busy'):
                with frontend._lock(self.root):
                    self.fail('second lock acquired')

    def _fake_binary(self):
        directory = frontend._directory(self.root / 'bin', create=True)
        # macOS /usr/bin/python3 is an exec launcher whose argv changes. Use
        # the actual interpreter so the test models a fixed Go executable.
        interpreter = Path(sys.prefix) / 'Resources/Python.app/Contents/MacOS/Python'
        if not interpreter.is_file():
            interpreter = Path(sys.executable).resolve()
        source = '#!' + str(interpreter) + '''
import json, os, signal, sys, time
from datetime import datetime, timezone
config=json.load(open(sys.argv[2]))
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
status={'version':1,'pid':os.getpid(),'phase':'login_required','updated_at':datetime.now(timezone.utc).isoformat(),
        'auth_url':'https://login.tailscale.com/a/DO_NOT_PRINT',
        'own':{'identity':'private@example.invalid','node_id':'private-node'}}
fd=os.open(config['status_path'],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f: json.dump(status,f)
while True: time.sleep(.05)
'''
        staging = directory / 'staging'
        staging.write_text(source)
        staging.chmod(0o700)
        digest = frontend._sha(staging)
        binary = directory / ('office-phone-' + digest[:16])
        staging.rename(binary)
        frontend._write_json(self.root / 'binary.json', {'version': 1, 'binary': str(binary),
                            'sha256': digest, 'tailscale_version': frontend.TS_VERSION})
        return binary

    def _start_fake(self):
        self._fake_binary()
        result = frontend.start_frontend(self.config, hostname='test-office')
        self.children.append(result['pid'])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = frontend.frontend_status(self.config)
            if result['state'] == 'login_required':
                return result
            time.sleep(.025)
        self.fail('local fake worker did not become ready')

    def test_real_fake_process_status_is_private_and_exact_stop_works(self):
        result = self._start_fake()
        self.assertTrue(result['running'])
        self.assertFalse(result['listening'])
        self.assertFalse(result['end_to_end_verified'])
        self.assertTrue(result['auth_url_available'])
        self.assertEqual(result['phase'], 'login_required')
        public = json.dumps(result)
        self.assertNotIn('DO_NOT_PRINT', public)
        self.assertNotIn('example.invalid', public)
        self.assertNotIn('auth_url"', public)
        record = frontend._read_json(Path(result['private_status_path']))
        self.assertIn('DO_NOT_PRINT', record['auth_url'])
        run = Path(result['private_status_path']).parent
        for name in ('request.json', 'status.json', 'result.json', 'stdout.log', 'config.json', 'worker-status.json'):
            self.assertEqual(stat.S_IMODE((run / name).stat().st_mode), 0o600)
        self.assertCode('frontend_already_running', frontend.start_frontend, self.config, hostname='test-office')
        self.assertCode('exact_run_id_required', frontend.stop_frontend, self.config, run_id='wrong')
        stopped = frontend.stop_frontend(self.config, run_id=result['run_id'])
        self.assertTrue(stopped['stopped'])
        self.assertFalse((self.root / 'active.json').exists())
        self.assertEqual(frontend.frontend_status(self.config)['state'], 'not_started')

    def test_stop_identity_mismatch_never_signals_a_reused_pid(self):
        result = self._start_fake()
        with mock.patch.object(frontend, '_process_identity', return_value={'started': 'unrelated'}), mock.patch.object(frontend.os, 'kill') as kill:
            self.assertCode('frontend_process_identity_mismatch', frontend.stop_frontend, self.config, run_id=result['run_id'])
            self.assertEqual(kill.call_args_list, [mock.call(result['pid'], 0)])
        with mock.patch.object(frontend, '_process_identity', return_value=None), mock.patch.object(frontend.os, 'kill') as kill:
            self.assertCode('frontend_process_identity_mismatch', frontend.stop_frontend, self.config, run_id=result['run_id'])
            self.assertEqual(kill.call_args_list, [mock.call(result['pid'], 0)])

    def test_forged_status_cannot_claim_login_listener_or_reveal_url(self):
        result = self._start_fake()
        path = Path(result['private_status_path'])
        for payload in ({'pid': result['pid'], 'phase': 'ready'}, {'pid': result['pid'] + 1, 'phase': 'ready'},
                        {'pid': result['pid'], 'phase': 'unknown'}, {'pid': result['pid'], 'phase': 'login_required',
                        'auth_url': 'https://login.tailscale.com.evil.invalid/secret'}):
            frontend._write_json(path, payload)
            status = frontend.frontend_status(self.config)
            self.assertFalse(status['listening'])
            self.assertFalse(status['auth_url_available'])
            self.assertFalse(status['end_to_end_verified'])

    def test_transient_identity_failure_is_not_stop_confirmation(self):
        result = self._start_fake()
        identity = frontend._active(self.root)['process_identity']
        with mock.patch.object(frontend, 'STOP_WAIT_SECONDS', 0), \
                mock.patch.object(frontend, '_process_identity', side_effect=[identity, identity, None, None]), \
                mock.patch.object(frontend.os, 'kill') as kill:
            stopped = frontend.stop_frontend(self.config, run_id=result['run_id'])
        self.assertFalse(stopped['stopped'])
        self.assertEqual(stopped['state'], 'stop_requested')
        self.assertTrue((self.root / 'active.json').exists())
        self.assertEqual(kill.call_args_list, [mock.call(result['pid'], signal.SIGTERM), mock.call(result['pid'], 0)])

    def test_live_pid_with_unreadable_identity_is_unknown_not_dead(self):
        self._start_fake()
        with mock.patch.object(frontend, '_process_identity', return_value=None):
            status = frontend.frontend_status(self.config)
        self.assertEqual(status['state'], 'process_identity_unknown')
        self.assertFalse(status['listening'])
        self.assertFalse(status['auth_url_available'])

    def test_stop_waits_for_pid_disappearance_after_process_arguments_disappear(self):
        result = self._start_fake()
        identity = frontend._active(self.root)['process_identity']
        with mock.patch.object(frontend, '_process_identity', side_effect=[identity, identity, None, None]), \
                mock.patch.object(frontend, '_pid_absent', side_effect=[False, True]) as absent, \
                mock.patch.object(frontend.os, 'kill', wraps=os.kill) as signal_worker:
            stopped = frontend.stop_frontend(self.config, run_id=result['run_id'])
        self.assertTrue(stopped['stopped'])
        self.assertEqual(absent.call_count, 2)
        signal_worker.assert_called_once_with(result['pid'], signal.SIGTERM)

    def test_missing_stale_or_future_status_does_not_advertise_health(self):
        result = self._start_fake()
        record = frontend._active(self.root)
        path = Path(result['private_status_path'])
        worker = frontend._read_json(path)
        stale = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        future = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
        for timestamp in (None, 'invalid', stale, future, '2026-01-01T00:00:00'):
            frontend._write_json(path, {**worker, 'updated_at': timestamp})
            status = frontend.frontend_status(self.config)
            self.assertEqual(status['state'], 'status_stale')
            self.assertFalse(status['auth_url_available'])
            self.assertFalse(status['listening'])
            frontend._write_json(path, {**worker, 'phase': 'ready', 'updated_at': timestamp})
            serving_status = frontend._public_status(self.root, {**record, 'mode': 'serve'})
            self.assertEqual(serving_status['state'], 'status_stale')
            self.assertFalse(serving_status['listening'])
        frontend._write_json(path, {**worker, 'phase': 'ready', 'updated_at': frontend._now()})
        self.assertTrue(frontend._public_status(self.root, {**record, 'mode': 'serve'})['listening'])

    def test_start_record_or_callback_failure_reaps_exact_owned_child(self):
        self._fake_binary()
        original_write = frontend._write_json
        original_popen = subprocess.Popen
        for failure in ('active.json', 'status.json', 'result.json', 'callback'):
            children = []
            def launch(argv, **kwargs):
                child = original_popen(argv, **kwargs)
                if kwargs.get('start_new_session'):
                    children.append(child)
                return child
            def write(path, data):
                if children and Path(path).name == failure:
                    raise OSError('injected private record failure')
                return original_write(path, data)
            def callback(_):
                if failure == 'callback':
                    raise RuntimeError('injected caller failure')
            with self.subTest(failure=failure), mock.patch.object(frontend.subprocess, 'Popen', side_effect=launch), mock.patch.object(frontend, '_write_json', side_effect=write):
                with self.assertRaises((OSError, RuntimeError)):
                    frontend.start_frontend(self.config, on_started=callback)
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())
            self.assertFalse((self.root / 'active.json').exists())
            self.assertFalse(frontend._LOCAL_CHILDREN)

    def test_manifest_tamper_and_state_hostname_change_fail_closed(self):
        binary = self._fake_binary()
        binary.write_text(binary.read_text() + '# changed\n')
        self.assertCode('frontend_binary_integrity_failed', frontend.start_frontend, self.config)
        self._fake_binary()
        frontend._write_json(self.root / 'node-settings.json', {'hostname': 'previous-office'})
        self.assertCode('frontend_hostname_requires_explicit_new_state', frontend.start_frontend, self.config)

    def _fake_build_sources(self):
        source = self.base / 'source'
        source.mkdir()
        module = source / 'phone-tsnet'
        module.mkdir()
        (module / 'go.mod').write_text('module test\n\ngo 1.27.4\n\nrequire tailscale.com v1.104.7\n')
        (module / 'go.sum').write_text('not-used-by-mocked-go\n')
        go = self.base / 'go'
        go.write_text('#!/bin/sh\nexit 1\n')
        go.chmod(0o700)
        return source, go

    def test_build_pins_dependency_toolchain_tags_and_private_output(self):
        source, go = self._fake_build_sources()
        calls = []

        class Build:
            pid = 12345
            def __init__(self, argv, **kwargs):
                calls.append((argv, kwargs))
                Path(argv[argv.index('-o') + 1]).write_bytes(b'fake built binary')
            def wait(self, timeout=None):
                return 0

        version = subprocess.CompletedProcess([], 0, 'go version go1.27.4 darwin/arm64\n', '')
        receipt = []
        with mock.patch.object(frontend, 'ROOT', source), mock.patch.object(frontend.subprocess, 'run', return_value=version), mock.patch.object(frontend.subprocess, 'Popen', Build):
            result = frontend.build_frontend(self.config, go, on_started=receipt.append)
        self.assertEqual(result['state'], 'built')
        self.assertEqual(receipt[0]['state'], 'building')
        self.assertEqual(receipt[0]['run_id'], result['run_id'])
        self.assertIn('-tags=ts_omit_peerapiserver', calls[0][0])
        self.assertIn('-mod=readonly', calls[0][0])
        self.assertIn('-p=2', calls[0][0])
        self.assertEqual(calls[0][1]['env']['GOTOOLCHAIN'], 'local')
        self.assertEqual(stat.S_IMODE(Path(result['binary_path']).stat().st_mode), 0o700)
        self.assertTrue(str(result['binary_path']).startswith(str(self.root / 'bin')))

    def test_old_go_and_dependency_override_do_not_start_a_build(self):
        source, go = self._fake_build_sources()
        version = subprocess.CompletedProcess([], 0, 'go version go1.27.3 darwin/arm64\n', '')
        with mock.patch.object(frontend, 'ROOT', source), mock.patch.object(frontend.subprocess, 'run', return_value=version), mock.patch.object(frontend.subprocess, 'Popen') as launch:
            self.assertCode('go_1_27_4_or_newer_required', frontend.build_frontend, self.config, go)
            self.assertFalse(launch.called)
            with (source / 'phone-tsnet' / 'go.mod').open('a') as file:
                file.write('replace tailscale.com => ../other\n')
            self.assertCode('frontend_dependency_pin_mismatch', frontend.build_frontend, self.config, go)
            self.assertFalse(launch.called)

    def test_build_record_or_callback_failure_cleans_up_owned_child(self):
        source, go = self._fake_build_sources()
        version = subprocess.CompletedProcess([], 0, 'go version go1.27.4 darwin/arm64\n', '')
        original_write = frontend._write_json
        for failure in ('status.json', 'callback'):
            children = []
            class Build:
                pid = 12345
                returncode = None
                terminated = False
                def __init__(self, argv, **kwargs):
                    children.append(self)
                def poll(self):
                    return self.returncode
                def terminate(self):
                    self.terminated = True
                    self.returncode = -15
                def wait(self, timeout=None):
                    return self.returncode
            def write(path, data):
                if children and Path(path).name == failure:
                    raise OSError('injected build record failure')
                return original_write(path, data)
            def callback(_):
                if failure == 'callback':
                    raise RuntimeError('injected build callback failure')
            with self.subTest(failure=failure), mock.patch.object(frontend, 'ROOT', source), mock.patch.object(frontend.subprocess, 'run', return_value=version), mock.patch.object(frontend.subprocess, 'Popen', Build), mock.patch.object(frontend, '_write_json', side_effect=write):
                with self.assertRaises((OSError, RuntimeError)):
                    frontend.build_frontend(self.config, go, on_started=callback)
            self.assertEqual(len(children), 1)
            self.assertTrue(children[0].terminated)
            self.assertIsNotNone(children[0].poll())


if __name__ == '__main__':
    unittest.main()
