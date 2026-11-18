"""Portable probe timing and Windows lifecycle regressions; no real Codex process."""
import io
import queue
import subprocess
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import codex_background
import codex_background_connections as connections
import codex_rpc


def connection(callback):
    value = connections.CodexBackgroundConnections.__new__(connections.CodexBackgroundConnections)
    value.bridge = SimpleNamespace(config={})
    value.runtime_probe = callback
    value._default_runtime_probe = False
    value._probe_lock = threading.RLock()
    value._probe_cache = None
    return value


def unstarted_rpc(deadline=None):
    value = codex_rpc.StdioRpc.__new__(codex_rpc.StdioRpc)
    value.deadline = deadline
    value.lock = threading.RLock()
    value.write_lock = threading.Lock()
    value.pending = {}
    value.next_id = 0
    value.closed = threading.Event()
    value.writes = queue.Queue(maxsize=32)
    return value


class ProbeBudgetTests(unittest.TestCase):
    def test_expired_probe_never_spawns_native_process(self):
        with patch('codex_rpc._desktop_candidates', return_value=[]), \
                patch('codex_rpc.subprocess.Popen') as popen, \
                patch('codex_rpc.subprocess.run') as run:
            result = codex_background.probe(deadline=time.monotonic() - 1)
        self.assertFalse(result['available'])
        self.assertEqual(result['reason'], 'codex_rpc_timeout')
        popen.assert_not_called()
        run.assert_not_called()

    def test_native_probe_lock_wait_uses_same_deadline(self):
        held = threading.Lock()
        held.acquire()
        before = time.monotonic()
        with patch('codex_background._PROBE_LOCK', held), \
                patch('codex_background.resolve_binary', return_value='/fixture/codex'), \
                patch('codex_background.subprocess.run') as run:
            result = codex_background.probe(deadline=before + 0.02)
        held.release()
        self.assertLess(time.monotonic() - before, 0.5)
        self.assertEqual(result['reason'], 'codex_rpc_timeout')
        run.assert_not_called()

    def test_all_metadata_stages_share_discovery_deadline(self):
        now, seen, calls, closed = [100.0], [], [], []
        clock = SimpleNamespace(monotonic=lambda: now[0])
        deadline = 145.0

        def resolve(settings, *, deadline):
            seen.append(('resolve', deadline))
            now[0] += 4
            return '/fixture/codex'

        def version(argv, **kwargs):
            seen.append(('version_timeout', kwargs['timeout']))
            now[0] += 1
            return SimpleNamespace(returncode=0, stdout='codex-cli 0.145.0\n')

        class MetadataRpc:
            def __init__(self, *args, deadline):
                self.deadline = deadline
                seen.append(('stdio', deadline))

            def spend(self, seconds):
                budget = codex_rpc.remaining_timeout(self.deadline, seconds)
                now[0] += budget
                if budget < seconds:
                    raise codex_rpc.RpcError('codex_rpc_timeout', sent=True)

            def initialize(self):
                self.spend(15)

            def call(self, method, params, timeout=20):
                calls.append(method)
                self.spend(15)
                return {'account': {'type': 'chatgpt'}} if method == 'account/read' else {'config': {}}

            def close(self):
                closed.append(True)

        with patch('codex_background.time', clock), patch('codex_rpc.time', clock), \
                patch.dict(codex_background._PROBES, {}, clear=True), \
                patch('codex_background.resolve_binary', side_effect=resolve), \
                patch('codex_background.subprocess.run', side_effect=version), \
                patch('codex_background.StdioRpc', MetadataRpc):
            result = codex_background.probe(deadline=deadline)
        self.assertEqual(seen, [('resolve', deadline), ('version_timeout', 5), ('stdio', deadline)])
        self.assertEqual(now[0], deadline)
        self.assertEqual(calls, ['account/read', 'config/read'])
        self.assertEqual(closed, [True])
        self.assertEqual(result['reason'], 'codex_rpc_timeout')
        self.assertFalse(result['available'])

    def test_connection_discovery_and_nested_probe_receive_one_deadline(self):
        seen = []

        def resolve(settings, *, deadline):
            seen.append(deadline)
            return '/fixture/codex'

        def default(settings, *, deadline):
            seen.append(deadline)
            return {'available': True, 'logged_in': True}

        value = connection(default)
        value._default_runtime_probe = True
        with patch.object(connections, '_default_probe', default), \
                patch('codex_rpc.resolve_binary', side_effect=resolve):
            result = value._probe()
        self.assertTrue(result['available'])
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1])

    def test_concurrent_status_readers_share_a_single_cached_probe(self):
        started, release = threading.Event(), threading.Event()
        calls, results = [], []

        def callback(settings):
            calls.append(settings)
            started.set()
            if not release.wait(1):
                raise AssertionError('test did not release probe')
            return {'available': True, 'logged_in': True, 'model': 'fixture-model'}

        value = connection(callback)
        readers = [threading.Thread(target=lambda: results.append(value._probe())) for _ in range(3)]
        try:
            for reader in readers:
                reader.start()
            self.assertTrue(started.wait(1))
        finally:
            release.set()
            for reader in readers:
                reader.join(1)
        self.assertTrue(all(not reader.is_alive() for reader in readers))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(row['available'] for row in results))
        results[0]['models'].append({'id': 'changed'})
        self.assertEqual(results[1]['models'], [])

    def test_busy_connection_probe_returns_safe_bounded_metadata(self):
        callback = Mock()
        value = connection(callback)
        value._probe_lock = threading.Lock()
        value._probe_lock.acquire()
        before = time.monotonic()
        try:
            with patch.object(connections, 'PROBE_TIMEOUT', 0.02):
                result = value._probe()
        finally:
            value._probe_lock.release()
        self.assertLess(time.monotonic() - before, 0.5)
        self.assertEqual(result['reason'], 'codex_rpc_timeout')
        self.assertEqual(result['models'], [])
        self.assertFalse(result['available'])
        callback.assert_not_called()

    def test_stdio_read_deadline_is_shorter_than_per_call_timeout_and_preserves_sent(self):
        client = unstarted_rpc(time.monotonic() + 0.02)
        client._write = Mock()
        before = time.monotonic()
        with self.assertRaises(codex_rpc.RpcError) as error:
            client.call('account/read', {}, timeout=20)
        self.assertLess(time.monotonic() - before, 0.5)
        self.assertEqual(error.exception.code, 'codex_rpc_timeout')
        self.assertTrue(error.exception.sent)
        self.assertEqual(client.pending, {})

    def test_stdio_write_lock_deadline_does_not_claim_an_unsent_request_was_sent(self):
        client = unstarted_rpc(time.monotonic() + 0.02)
        client.write_lock.acquire()
        try:
            with self.assertRaises(codex_rpc.RpcError) as error:
                client.call('account/read', {}, timeout=20)
        finally:
            client.write_lock.release()
        self.assertEqual(error.exception.code, 'codex_rpc_timeout')
        self.assertFalse(error.exception.sent)
        self.assertTrue(client.writes.empty())

    def test_expired_stdio_constructor_never_launches_a_process(self):
        with patch('codex_rpc.subprocess.Popen') as popen:
            with self.assertRaises(codex_rpc.RpcError):
                codex_rpc.StdioRpc('/fixture/codex', '/fixture', deadline=time.monotonic() - 1)
        popen.assert_not_called()


