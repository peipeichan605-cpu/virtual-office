"""Loopback occupancy checks before changing an installation's offline settings."""
import socket
import sys
import unittest
from unittest.mock import MagicMock, call, patch

import office


class WindowsOfflineCheckTests(unittest.TestCase):
    def test_empty_ports_reserved_exclusively_then_closed_without_listening(self):
        contexts = [MagicMock(), MagicMock()]
        with patch.object(office.sys, 'platform', 'win32'), \
                patch.object(socket, 'SO_EXCLUSIVEADDRUSE', -5, create=True), \
                patch.object(socket, 'socket', side_effect=contexts):
            office.require_offline({'port': 4318, 'mobile': {'port': 4329}})
        for ctx, port in zip(contexts, (4318, 4329)):
            self.assertEqual(ctx.__enter__.return_value.method_calls,
                             [call.setsockopt(socket.SOL_SOCKET, -5, 1), call.bind(('127.0.0.1', port))])
            ctx.__exit__.assert_called_once()

    def test_conflicting_and_uncertain_endpoints_block_and_release_both_probes(self):
        for code in (10048, 10013, 10035):
            with self.subTest(code=code):
                contexts = [MagicMock(), MagicMock()]
                contexts[1].__enter__.return_value.bind.side_effect = OSError(code, 'fixture')
                with patch.object(office.sys, 'platform', 'win32'), \
                        patch.object(socket, 'SO_EXCLUSIVEADDRUSE', -5, create=True), \
                        patch.object(socket, 'socket', side_effect=contexts), \
                        self.assertRaisesRegex(ValueError, 'no configuration changed'):
                    office.require_offline({'port': 4318, 'mobile': {'port': 4329}})
                for ctx in contexts:
                    ctx.__exit__.assert_called_once()

    @unittest.skipUnless(sys.platform == 'win32', 'real Winsock exclusive bind')
    def test_native_free_bound_and_listening_port(self):
        with socket.socket() as endpoint:
            endpoint.bind(('127.0.0.1', 0))
            port = endpoint.getsockname()[1]
            with self.assertRaisesRegex(ValueError, 'no configuration changed'):
                office.require_offline({'port': port})
            endpoint.listen(1)
            with self.assertRaisesRegex(ValueError, 'no configuration changed'):
                office.require_offline({'port': port})
        office.require_offline({'port': port})
        with socket.socket() as next_user:
            next_user.bind(('127.0.0.1', port))


if __name__ == '__main__':
    unittest.main()
