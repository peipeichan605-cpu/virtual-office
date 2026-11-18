import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from command_adapters import DeliveryError
from grok_sdk import GrokSdkAdapter, GrokSdkClient, GrokSdkError, _environment, _vendor_valid


class FakeClient:
    def __init__(self):
        self.state = {'state': 'authenticated', 'account_fingerprint': 'a' * 64}
        self.sent = []
        self.error = None
    def auth_status(self): return self.state
    def send_message(self, bot, session, message, text):
        self.sent.append((bot, session, message, text))
        if self.error: raise self.error
        return {'botId': bot, 'messageId': message, 'delivery': 'accepted-box', 'dispatched': True}


class GrokSdkTests(unittest.TestCase):
    def config(self, root):
        return {'data_dir': root, 'command_adapters': {'grok_sdk': {'sessions': {'sdk-test':
            {'bot_id': 'bot-1', 'native_session_id': '', 'account_fingerprint': 'a' * 64}}}}}

    def test_pinned_runtime_integrity(self):
        self.assertTrue(_vendor_valid())

    def test_no_node_loader_or_credentials_in_environment(self):
        with patch.dict('os.environ', {'NODE_OPTIONS': '--require /evil', 'XAI_API_KEY': 'secret', 'HTTPS_PROXY': 'bad'}):
            env = _environment()
        for key in ('NODE_OPTIONS', 'XAI_API_KEY', 'HTTPS_PROXY'): self.assertNotIn(key, env)

    def test_adapter_preserves_exact_identity_and_does_not_claim_completion(self):
        with tempfile.TemporaryDirectory() as root:
            client = FakeClient(); events = []
            adapter = GrokSdkAdapter(self.config(root), events.append, client)
            command = str(uuid.uuid4()); text = '[OFFICE:' + command + ']\nTest'
            result = adapter.submit('sdk-test', text)
            self.assertEqual(client.sent, [('bot-1', '', command, text)])
            self.assertEqual(result['native_message_id'], command)
            self.assertEqual(result['attention_code'], 'awaiting_native_confirmation')
            self.assertEqual(events, [])

    def test_account_change_expiry_and_missing_session_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            client = FakeClient(); adapter = GrokSdkAdapter(self.config(root), client=client)
            self.assertTrue(adapter.native_verified('sdk-test'))
            client.state['account_fingerprint'] = 'b' * 64
            self.assertFalse(adapter.available('sdk-test')); self.assertEqual(adapter.reason, 'grok_sdk_account_changed')
            client.state = {'state': 'expired', 'reason': 'grok_sdk_login_required'}
            self.assertFalse(adapter.available('sdk-test')); self.assertEqual(adapter.reason, 'grok_sdk_login_required')
            del adapter.sessions['sdk-test']['native_session_id']
            self.assertFalse(adapter.available('sdk-test'))

    def test_uncertain_send_is_not_retried(self):
        with tempfile.TemporaryDirectory() as root:
            client = FakeClient(); client.error = GrokSdkError('grok_sdk_delivery_unknown', uncertain=True)
            adapter = GrokSdkAdapter(self.config(root), client=client)
            with self.assertRaises(DeliveryError) as caught:
                adapter.submit('sdk-test', '[OFFICE:' + str(uuid.uuid4()) + ']\nTest')
            self.assertTrue(caught.exception.uncertain); self.assertEqual(len(client.sent), 1)

    def test_rpc_errors_are_fixed_codes_and_no_raw_errors_escape(self):
        with tempfile.TemporaryDirectory() as root:
            client = GrokSdkClient(self.config(root), runner=lambda request: {'ok': False, 'error': 'secret https://private'})
            with self.assertRaises(GrokSdkError) as caught: client.list_bots()
            self.assertEqual(str(caught.exception), 'grok_sdk_worker_failed')

    def test_authentication_status_reads_local_worker_only_and_caches(self):
        with tempfile.TemporaryDirectory() as root:
            calls = []
            def runner(request):
                calls.append(request)
                return {'ok': True, 'result': {'state': 'authenticated', 'account_fingerprint': 'a' * 64}}
            client = GrokSdkClient(self.config(root), runner=runner)
            self.assertEqual(client.auth_status()['state'], 'authenticated')
            client.auth_status()
            self.assertEqual([r['action'] for r in calls], ['auth_status'])

    def test_sensitive_values_never_in_safe_status(self):
        with tempfile.TemporaryDirectory() as root:
            client = GrokSdkClient(self.config(root)); client._prepare()
            client.credential_file.write_text(json.dumps({'accessToken': 'token-A', 'refreshToken': 'token-B'}))
            client.credential_file.chmod(0o600)
            self.assertEqual(client.redaction_secrets(), ['token-A', 'token-B'])


if __name__ == '__main__': unittest.main()
