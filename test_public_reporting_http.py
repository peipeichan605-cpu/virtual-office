"""Real loopback setup receipts; no native Bot, memory tool or vendor request."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer

from configuration import initialize, load_config, save_config
from dashboard import Bridge, proxy_handler


class PublicReportingHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.config_path = self.root / 'config.json'
        initialize(self.config_path)
        config = load_config(self.config_path)
        config.update(run_id='reporting-http-' + uuid.uuid4().hex, run_dir=str(self.root / 'run'))
        save_config(self.config_path, config)
        self.bridge = Bridge(config)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), proxy_handler(self.bridge))
        config['port'] = self.server.server_address[1]
        save_config(self.config_path, config)
        self.base = 'http://127.0.0.1:' + str(config['port'])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.agent_id, self.credential, self.setup = self.invite()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.bridge.commands.close()
        self.bridge.history.close()
        self.temp.cleanup()

    def request(self, path, body=None, owner=False, headers=None):
        outgoing = {'Content-Type': 'application/json'}
        if owner:
            outgoing['X-Office-Key'] = self.bridge.config['topic']
        outgoing.update(headers or {})
        req = urllib.request.Request(self.base + path,
            data=json.dumps(body).encode() if body is not None else None, headers=outgoing)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def invite(self):
        code, invitation = self.request('/api/office/invitations', {
            'provider': 'grok_bot', 'name': 'Reporting fixture ' + uuid.uuid4().hex[:8],
            'request_id': 'reporting-' + uuid.uuid4().hex}, owner=True)
        self.assertEqual(code, 200, invitation)
        path, _, credential_path = self.bridge.invitations.paths(invitation['id'])
        invite = json.loads(path.read_text())
        credential = json.loads(credential_path.read_text())
        code, claimed = self.request('/api/agents/join', {'claim_id': uuid.uuid4().hex},
            headers={'X-Invitation-Id': invitation['id'], 'Authorization': 'Bearer ' + invite['token']})
        self.assertEqual(code, 200, claimed)
        setup = self.bridge.get_public_reporting().ensure(claimed['agent_id'])
        return claimed['agent_id'], credential, setup

    def body(self, setup=None, agent_id=None):
        setup = setup or self.setup
        return {key: setup[key] for key in ('setup_id', 'policy_version', 'policy_hash')} | {
            'readback_agent_id': agent_id or self.agent_id,
            'readback_policy_version': setup['policy_version'], 'evidence': 'agent_reported_saved'}

    def confirm(self, body=None, credential=None, headers=None):
        credential = credential or self.credential
        outgoing = {'X-Agent-Id': credential['agent_id'], 'Authorization': 'Bearer ' + credential['token']}
        outgoing.update(headers or {})
        return self.request('/api/agents/reporting/setup', body or self.body(), headers=outgoing)

    def assert_no_work_dispatched(self):
        self.assertEqual(self.bridge.commands.db.execute('SELECT count(*) FROM commands').fetchone()[0], 0)
        self.assertEqual(self.bridge.history.db.execute('SELECT count(*) FROM runs').fetchone()[0], 0)
        self.assertEqual(self.bridge.history.db.execute('SELECT count(*) FROM messages').fetchone()[0], 0)

    def helper(self, *args):
        manifest = json.loads(Path(self.setup['manifest_path']).read_text())
        return subprocess.run([sys.executable, str(Path(__file__).parent / 'adapters/reporting_setup.py'),
            '--manifest', self.setup['manifest_path'], 'confirm',
            '--readback-version', manifest['policy_version'], '--readback-agent', self.agent_id, *args],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)

    def test_only_original_event_credential_can_confirm_setup(self):
        route = '/api/agents/reporting/setup'
        self.assertEqual(self.request(route, self.body())[0], 403)
        self.assertEqual(self.request(route, self.body(), owner=True)[0], 403)
        self.assertEqual(self.confirm(headers={'Authorization': 'Bearer invalid-fixture-token'})[0], 403)
        self.assertEqual(self.confirm(headers={'X-Agent-Id': 'grok_bot:unselected-fixture'})[0], 403)
        code, conversation = self.request('/api/office/conversation', {'agent_id': self.agent_id}, owner=True)
        self.assertEqual(code, 200, conversation)
        inbox = json.loads(Path(conversation['credential_path']).read_text())
        self.assertEqual(self.confirm(credential=inbox)[0], 403, 'Inbox identity must not confirm public reporting')
        self.assertEqual(self.bridge.get_public_reporting().status(self.agent_id)['state'], 'setup_required')
        self.assert_no_work_dispatched()

    def test_foreign_origin_is_rejected_even_with_valid_event_identity(self):
        self.assertEqual(self.confirm(headers={'Origin': 'https://foreign.example'})[0], 403)
        self.assertEqual(self.bridge.get_public_reporting().status(self.agent_id)['state'], 'setup_required')
        self.assert_no_work_dispatched()

    def test_exact_confirmation_and_duplicate_keep_cooperative_evidence_only(self):
        code, first = self.confirm()
        self.assertEqual(code, 200, first)
        self.assertEqual(first['state'], 'agent_confirmed')
        self.assertEqual(first['evidence'], 'agent_reported_saved')
        self.assertFalse(first['native_observer'])
        self.assertFalse(first['manual_work_verified'])
        code, again = self.confirm()
        self.assertEqual(code, 200, again)
        self.assertEqual(first, again)
        self.assertEqual(self.bridge.get_public_reporting().status(self.agent_id)['confirmed_at'], first['confirmed_at'])
        for secret in (self.credential['token'], self.bridge.config['topic']):
            self.assertNotIn(secret, json.dumps(first))
        self.assert_no_work_dispatched()

    def test_policy_nonce_readback_and_evidence_mismatches_do_not_confirm(self):
        cases = {
            'setup_id': uuid.uuid4().hex, 'policy_version': 'old-policy-version',
            'policy_hash': '0' * 64, 'readback_agent_id': 'grok_bot:another-fixture',
            'readback_policy_version': 'old-policy-version', 'evidence': 'native_hook',
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                code, result = self.confirm(self.body() | {field: value})
                self.assertEqual(code, 409, result)
                self.assertEqual(self.bridge.get_public_reporting().status(self.agent_id)['state'], 'setup_required')
        self.assertEqual(self.confirm(self.body() | {'native_verified': True})[0], 400)
        self.assert_no_work_dispatched()

    def test_another_selected_agent_cannot_reuse_this_setup_receipt(self):
        other_id, other_credential, _ = self.invite()
        code, result = self.confirm(self.body(), credential=other_credential)
        self.assertEqual(code, 409, result)
        self.assertEqual(self.bridge.get_public_reporting().status(self.agent_id)['state'], 'setup_required')
        self.assertEqual(self.bridge.get_public_reporting().status(other_id)['state'], 'setup_required')
        self.assert_no_work_dispatched()

    def test_logout_rejoin_does_not_revive_or_reuse_the_old_setup(self):
        self.assertEqual(self.confirm()[0], 200)
        code, result = self.request('/api/office/agents/logout', {'agent_id': self.agent_id}, owner=True)
        self.assertEqual(code, 200, result)
        self.assertEqual(self.confirm()[0], 403)
        self.assertEqual(self.bridge.get_public_reporting().status(self.agent_id)['state'], 'revoked')
        new_id, new_credential, new_setup = self.invite()
        self.assertNotEqual(new_id, self.agent_id)
        self.assertEqual(self.confirm(self.body(), credential=new_credential)[0], 409)
        code, result = self.confirm(self.body(new_setup, new_id), credential=new_credential)
        self.assertEqual(code, 200, result)
        self.assertEqual(result['agent_id'], new_id)
        self.assert_no_work_dispatched()

    def test_blackbox_helper_confirms_own_binding_without_exposing_credentials(self):
        result = self.helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        body = json.loads(result.stdout)
        self.assertEqual(body['agent_id'], self.agent_id)
        self.assertEqual(body['state'], 'agent_confirmed')
        self.assertFalse(body['manual_work_verified'])
        for secret in (self.credential['token'], self.bridge.config['topic']):
            self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertEqual(self.helper().returncode, 0)
        self.assert_no_work_dispatched()

    def test_helper_rejects_inbox_credential_and_tampered_endpoint_before_network(self):
        path = Path(self.setup['manifest_path'])
        original = json.loads(path.read_text())
        _, conversation = self.request('/api/office/conversation', {'agent_id': self.agent_id}, owner=True)
        for change in ({'credential_path': conversation['credential_path']},
                       {'confirmation_endpoint': 'https://foreign.example/reporting/setup'}):
            with self.subTest(fields=list(change)):
                path.write_text(json.dumps(original | change))
                result = self.helper()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('OFFICE 公開同步設定未完成', result.stderr)
                self.assertEqual(self.bridge.get_public_reporting().status(self.agent_id)['state'], 'stale')
        path.write_text(json.dumps(original))
        self.assertEqual(self.bridge.get_public_reporting().status(self.agent_id)['state'], 'setup_required')
        self.assert_no_work_dispatched()


if __name__ == '__main__':
    unittest.main()
