import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('child_client', Path(__file__).parent / 'adapters/children.py')
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


class ChildClientTests(unittest.TestCase):
    def setUp(self):
        self.credential = {'endpoint': 'http://127.0.0.1:4318/api/agents/events',
                           'agent_id': 'grok_bot:fixture', 'source_id': 'fixture-parent', 'token': 'fixture-only'}

    def arguments(self, *extra):
        return client.parser().parse_args(['--credential', 'fixture.json', 'message',
            '--child-id', 'native-child', '--parent-run', 'parent-turn', '--run', 'child-turn',
            '--event-id', 'fixed-message', *extra])

    def test_repeated_callback_has_identical_payload_without_new_time_or_native_claim(self):
        args = self.arguments()
        first = client.payload(args, self.credential, '公開進度')
        self.assertEqual(first, client.payload(args, self.credential, '公開進度'))
        self.assertNotIn('at', first)
        self.assertNotIn('evidence', first)
        self.assertNotIn('provider', first)
        self.assertEqual(first['native_parent_id'], 'fixture-parent')

    def test_public_user_message_is_prompt_and_public_final_is_preserved(self):
        task = client.payload(self.arguments('--role', 'user'), self.credential, '委派任務')
        self.assertEqual((task['role'], task['phase']), ('user', 'prompt'))
        final = client.payload(self.arguments('--phase', 'final'), self.credential, '交付')
        self.assertEqual((final['role'], final['phase']), ('assistant', 'final'))

    def test_credential_cannot_redirect_to_remote_service_or_embed_secret_in_url(self):
        for bad in ('https://example.com/api/agents/events', 'http://127.0.0.1:4318/api/agents/events?token=x',
                    'http://user:password@localhost:4318/api/agents/events', 'http://localhost:4318/other'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                client.endpoint(dict(self.credential, endpoint=bad))
        self.assertEqual(client.endpoint(self.credential), 'http://127.0.0.1:4318/api/agents/children/reports')

    def test_oversized_text_rejected_before_sending(self):
        with self.assertRaises(ValueError):
            client.payload(self.arguments(), self.credential, 'x' * 24001)


if __name__ == '__main__':
    unittest.main()
