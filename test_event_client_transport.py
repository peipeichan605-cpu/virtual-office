"""Synthetic loopback transport checks; no native App or real credentials."""
import importlib.util
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


SPEC = importlib.util.spec_from_file_location(
    'fixture_event_client_transport', Path(__file__).parent / 'adapters/client.py')
client = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client)


class EventClientTransportTests(unittest.TestCase):
    def setUp(self):
        self.servers = []
        self.seen = []

    def tearDown(self):
        for server, thread in self.servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def server(self, redirect=None, sink=False):
        seen = self.seen

        class Handler(BaseHTTPRequestHandler):
            def handle_request(self):
                length = int(self.headers.get('Content-Length', '0'))
                body = self.rfile.read(length) if length else b''
                seen.append({'sink': sink, 'method': self.command,
                             'authorization': self.headers.get('Authorization'),
                             'agent_id': self.headers.get('X-Agent-Id'), 'body': body})
                if redirect:
                    self.send_response(302)
                    self.send_header('Location', redirect)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                result = json.dumps({'accepted': True, 'fixture': True}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(result)))
                self.end_headers()
                self.wfile.write(result)

            do_GET = handle_request
            do_POST = handle_request

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return 'http://127.0.0.1:' + str(server.server_port)

    @staticmethod
    def credential(base):
        return {'endpoint': base + '/api/agents/events',
                'agent_id': 'grok_bot:fixture-local-agent',
                'token': 'fixture-local-event-token'}

    def test_valid_local_event_is_accepted_with_exact_public_body(self):
        credential = self.credential(self.server())
        event = {'source_id': 'fixture-local-agent', 'text': '公開進度', 'type': 'message'}
        result = client.send(credential, event)
        self.assertTrue(result['accepted'])
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(self.seen[0]['method'], 'POST')
        self.assertEqual(self.seen[0]['agent_id'], credential['agent_id'])
        self.assertEqual(self.seen[0]['authorization'], 'Bearer ' + credential['token'])
        self.assertEqual(json.loads(self.seen[0]['body']), event)

    def test_redirect_is_refused_without_forwarding_credentials(self):
        sink = self.server(sink=True)
        origin = self.server(redirect=sink + '/fixture-redirect-sink')
        with self.assertRaises(ValueError):
            client.send(self.credential(origin), {'type': 'message', 'text': 'fixture'})
        self.assertEqual(len(self.seen), 1)
        self.assertFalse(self.seen[0]['sink'])
        self.assertFalse(any(request['sink'] for request in self.seen))

    def test_configured_http_proxy_is_ignored(self):
        origin = self.server()
        proxy = self.server(sink=True)
        with patch.object(client.urllib.request, 'getproxies', return_value={'http': proxy}), \
                patch.object(client.urllib.request, 'proxy_bypass', return_value=False):
            result = client.send(self.credential(origin), {'type': 'message', 'text': 'fixture'})
        self.assertTrue(result['accepted'])
        self.assertEqual(len(self.seen), 1)
        self.assertFalse(self.seen[0]['sink'])

    def test_send_revalidates_endpoint_before_creating_network_opener(self):
        credential = self.credential('https://nonlocal.example.invalid')
        with patch.object(client.urllib.request, 'build_opener') as opener:
            with self.assertRaises(ValueError):
                client.send(credential, {'type': 'message', 'text': 'fixture'})
        opener.assert_not_called()


if __name__ == '__main__':
    unittest.main()
