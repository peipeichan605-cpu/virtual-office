"""Own the pinned Cursor SDK bridge and its local transport for one run."""
import asyncio
from concurrent.futures import Future
from contextlib import ExitStack, suppress
from importlib.metadata import distribution
import re
import sys
import threading


WINDOWS = sys.platform == 'win32'


def windows_bridge_command():
    # The Windows wheel's .cmd wrapper starts Node as a separate child. Own
    # the bundled interpreter itself, including when the workspace has spaces.
    package = distribution('cursor-sdk')
    bundle = package.locate_file('cursor_sdk/_vendor/bridge')
    command = (bundle / 'bin' / 'node.exe', bundle / 'dist' / 'bin' / 'cursor-sdk-bridge.js')
    if not all(path.is_file() for path in command):
        raise RuntimeError('cursor_sdk_bridge_bundle_missing')
    return [str(path) for path in command]


async def _drain(stderr):
    # Keep the transport flowing for the entire run without saving SDK output.
    while await stderr.read(16384):
        pass


async def _serve(cwd, client_type, connected, stopping):
    from cursor_sdk import AsyncBridge, BridgeEndpoint, DefaultHttpxClient

    bridge = await AsyncBridge.launch(command=windows_bridge_command(), workspace=cwd,
                                      host='127.0.0.1', timeout=30)
    drain = None
    try:
        endpoint, process = bridge.endpoint, bridge.process
        if process is None or process.stderr is None:
            raise ValueError('cursor_sdk_bridge_endpoint_invalid')
        drain = asyncio.create_task(_drain(process.stderr))
        port = re.fullmatch(r'http://127\.0\.0\.1:([1-9][0-9]{0,4})/?', endpoint.url)
        if (not port or int(port[1]) > 65535 or endpoint.pid != process.pid
                or not endpoint.auth_token):
            raise ValueError('cursor_sdk_bridge_endpoint_invalid')
        with ExitStack() as resources:
            http = DefaultHttpxClient(trust_env=False, follow_redirects=False)
            resources.callback(http.close)
            client = client_type.connect(endpoint.url, endpoint.auth_token, max_retries=0, http_client=http)
            resources.callback(client.close)
            connected.set_result(client)
            await asyncio.wrap_future(stopping)
            # Graceful shutdown uses the same authenticated, proxy-free transport.
            with suppress(Exception):
                client.with_options(unary_timeout=2).shutdown()
    finally:
        # SDK 1.3.1 aclose() separately attempts an HTTP shutdown with its own
        # proxy settings. Disable that redundant RPC before it reaps the child
        # and closes SDK callback servers, including on rejected discovery.
        bridge.endpoint = BridgeEndpoint(url='disabled://local', auth_token='')
        try:
            await bridge.aclose()
        finally:
            if drain is not None:
                drain.cancel()
                with suppress(asyncio.CancelledError, OSError):
                    await drain


def launch_client(cwd, client_type):
    if not WINDOWS:
        client = client_type.launch_bridge(workspace=cwd, host='127.0.0.1', max_retries=0)
        return client, client.close

    # The Windows event loop supports subprocess pipes. The SDK's synchronous
    # discovery uses select() on stderr, which Windows only supports on sockets.
    connected, stopping = Future(), Future()
    close_lock = threading.Lock()

    def run():
        try:
            asyncio.run(_serve(cwd, client_type, connected, stopping))
        except Exception as error:
            if not connected.done():
                # SDK startup exceptions can contain raw stderr or command-line
                # arguments. Return only our fixed diagnostic codes.
                known = ('cursor_sdk_bridge_bundle_missing', 'cursor_sdk_bridge_endpoint_invalid')
                code = error.args[0] if len(error.args) == 1 and error.args[0] in known else 'cursor_sdk_bridge_start_failed'
                connected.set_exception(RuntimeError(code))

    worker = threading.Thread(target=run, name='cursor-sdk-bridge', daemon=True)
    worker.start()

    def close():
        with close_lock:
            if not stopping.done():
                stopping.set_result(None)
        # The SDK bounds discovery to 30s and terminate/kill waits to 5s each.
        worker.join(timeout=45)

    try:
        return connected.result(timeout=45), close
    except BaseException:
        close()
        raise
