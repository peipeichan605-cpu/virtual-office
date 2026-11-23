"""Exercise bridge ownership and pipe handling with a synthetic SDK."""
import asyncio
from contextlib import ExitStack
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import cursor_sdk_bridge as adapter


class FakeProcess:
    pid = 17

    def __init__(self):
        self.returncode = None
        self.stderr = asyncio.StreamReader()
        self.done = asyncio.Event()

    def terminate(self):
        self.returncode = 0
        self.stderr.feed_eof()
        self.done.set()

    kill = terminate

    async def wait(self):
        await self.done.wait()
        return self.returncode


class BridgeTests(unittest.TestCase):
    workspace = 'workspace with spaces'
    command = ['bundled/node.exe', 'bundled/dist/bin/cursor-sdk-bridge.js']
    address = 'http://127.0.0.1:49730'
    token = 'fixture-bridge-token'
    reported_pid = FakeProcess.pid

    def setUp(self):
        scope = ExitStack()
        self.addCleanup(scope.close)
        self.clients = Mock()
        self.sdk = ModuleType('cursor_sdk')
        self.sdk.AsyncBridge = SimpleNamespace(launch=AsyncMock(side_effect=self.launch))
        self.sdk.DefaultHttpxClient = Mock()
        self.sdk.BridgeEndpoint = SimpleNamespace
        self.sdk_cleanup_endpoints = []
        self.stderr_drained = threading.Event()
        scope.enter_context(patch.dict(sys.modules, {'cursor_sdk': self.sdk}))
        scope.enter_context(patch.object(adapter, 'WINDOWS', True))
        scope.enter_context(patch.object(adapter, 'windows_bridge_command', return_value=self.command))

    async def launch(self, **kwargs):
        process = FakeProcess()
        read = process.stderr.read

        async def read_checked(*args, **kwargs):
            value = await read(*args, **kwargs)
            if value == b'after discovery':
                self.stderr_drained.set()
            return value

        process.stderr.read = read_checked
        process.stderr.feed_data(b'after discovery')
        self.bridge = SimpleNamespace(
            endpoint=SimpleNamespace(url=self.address, auth_token=self.token, pid=self.reported_pid),
            process=process,
        )
        self.launch_loop = asyncio.get_running_loop()

        async def close():
            self.assertIs(asyncio.get_running_loop(), self.launch_loop)
            if self.bridge.process is not None:
                self.sdk_cleanup_endpoints.append((self.bridge.endpoint.url, self.bridge.endpoint.auth_token))
                process.terminate()
                self.bridge.process = None

        self.bridge.aclose = AsyncMock(side_effect=close)
        return self.bridge

    def test_posix_keeps_managed_synchronous_sdk_client(self):
        with patch.object(adapter, 'WINDOWS', False):
            client, close = adapter.launch_client(self.workspace, self.clients)
        self.assertIs(client, self.clients.launch_bridge.return_value)
        self.clients.launch_bridge.assert_called_once_with(
            workspace=self.workspace, host='127.0.0.1', max_retries=0)
        self.sdk.AsyncBridge.launch.assert_not_awaited()
        close()
        client.close.assert_called_once_with()

    def test_windows_connects_to_owned_endpoint_and_drains_pipe_until_closed(self):
        client, close = adapter.launch_client(self.workspace, self.clients)
        try:
            self.assertIs(client, self.clients.connect.return_value)
            self.sdk.AsyncBridge.launch.assert_awaited_once_with(
                command=self.command, workspace=self.workspace, host='127.0.0.1', timeout=30)
            self.assertTrue(self.stderr_drained.wait(2))
            self.assertFalse(self.launch_loop.is_closed())
            args, kwargs = self.clients.connect.call_args
            self.assertEqual(args or (kwargs['base_url'], kwargs['auth_token']), (self.address, self.token))
            self.assertEqual(kwargs['max_retries'], 0)
            self.assertIs(kwargs['http_client'], self.sdk.DefaultHttpxClient.return_value)
            self.sdk.DefaultHttpxClient.assert_called_once_with(trust_env=False, follow_redirects=False)
        finally:
            close()
        close()
        client.close.assert_called_once_with()
        client.with_options.assert_called_once_with(unary_timeout=2)
        client.with_options.return_value.shutdown.assert_called_once()
        self.sdk.DefaultHttpxClient.return_value.close.assert_called_once_with()
        self.assertEqual(self.sdk_cleanup_endpoints, [('disabled://local', '')])
        self.assertTrue(self.launch_loop.is_closed())

    def test_rejects_loopback_endpoint_owned_by_a_different_process(self):
        self.reported_pid = FakeProcess.pid + 1
        with self.assertRaisesRegex(RuntimeError, '^cursor_sdk_bridge_endpoint_invalid$'):
            adapter.launch_client(self.workspace, self.clients)
        self.clients.connect.assert_not_called()
        self.assertEqual(self.sdk_cleanup_endpoints, [('disabled://local', '')])

    def test_launch_error_does_not_expose_discovery_or_child_stderr(self):
        self.sdk.AsyncBridge.launch.side_effect = RuntimeError('synthetic stderr ' + self.token)
        with self.assertRaisesRegex(RuntimeError, '^cursor_sdk_bridge_start_failed$'):
            adapter.launch_client(self.workspace, self.clients)
        self.sdk.AsyncBridge.launch.assert_awaited_once()
        self.clients.connect.assert_not_called()

    def test_rejects_nonlocal_discovery_without_sending_its_token(self):
        self.address = 'http://192.0.2.2:49730'
        with self.assertRaises(Exception) as raised:
            adapter.launch_client(self.workspace, self.clients)
        self.clients.connect.assert_not_called()
        self.assertNotIn(self.token, str(raised.exception))
        self.assertEqual(self.sdk_cleanup_endpoints, [('disabled://local', '')])
        self.assertTrue(self.launch_loop.is_closed())

    def test_connect_error_releases_bridge_without_an_agent_or_second_launch(self):
        self.clients.connect.side_effect = RuntimeError('synthetic client construction failure')
        with self.assertRaises(Exception):
            adapter.launch_client(self.workspace, self.clients)
        self.sdk.AsyncBridge.launch.assert_awaited_once()
        self.sdk.DefaultHttpxClient.return_value.close.assert_called_once_with()
        self.assertEqual(self.sdk_cleanup_endpoints, [('disabled://local', '')])
        self.assertTrue(self.launch_loop.is_closed())


class WindowsBundleTests(unittest.TestCase):
    def test_uses_packaged_node_and_script_separate_argv_even_in_paths_with_spaces(self):
        with TemporaryDirectory(prefix='bridge fixture ') as tmp:
            bundle = Path(tmp) / 'cursor_sdk' / '_vendor' / 'bridge'
            binary = bundle / 'bin' / 'node.exe'
            script = bundle / 'dist' / 'bin' / 'cursor-sdk-bridge.js'
            binary.parent.mkdir(parents=True)
            binary.touch()
            script.parent.mkdir(parents=True)
            package = Mock()
            package.locate_file.return_value = bundle
            with patch.object(adapter, 'distribution', return_value=package) as distribution:
                with self.assertRaisesRegex(RuntimeError, '^cursor_sdk_bridge_bundle_missing$'):
                    adapter.windows_bridge_command()
                script.touch()
                self.assertEqual(adapter.windows_bridge_command(), [str(binary), str(script)])
            distribution.assert_called_with('cursor-sdk')
            package.locate_file.assert_called_with('cursor_sdk/_vendor/bridge')


if __name__ == '__main__':
    unittest.main()
