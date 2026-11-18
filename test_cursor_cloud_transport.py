"""Cloud transport regressions; synthetic fixtures never invoke a vendor."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from command_adapters import DeliveryError
from platform_adapters import CursorCloudAdapter, RemoteError


SID = 'bc-00000000-0000-4000-8000-000000000001'
NATIVE = 'run-fixture-native'
COMMAND = '00000000-0000-4000-8000-000000000002'
MARKER = '[OFFICE:' + COMMAND + ']\n'


class Transport:
    def __init__(self):
        self.calls = []
        self.frames = []
        self.receipt = {'run': {'id': NATIVE, 'agentId': SID}}
        self.result = {'id': NATIVE, 'agentId': SID, 'status': 'FINISHED', 'result': 'Verified reply'}

    def json(self, method, url, headers, data=None):
        self.calls.append((method, url, data))
        return self.receipt if method == 'POST' else self.result

    def open(self, method, url, headers):
        self.calls.append((method, url, None))
        body = ''.join('event: ' + kind + '\ndata: ' + json.dumps(value) + '\n\n'
                       for kind, value in self.frames).encode()
        return io.BytesIO(), io.BytesIO(body)


class CursorCloudTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        credential = Path(self.temp.name) / 'key.json'
        credential.write_text(json.dumps({'api_key': 'fixture-private-key'}))
        credential.chmod(0o600)
        self.events = []
        self.transport = Transport()
        config = {'command_adapters': {'cursor_cloud': {'sessions': {
            SID: {'credential_file': str(credential)}}}}}
        self.adapter = CursorCloudAdapter(config, lambda agent, event: self.events.append(event), self.transport)

    def tearDown(self):
        self.adapter.close()
        self.temp.cleanup()

    def test_only_transport_prefix_is_removed_preserving_user_text_and_receipt(self):
        prompt = 'Keep\n  indentation and a quoted marker:\n' + MARKER + 'literal text'
        with patch.object(self.adapter, 'resume') as resume:
            receipt = self.adapter.submit(SID, MARKER + prompt)
        self.assertEqual(self.transport.calls, [('POST', self.adapter.base + SID + '/runs',
                                               {'prompt': {'text': prompt}})])
        self.assertEqual(receipt['native_message_id'], NATIVE)
        self.assertEqual(self.events[0]['run_id'], COMMAND)
        self.assertEqual(self.events[0]['type'], 'accepted')
        resume.assert_called_once_with(SID, COMMAND, NATIVE, MARKER + prompt)

    def test_malformed_and_wrong_target_receipts_stay_uncertain(self):
        for receipt in ({'run': None}, {'run': []}, {'run': {'id': NATIVE, 'agentId': SID + '-other'}}):
            with self.subTest(receipt=receipt):
                self.transport.receipt = receipt
                with self.assertRaises(DeliveryError) as caught:
                    self.adapter.submit(SID, MARKER + 'Task')
                self.assertTrue(caught.exception.uncertain)
                self.assertEqual(caught.exception.code, 'cursor_invalid_run_receipt')
        self.assertEqual(self.events, [])

    def test_foreign_stream_identity_never_publishes_its_text(self):
        for field, value in (('agentId', SID + '-other'), ('runId', NATIVE + '-other')):
            with self.subTest(field=field):
                self.events.clear()
                self.transport.frames = [('assistant', {'text': 'FOREIGN PRIVATE TEXT', field: value})]
                self.adapter.observe(SID, COMMAND, NATIVE, MARKER + 'Task')
                self.assertNotIn('FOREIGN PRIVATE TEXT', json.dumps(self.events))
                self.assertEqual(self.events[-1]['state'], 'completed')
                self.assertEqual(self.events[-1]['text'], 'Verified reply')

    def test_scoped_public_deltas_allow_repeated_text_and_skip_private_frames(self):
        self.transport.frames = [
            ('status', {'runId': NATIVE, 'status': 'RUNNING'}),
            ('thinking', {'text': 'PRIVATE REASONING'}),
            ('interaction_update', {'text': 'PRIVATE RAW PAYLOAD'}),
            ('assistant', {'text': 'ha', 'agentId': SID, 'runId': NATIVE}),
            ('assistant', {'text': 'ha'}),
            ('result', {'runId': NATIVE, 'agentId': SID, 'status': 'FINISHED', 'text': 'haha'})]
        self.adapter.observe(SID, COMMAND, NATIVE, MARKER + 'Task')
        public = [event['text'] for event in self.events if event['type'] == 'text']
        self.assertEqual(public, ['ha', 'haha', 'haha'])
        self.assertNotIn('PRIVATE', json.dumps(self.events))
        self.assertEqual(len([event for event in self.events if event['type'] == 'finished']), 1)

    def test_exact_terminal_get_recovers_without_post_or_stream(self):
        self.adapter.observe(SID, COMMAND, NATIVE, 'Saved preview', recovery=True, last_segment=4)
        self.assertEqual(self.transport.calls, [('GET', self.adapter.base + SID + '/runs/' + NATIVE, None)])
        final = next(event for event in self.events if event['type'] == 'text')
        self.assertEqual(final['segment'], 4)
        self.assertTrue(final['final'])

    def test_wrong_get_target_does_not_claim_completion(self):
        self.transport.result['agentId'] = SID + '-other'
        self.adapter.observe(SID, COMMAND, NATIVE, 'Saved preview', recovery=True)
        self.assertEqual([event['type'] for event in self.events], ['interrupted'])
        self.assertEqual(self.events[0]['state'], 'delivery_unknown')

    def test_uncertain_post_is_not_repeated_and_cannot_emit_acceptance(self):
        with patch.object(self.transport, 'json', side_effect=RemoteError('platform_delivery_unknown', True)) as call:
            with self.assertRaises(DeliveryError) as caught:
                self.adapter.submit(SID, MARKER + 'Task')
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(self.events, [])


if __name__ == '__main__':
    unittest.main()