class WindowsLifecycleTests(unittest.TestCase):
    def test_force_cleanup_uses_owned_child_kill_without_posix_signals(self):
        client = unstarted_rpc()
        child = Mock()
        child.stdin, child.stdout, child.stderr = io.BytesIO(), io.BytesIO(), io.BytesIO()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired('fixture', 2), 0]
        client.child = child
        with patch('codex_rpc.os', SimpleNamespace(name='nt')), patch('codex_rpc.signal', SimpleNamespace()):
            client.close()
        child.terminate.assert_called_once_with()
        child.kill.assert_called_once_with()
        self.assertEqual(child.wait.call_count, 2)
        self.assertTrue(all(stream.closed for stream in (child.stdin, child.stdout, child.stderr)))

    def test_windows_batch_launcher_is_rejected_before_process_creation(self):
        with patch('codex_rpc.os', SimpleNamespace(name='nt')), patch('codex_rpc.subprocess.Popen') as popen:
            for binary in ['C:\\tools\\codex.CMD', 'C:\\tools\\codex.bat']:
                with self.assertRaises(codex_rpc.RpcError) as error:
                    codex_rpc.StdioRpc(binary, 'C:\\project & work')
                self.assertEqual(error.exception.code, 'codex_native_executable_required')
        popen.assert_not_called()

    def test_native_windows_launcher_keeps_literal_arguments_and_private_stdio(self):
        child = Mock()
        binary, cwd = 'C:\\tools & apps\\codex.exe', 'C:\\project & work'
        overrides = ['model="fixture-model"']
        with patch('codex_rpc.os', SimpleNamespace(name='nt')), \
                patch('codex_rpc.subprocess.CREATE_NO_WINDOW', 0x08000000, create=True), \
                patch('codex_rpc.child_environment', return_value={}), \
                patch('codex_rpc.threading.Thread'), \
                patch('codex_rpc.subprocess.Popen', return_value=child) as popen:
            codex_rpc.StdioRpc(binary, cwd, overrides)
        self.assertEqual(popen.call_args.args[0], [binary, '-c', overrides[0], 'app-server', '--listen', 'stdio://'])
        self.assertEqual(popen.call_args.kwargs['cwd'], cwd)
        self.assertFalse(popen.call_args.kwargs.get('shell', False))
        self.assertFalse(popen.call_args.kwargs['start_new_session'])
        self.assertEqual(popen.call_args.kwargs['creationflags'], 0x08000000)
        self.assertEqual(popen.call_args.kwargs['stdin'], subprocess.PIPE)


if __name__ == '__main__':
    unittest.main()
