"""Phone transport contracts using a fake Tailscale status and a loopback-only gateway."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import http.client
import http.server
from http.cookies import SimpleCookie
import io
import ipaddress
import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import phone_peer_gate as gate
from mobile_gateway import BoundedHTTPServer, COOKIE, handler
from mobile_service import MobileService
from test_mobile_service import A, Bridge, IDENTITY
from test_phone_setup_assistant import SETTINGS, network


SIGNATURE = bytes.fromhex('0d0a0d0a000d0a515549540a')
SRC, DST = '100.64.0.2', '100.64.0.1'


def proxy_frame(port, source=SRC, destination=DST):
    src, dst = ipaddress.ip_address(source), ipaddress.ip_address(destination)
    family = 0x11 if src.version == 4 else 0x21
    addresses = src.packed + dst.packed + struct.pack('!HH', 51827, port)
    return SIGNATURE + struct.pack('!BBH', 0x21, family, len(addresses)) + addresses


class PeerGateTests(unittest.TestCase):
    def test_settings_select_specific_distinct_devices(self):
        valid = {'version': 1, 'host_id': 'nHost', 'node_ids': ['nPhone']}
        self.assertEqual(gate.validate_peer_gate(valid), valid)
        for fields in ({'version': True}, {'version': 2}, {'node_ids': ['nPhone', 'nPhone']},
                       {'node_ids': ['nHost']}, {'host_id': ''}, {'node_ids': 'nPhone'},
                       {'node_ids': []}, {'node_ids': ['']}, {'unknown': 1}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                gate.validate_peer_gate({**valid, **fields})

    def test_tls_forwarder_ipv4_and_ipv6_framing_consumes_only_preamble(self):
        cases = [(SRC, DST), ('fd7a:115c:a1e0::2', 'fd7a:115c:a1e0::1')]
        for source, destination in cases:
            with self.subTest(source=source):
                stream = io.BytesIO(proxy_frame(4319, source, destination) + b'GET /mobile/')
                self.assertEqual(gate.read_proxy_v2(stream, 4319), (source, destination))
                self.assertEqual(stream.read(), b'GET /mobile/')

    def test_unsupported_transport_fields_and_truncated_preambles_are_rejected(self):
        valid = proxy_frame(4319)
        cases = [valid[:15], valid[:-1], bytes(12) + valid[12:],
                 valid[:12] + b'\x20' + valid[13:], valid[:13] + b'\x12' + valid[14:],
                 valid[:14] + struct.pack('!H', 13) + valid[16:] + b'\x00', proxy_frame(4320)]
        for index, data in enumerate(cases):
            with self.subTest(index=index), self.assertRaises((ValueError, PermissionError, EOFError)):
                gate.read_proxy_v2(io.BytesIO(data), 4319)

    def test_node_lookup_verifies_both_ends_and_invalidates_old_status(self):
        status = network()
        read, clock = Mock(return_value=status), Mock(return_value=40.)
        peers = gate.PeerGate('computer', SETTINGS, read_status=read, clock=clock)
        self.assertEqual(peers.resolve(SRC, DST), 'phone')
        self.assertIsNone(peers.resolve('100.64.0.8', DST))
        self.assertIsNone(peers.resolve(SRC, '100.64.0.9'))
        self.assertEqual(read.call_count, 1)
        clock.return_value = 50.
        status['Peer']['phone']['Online'] = False
        self.assertIsNone(peers.resolve(SRC, DST))
        self.assertEqual(read.call_count, 2)
        clock.return_value = 60.
        status['Self']['ID'] = 'replacement-host'
        self.assertIsNone(peers.resolve('100.64.0.3', DST))

    def test_expiry_applies_while_a_native_snapshot_is_cached(self):
        status = network()
        now = datetime.now(timezone.utc).timestamp()
        expires = datetime.fromtimestamp(now + 120, timezone.utc).isoformat()
        status['Peer']['phone']['KeyExpiry'] = expires
        wall, clock = Mock(return_value=now), Mock(return_value=20.)
        peers = gate.PeerGate('computer', SETTINGS, read_status=Mock(return_value=status),
                              clock=clock, wall_clock=wall)
        self.assertEqual(peers.resolve(SRC, DST), 'phone')
        wall.return_value = now + 121
        self.assertIsNone(peers.resolve(SRC, DST))

    def test_parallel_connections_share_one_native_observation(self):
        start = threading.Barrier(5, timeout=5)
        read = Mock(return_value=network())
        peers = gate.PeerGate('computer', SETTINGS, read_status=read, clock=lambda: 40.)

        def open_connection(source):
            start.wait()
            return peers.resolve(source, DST)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = [pool.submit(open_connection, address)
                       for address in (SRC, '100.64.0.3', '100.64.0.8', SRC)]
            start.wait()
            self.assertEqual([item.result(timeout=5) for item in results], ['phone', 'iPhone', None, 'phone'])
        read.assert_called_once_with()

    def test_overlapping_observations_and_failed_recheck_do_not_reuse_a_grant(self):
        status = network()
        read, clock = Mock(return_value=status), Mock(return_value=40.)
        peers = gate.PeerGate('computer', SETTINGS, read_status=read, clock=clock)
        self.assertEqual(peers.resolve(SRC, DST), 'phone')
        clock.return_value = 50.
        read.side_effect = OSError('test transport offline')
        for addr in (SRC, '100.64.0.3', '100.64.0.9'):
            self.assertIsNone(peers.resolve(addr, DST))
        self.assertEqual(read.call_count, 2)
        clock.return_value = 60.
        read.side_effect = None
        status['Peer']['unrelated'] = dict(status['Peer']['phone'], ID='other', UserID=25)
        self.assertIsNone(peers.resolve(SRC, DST))
        self.assertEqual(peers.resolve('100.64.0.3', DST), 'iPhone')


class PhonePeerGatewayHTTPTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bridge = Bridge(Path(temp.name))
        self.addCleanup(self.bridge.history.close)
        self.addCleanup(self.bridge.commands.close)
        self.service = MobileService(self.bridge)
        self.addCleanup(self.service.close)
        self.server = BoundedHTTPServer(('127.0.0.1', 0), http.server.BaseHTTPRequestHandler)
        self.addCleanup(self.server.server_close)
        self.port = self.server.server_port
        self.config = self.bridge.config
        self.config['mobile']['port'] = self.port
        self.config['mobile_peer_gate'] = {'version': 1, 'host_id': 'nHost', 'node_ids': ['nPhone']}
        self.bridge.phone_onboarding = Mock()
        self.bridge.phone_onboarding.pairing_ready.side_effect = lambda node_id: node_id == 'nPhone'
        resolver = patch.object(gate.PeerGate, 'resolve', return_value='nPhone')
        self.resolve = resolver.start()
        self.addCleanup(resolver.stop)
        self.static_root = Path(temp.name)
        self.server.RequestHandlerClass = handler(self.service, self.config, self.static_root)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def request(self, path, *, framed=True, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        try:
            conn.connect()
            if framed:
                conn.sock.sendall(proxy_frame(self.port))
            fields = {'Host': 'office.example.ts.net', **(headers or {})}
            payload = None
            if body is not None:
                fields.update({'Content-Type': 'application/json', 'Origin': 'https://office.example.ts.net'})
                payload = json.dumps(body).encode()
            conn.request('POST' if payload is not None else 'GET', path, payload, fields)
            response = conn.getresponse()
            return response.status, response.getheaders(), response.read()
        except (http.client.RemoteDisconnected, ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            return None, [], b''
        finally:
            conn.close()

    def test_same_account_phones_have_separate_asset_budgets_and_share_global_limit(self):
        self.config['mobile_peer_gate']['node_ids'].append('other-approved-phone')
        css = self.static_root / 'office' / 'mobile.css'
        css.parent.mkdir(exist_ok=True)
        css.write_text('body {}', encoding='utf-8')
        auth = self.service.auth
        auth.RATE_LIMITS = {**auth.RATE_LIMITS, 'asset': (2, 60)}
        auth.GLOBAL_LIMITS = {**auth.GLOBAL_LIMITS, 'asset': 3}

        self.assertEqual(self.request('/office/mobile.css')[0], 200)
        self.assertEqual(self.request('/office/mobile.css')[0], 200)
        self.assertEqual(self.request('/office/mobile.css')[0], 429)
        self.resolve.return_value = 'other-approved-phone'
        self.assertEqual(self.request('/office/mobile.css')[0], 200)
        self.assertEqual(self.request('/office/mobile.css')[0], 429)

    def test_selected_phone_pairs_and_reopens_session_then_loses_transport_permission(self):
        self.config['mobile_peer_gate']['node_ids'].append('other-approved-phone')
        pair = self.service.owner_create_pairing({'identity': IDENTITY, 'label': 'test phone',
                                                  'view_agents': [A], 'send_agents': [], 'node_id': 'nPhone'})
        self.resolve.return_value = 'other-approved-phone'
        self.assertEqual(self.request('/api/mobile/pair', body={'token': pair['pairing_token']})[0], 401)
        self.resolve.return_value = 'nPhone'
        status, headers, data = self.request('/api/mobile/pair', body={'token': pair['pairing_token']})
        self.assertEqual(status, 200, data)
        self.assertTrue(json.loads(data)['authenticated'])
        cookies = SimpleCookie()
        for key, value in headers:
            if key.lower() == 'set-cookie':
                cookies.load(value)
        cookie = {'Cookie': COOKIE + '=' + cookies[COOKIE].value}
        self.assertEqual(self.request('/api/mobile/session', headers=cookie)[0], 200)
        self.resolve.return_value = 'other-approved-phone'
        self.assertEqual(self.request('/api/mobile/session', headers=cookie)[0], 401)
        self.resolve.return_value = 'nPhone'
        self.config['mobile_peer_gate']['node_ids'] = ['other-approved-phone']
        status, _, data = self.request('/api/mobile/session', headers=cookie)
        self.assertIn(status, {None, 403})
        self.assertNotIn(b'"authenticated": true', data)

    def test_restored_history_page_can_boot_only_for_the_remembered_phone(self):
        from mobile_gateway import RENEW_COOKIE
        self.config['mobile_peer_gate']['node_ids'].append('other-approved-phone')
        shell = self.static_root / 'office' / 'mobile-agent.html'
        shell.parent.mkdir()
        shell.write_text('<head></head><script type="module" src="/office/agent.js"></script>', encoding='utf-8')
        invitation = self.service.owner_create_pairing({
            'identity': IDENTITY, 'label': 'remembered phone', 'view_agents': [A],
            'send_agents': [], 'node_id': 'nPhone',
        })
        status, headers, data = self.request('/api/mobile/pair', body={
            'token': invitation['pairing_token'], 'remember_device': True,
        })
        self.assertEqual(status, 200)
        device_id = json.loads(data)['device']['device_id']
        jar = SimpleCookie()
        for key, value in headers:
            if key.lower() == 'set-cookie':
                jar.load(value)
        renewal = {'Cookie': RENEW_COOKIE + '=' + jar[RENEW_COOKIE].value}
        self.service.auth.db.execute('UPDATE mobile_devices SET last_seen=last_seen-? WHERE id=?',
                                     (self.service.auth.idle_ttl + 1, device_id))
        history_page = '/mobile/agent?agent=' + A
        status, headers, data = self.request(history_page, headers=renewal)
        self.assertEqual(status, 200)
        self.assertIn(b'/office/agent.js', data)
        self.assertFalse(any(key.lower() == 'set-cookie' for key, _ in headers))
        status, _, data = self.request('/api/mobile/history?agent=' + A, headers=renewal)
        self.assertEqual((status, json.loads(data)['error']), (401, 'mobile_session_renewal_required'))

        self.resolve.return_value = 'other-approved-phone'
        self.assertEqual(self.request(history_page, headers=renewal)[0], 401)
        self.resolve.return_value = 'nPhone'
        status, _, data = self.request('/api/mobile/renew', body={},
                                       headers={**renewal, 'X-Mobile-Renew': '1'})
        self.assertEqual(status, 200)
        device = json.loads(data)['device']
        self.assertEqual(device['device_id'], device_id)
        with patch.object(self.service.auth, 'clock', return_value=device['renewal_expires_at'] + 1):
            self.assertEqual(self.request(history_page, headers=renewal)[0], 401)
        self.service.auth.revoke(device_id)
        self.assertEqual(self.request(history_page, headers=renewal)[0], 401)
        self.assertEqual(self.request(history_page)[0], 401)

    def test_native_transport_and_application_session_are_separate_checks(self):
        self.assertEqual(self.request('/api/mobile/session')[0], 401)
        for extra in ({'Tailscale-User-Login': IDENTITY}, {'X-Forwarded-For': SRC},
                      {'Forwarded': 'for=' + SRC}):
            with self.subTest(extra=extra):
                self.assertIn(self.request('/api/mobile/session', headers=extra)[0], {None, 403})
        self.resolve.return_value = None
        self.assertIn(self.request('/api/mobile/session')[0], {None, 403})

    def test_local_setup_proof_is_disjoint_from_the_proxied_phone_routes(self):
        from phone_setup_assistant import gateway_proof, gateway_proof_request
        nonce = '6d' * 32
        headers = {'Host': '127.0.0.1:' + str(self.port), 'X-Office-Setup-Nonce': nonce,
                   'X-Office-Setup-Request': gateway_proof_request(self.config, nonce)}
        path = '/_office/local/gateway-proof'
        status, _, body = self.request(path, framed=False, headers=headers)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body), {'proof': gateway_proof(self.config, nonce)})
        self.assertIn(self.request(path, headers=headers)[0], {None, 403})
        self.assertIn(self.request('/api/mobile/session', framed=False)[0], {None, 403})


if __name__ == '__main__':
    unittest.main()
