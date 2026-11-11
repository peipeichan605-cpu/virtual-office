"""Actual loopback round trips; these fixtures do not contact a vendor or model."""
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from configuration import initialize, load_config, save_config
from dashboard import Bridge, proxy_handler


class FixtureConnection:
    def close(self):
        pass


class FixtureResponse(io.BytesIO):
    status = 200


class LoopbackWebhookTransport:
    """Run the production adapter while routing its dummy URL to our HTTP sink."""
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.calls = 0

    def open(self, method, url, headers, body=None):
        if method != 'POST' or not url.startswith('https://api.x.ai/office-fixture/'):
            raise AssertionError('Fixture must never contact another endpoint')
        self.calls += 1
        request = urllib.request.Request(self.endpoint, data=body, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=4) as response:
            if response.status != 200:
                raise AssertionError('Fixture Webhook did not accept the request')
            data = response.read()
        return FixtureConnection(), FixtureResponse(data)


class ConnectionAssistantHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.config_path = self.root / 'config.json'
        initialize(self.config_path)
        config = load_config(self.config_path)
        config.update(run_id='connection-http-' + uuid.uuid4().hex, run_dir=str(self.root / 'run'))
        save_config(self.config_path, config)
        self.bridge = Bridge(config)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), proxy_handler(self.bridge))
        config['port'] = self.server.server_address[1]
        save_config(self.config_path, config)
        self.base = 'http://127.0.0.1:' + str(config['port'])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.webhooks = []
        received = self.webhooks

        class Sink(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers['Content-Length'])
                received.append(json.loads(self.rfile.read(length)))
                self.send_response(200)
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'{}')

        self.sink = ThreadingHTTPServer(('127.0.0.1', 0), Sink)
        self.sink_thread = threading.Thread(target=self.sink.serve_forever, daemon=True)
        self.sink_thread.start()
        self.transport = LoopbackWebhookTransport('http://127.0.0.1:' + str(self.sink.server_address[1]) + '/webhook')
        self.url = 'https://api.x.ai/office-fixture/' + uuid.uuid4().hex
        self.key = 'fixture-only-key-' + uuid.uuid4().hex
        code, invitation = self.request('/api/office/invitations', {
            'provider': 'grok_bot', 'name': 'Selected fixture ' + uuid.uuid4().hex[:8],
            'request_id': 'invitation-' + uuid.uuid4().hex,
        })
        self.assertEqual(code, 200)
        invite_path, _, credential_path = self.bridge.invitations.paths(invitation['id'])
        invite = json.loads(invite_path.read_text())
        self.credential = json.loads(credential_path.read_text())
        code, joined = self.request('/api/agents/join', {'claim_id': uuid.uuid4().hex}, owner=False,
            headers={'X-Invitation-Id': invitation['id'], 'Authorization': 'Bearer ' + invite['token']})
        self.assertEqual(code, 200)
        self.agent_id = joined['agent_id']
        self.agent = self.bridge.definitions[self.agent_id]
        self.test_id = None
        for kind in ('turn_started', 'turn_completed'):
            code, receipt = self.event(kind, 'Initial local report', run='join-' + invitation['id'])
            self.assertEqual(code, 200)
            self.assertTrue(receipt['accepted'])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.sink.shutdown()
        self.sink.server_close()
        self.sink_thread.join(timeout=3)
        self.bridge.commands.close()
        self.bridge.history.close()
        self.temp.cleanup()

    def request(self, path, body=None, owner=True, headers=None):
        outgoing = {'Content-Type': 'application/json'}
        if owner:
            outgoing['X-Office-Key'] = self.bridge.config['topic']
        outgoing.update(headers or {})
        request = urllib.request.Request(self.base + path,
            data=json.dumps(body).encode() if body is not None else None, headers=outgoing)
        try:
            with urllib.request.urlopen(request, timeout=4) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def event(self, kind, text='', run=None, event_id=None, **extra):
        data = {'source_id': self.agent['source_id'], 'event_id': event_id or str(uuid.uuid4()),
            'run_id': run or self.test_id, 'type': kind, 'text': text, **extra}
        return self.request('/api/agents/events', data, owner=False,
            headers={'X-Agent-Id': self.agent_id, 'Authorization': 'Bearer ' + self.credential['token']})

    def connect(self, **changes):
        result = self.request('/api/office/connections/grok', {
            'agent_id': self.agent_id, 'url': self.url, 'key': self.key, **changes,
        })
        self.assertEqual(result[0], 200, result[1])
        self.bridge.commands.adapters['grok_webhook'].transport = self.transport
        return result[1]

    def status(self):
        code, value = self.request('/api/office/connections/check?agent=' + urllib.parse.quote(self.agent_id, safe=''))
        self.assertEqual(code, 200, value)
        return value

    def start(self):
        self.test_id = str(uuid.uuid4())
        code, value = self.request('/api/office/connections/check', {
            'agent_id': self.agent_id, 'request_id': self.test_id,
        })
        self.assertEqual(code, 202, value)
        return value

    def deliver(self):
        self.bridge.commands.tick()
        self.assertTrue(self.webhooks)
        payload = self.webhooks[-1]
        self.assertEqual(payload['office']['command_id'], self.test_id)
        self.assertEqual(payload['office']['agent_id'], self.agent_id)
        expected = payload['prompt']['text'].splitlines()[-1]
        self.assertRegex(expected, r'^OFFICE_CONNECTION_OK_[0-9a-f]{32}$')
        return payload['prompt']['text'], expected

    def complete(self):
        self.connect()
        self.start()
        prompt, expected = self.deliver()
        self.assertEqual(self.event('turn_started', prompt)[0], 200)
        self.assertEqual(self.event('turn_completed', expected)[0], 200)
        self.assertTrue(self.status()['check']['verified'])
        return prompt, expected

    def test_owner_auth_and_origin_protect_test_creation_and_status(self):
        self.connect()
        route = '/api/office/connections/check'
        body = {'agent_id': self.agent_id, 'request_id': str(uuid.uuid4())}
        self.assertEqual(self.request(route, body, owner=False)[0], 403)
        self.assertEqual(self.request(route, body, owner=False,
            headers={'X-Agent-Id': self.agent_id, 'Authorization': 'Bearer ' + self.credential['token']})[0], 403)
        self.assertEqual(self.request(route, body, headers={'Origin': 'https://untrusted.example'})[0], 403)
        self.assertEqual(self.request(route + '?agent=' + urllib.parse.quote(self.agent_id), owner=False)[0], 403)
        self.assertEqual(self.request(route + '?agent=' + urllib.parse.quote(self.agent_id),
            headers={'Origin': 'https://untrusted.example'})[0], 403)
        self.assertEqual(self.transport.calls, 0)
        self.assertEqual(self.bridge.commands.db.execute('SELECT COUNT(*) FROM connection_checks').fetchone()[0], 0)

    def test_actual_http_receipt_start_reply_and_completion_are_distinct(self):
        self.assertEqual(self.status()['check']['state'], 'unconfigured')
        connected = self.connect()
        self.assertFalse(connected['execution_verified'])
        for _ in range(3):
            value = self.status()
            self.assertEqual(value['check']['state'], 'ready_to_test')
            self.assertFalse(value['capability']['execution_verified'])
        self.assertEqual(self.transport.calls, 0, 'Status polling must not send model work')
        initial = self.start()
        self.assertEqual(initial['check']['test_id'], self.test_id)
        prompt, expected = self.deliver()
        self.assertEqual(self.transport.calls, 1)
        receipt_only = self.status()
        self.assertFalse(receipt_only['check']['verified'])
        self.assertFalse(receipt_only['check']['received'])
        self.assertFalse(receipt_only['check']['replied'])
        self.assertFalse(receipt_only['capability']['execution_verified'])
        # A terminal callback cannot stand in for a missing actual start.
        self.assertEqual(self.event('turn_completed', expected)[0], 409)
        self.assertEqual(self.event('turn_started', prompt + ' altered')[0], 409)
        self.assertEqual(self.event('turn_started', prompt, run=str(uuid.uuid4()))[0], 409)
        self.assertEqual(self.event('turn_started', prompt, source_id='unselected-fixture-source')[0], 400)
        self.assertEqual(self.request('/api/agents/events', {
            'source_id': self.agent['source_id'], 'run_id': self.test_id,
            'event_id': str(uuid.uuid4()), 'type': 'turn_started', 'text': prompt,
        }, owner=False, headers={'X-Agent-Id': 'grok_bot:another-fixture',
            'Authorization': 'Bearer ' + self.credential['token']})[0], 403)
        self.assertFalse(self.status()['check']['received'])
        start_event = str(uuid.uuid4())
        code, started = self.event('turn_started', prompt, event_id=start_event)
        self.assertEqual(code, 200)
        self.assertTrue(started['accepted'])
        self.assertTrue(self.status()['check']['received'])
        self.assertFalse(self.status()['check']['verified'])
        self.assertTrue(self.event('turn_started', prompt, event_id=start_event)[1]['duplicate'])
        self.assertEqual(self.event('message', 'A different answer', role='assistant', phase='final')[0], 200)
        self.assertFalse(self.status()['check']['replied'])
        self.assertEqual(self.event('message', expected, role='assistant', phase='final')[0], 200)
        partial = self.status()['check']
        self.assertTrue(partial['received'])
        self.assertTrue(partial['replied'])
        self.assertFalse(partial['verified'], 'A public final bubble alone is not terminal proof')
        self.assertEqual(self.event('turn_completed', expected)[0], 200)
        final = self.status()
        self.assertEqual(final['check']['state'], 'verified')
        self.assertTrue(final['check']['verified'])
        self.assertTrue(final['capability']['execution_verified'])
        # Retrying the same owner request only reads the existing result.
        for _ in range(2):
            code, replay = self.request('/api/office/connections/check', {
                'agent_id': self.agent_id, 'request_id': self.test_id})
            self.assertEqual(code, 202)
            self.assertTrue(replay['check']['duplicate'])
            self.assertEqual(replay['check']['test_id'], self.test_id)
            self.bridge.commands.tick()
        self.assertEqual(self.transport.calls, 1)
        serialized = json.dumps(final)
        for secret in (self.key, self.url, self.credential['token'], self.bridge.config['topic']):
            self.assertNotIn(secret, serialized)

    def test_wrong_terminal_reply_stays_failed_until_a_new_explicit_test(self):
        self.connect()
        self.start()
        prompt, _ = self.deliver()
        self.assertEqual(self.event('turn_started', prompt)[0], 200)
        self.assertEqual(self.event('turn_completed', 'I completed an unrelated task')[0], 200)
        failed = self.status()
        self.assertEqual(failed['check']['state'], 'failed')
        self.assertFalse(failed['check']['verified'])
        self.assertFalse(failed['capability']['execution_verified'])
        self.assertTrue(failed['check']['can_retry'])
        for _ in range(3):
            self.status()
            self.bridge.commands.tick()
        self.assertEqual(self.transport.calls, 1)
        self.start()
        prompt, expected = self.deliver()
        self.assertEqual(self.event('turn_started', prompt)[0], 200)
        self.assertEqual(self.event('turn_completed', expected)[0], 200)
        self.assertTrue(self.status()['check']['verified'])
        self.assertEqual(self.transport.calls, 2)

    def test_retry_of_older_test_keeps_its_identity_after_a_newer_test_finishes(self):
        self.complete()
        first_id = self.test_id
        self.complete()
        second_id = self.test_id
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(self.status()['check']['test_id'], second_id)
        code, replay = self.request('/api/office/connections/check', {
            'agent_id': self.agent_id, 'request_id': first_id,
        })
        self.assertEqual(code, 202)
        self.assertTrue(replay['check']['duplicate'])
        self.assertTrue(replay['check']['verified'])
        self.assertEqual(replay['check']['test_id'], first_id)
        self.assertEqual(replay['capability']['connection_check']['test_id'], second_id)
        self.bridge.commands.tick()
        self.assertEqual(self.transport.calls, 2, 'Reconciling an older receipt must not submit another test')

    def test_same_detail_save_preserves_proof_but_replacement_at_same_slot_invalidates(self):
        self.complete()
        test_id = self.test_id
        self.connect()
        unchanged = self.status()['check']
        self.assertTrue(unchanged['verified'])
        self.assertEqual(unchanged['test_id'], test_id)
        self.connect(key='fixture-replacement-key-' + uuid.uuid4().hex)
        changed = self.status()
        self.assertEqual(changed['check']['state'], 'ready_to_test')
        self.assertFalse(changed['check']['verified'])
        self.assertFalse(changed['capability']['execution_verified'])
        self.assertIsNone(changed['check']['test_id'])
        self.assertEqual(self.transport.calls, 1, 'Saving replacement details must not silently test')

    def test_disabled_and_logged_out_members_cannot_retain_connected_state(self):
        self.complete()
        config = load_config(self.config_path)
        config['disabled_command_agents'] = [self.agent_id]
        save_config(self.config_path, config)
        self.bridge.refresh_connections(config)
        disabled = self.status()
        self.assertEqual(disabled['check']['state'], 'disabled')
        self.assertFalse(disabled['check']['verified'])
        self.assertFalse(disabled['capability']['execution_verified'])
        self.assertEqual(self.request('/api/office/connections/check', {
            'agent_id': self.agent_id, 'request_id': str(uuid.uuid4())})[0], 409)
        config['disabled_command_agents'] = []
        save_config(self.config_path, config)
        self.bridge.refresh_connections(config)
        code, result = self.request('/api/office/agents/logout', {'agent_id': self.agent_id})
        self.assertEqual(code, 200)
        self.assertTrue(result['logged_out'])
        code, retired = self.request('/api/office/connections/check?agent=' + urllib.parse.quote(self.agent_id, safe=''))
        self.assertEqual(code, 404)
        self.assertEqual(retired['error'], 'agent_not_selected')
        self.assertEqual(self.request('/api/office/connections/check', {
            'agent_id': self.agent_id, 'request_id': str(uuid.uuid4())})[0], 404)
        self.assertEqual(self.transport.calls, 1)


if __name__ == '__main__':
    unittest.main()
