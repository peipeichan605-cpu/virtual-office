"""Synthetic lost-receipt recovery proof; no live vendor requests."""
import datetime as dt
import io
import json
import unittest
from unittest.mock import patch

from configuration import load_config
from cursor_cloud_recovery import RecoveryError, recover_legacy_run
import test_platforms as fixtures


NATIVE = 'run-lost-receipt-fixture'


class RecoveryTransport:
    def __init__(self, command, prompt, at):
        self.calls = []
        self.native = {'id': NATIVE, 'agentId': fixtures.SID, 'status': 'FINISHED', 'createdAt': at}
        self.messages = [{'type': 'user_message', 'session_id': NATIVE,
                          'text': '[OFFICE:' + command + ']\n' + prompt}]
        self.before_open = None

    def json(self, method, url, headers, data=None):
        self.calls.append((method, url))
        if method != 'GET':
            raise AssertionError('Recovery must never POST')
        return self.native

    def open(self, method, url, headers):
        self.calls.append((method, url))
        if self.before_open:
            self.before_open()
        frames = [('thinking', {'text': 'MUST_NOT_READ_PRIVATE_REASONING'})]
        frames += [('interaction_update', {'type': 'user-message-appended', 'userMessage': message}) for message in self.messages]
        body = ''.join('event: ' + kind + '\ndata: ' + json.dumps(value) + '\n\n' for kind, value in frames).encode()
        return io.BytesIO(), io.BytesIO(body)


class CursorCloudRecoveryTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp
    tearDown = fixtures.PlatformTests.tearDown
    connect = fixtures.PlatformTests.connect
    submit = fixtures.PlatformTests.submit

    def attempt(self):
        self.connect()
        request = self.submit(text='Keep the exact\n  user prompt')
        command = request['request_id']
        self.bridge.commands.db.execute("UPDATE commands SET state='delivery_unknown',payload=NULL WHERE id=?", (command,))
        self.bridge.commands.db.commit()
        row = self.row(command)
        remote = RecoveryTransport(command, request['text'], row['updated_at'])
        return command, remote

    def row(self, command):
        return dict(self.bridge.commands.db.execute('SELECT * FROM commands WHERE id=?', (command,)).fetchone())

    def recover(self, remote, apply=False):
        return recover_legacy_run(self.bridge.commands, fixtures.AGENT, NATIVE, apply=apply, transport=remote)

    def test_dry_run_reads_exact_target_only_and_does_not_attach(self):
        command, remote = self.attempt()
        before = self.row(command)
        result = self.recover(remote)
        self.assertTrue(result['verified'])
        self.assertFalse(result['applied'])
        self.assertEqual(result['command_id'], command)
        self.assertEqual(before, self.row(command))
        base = 'https://api.cursor.com/v1/agents/' + fixtures.SID + '/runs/' + NATIVE
        self.assertEqual(remote.calls, [('GET', base), ('GET', base + '/stream')])
        self.assertNotIn('user prompt', json.dumps(result))

    def test_apply_attaches_receipt_only_then_normal_observer_can_resume(self):
        command, remote = self.attempt()
        result = self.recover(remote, apply=True)
        self.assertTrue(result['applied'])
        row = self.row(command)
        self.assertEqual(row['native_message_id'], NATIVE)
        self.assertEqual(row['state'], 'delivery_unknown')
        self.assertIsNone(row['run_id'])
        self.assertEqual(self.bridge.history.messages({fixtures.AGENT}, fixtures.AGENT), [])
        with patch.object(self.bridge.commands.adapters['cursor_cloud'], 'resume') as resume:
            self.bridge.commands.restore_observations()
        self.assertEqual(resume.call_args.args[:3], (fixtures.SID, command, NATIVE))
        self.assertTrue(resume.call_args.kwargs['recovery'])
        count = len(remote.calls)
        self.assertTrue(self.recover(remote, apply=True)['duplicate'])
        self.assertEqual(len(remote.calls), count)

    def test_matching_words_or_marker_without_exact_fingerprint_are_rejected(self):
        command, remote = self.attempt()
        original = remote.messages[0]['text']
        for text in ('Keep the exact\n  user prompt', original + '\n', original.replace('  user', ' user'),
                     original.replace(command, '00000000-0000-4000-8000-000000000001')):
            with self.subTest(text=text):
                remote.messages[0]['text'] = text
                with self.assertRaises(RecoveryError):
                    self.recover(remote, apply=True)
        self.assertIsNone(self.row(command)['native_message_id'])

    def test_later_matching_quote_cannot_claim_an_unrelated_native_run(self):
        command, remote = self.attempt()
        remote.messages.insert(0, {'type': 'user_message', 'session_id': NATIVE, 'text': 'Unrelated initial prompt'})
        with self.assertRaises(RecoveryError):
            self.recover(remote, apply=True)
        self.assertIsNone(self.row(command)['native_message_id'])

    def test_wrong_native_agent_run_or_prompt_session_is_rejected(self):
        command, remote = self.attempt()
        for field in ('id', 'agentId'):
            original = remote.native[field]
            remote.native[field] = original + '-wrong'
            with self.assertRaises(RecoveryError):
                self.recover(remote, apply=True)
            remote.native[field] = original
        remote.messages[0]['session_id'] = NATIVE + '-wrong'
        with self.assertRaises(RecoveryError):
            self.recover(remote, apply=True)
        self.assertIsNone(self.row(command)['native_message_id'])

    def test_matching_prompt_outside_dispatch_window_is_rejected(self):
        command, remote = self.attempt()
        at = dt.datetime.fromisoformat(remote.native['createdAt'].replace('Z', '+00:00'))
        remote.native['createdAt'] = (at + dt.timedelta(hours=1)).isoformat()
        with self.assertRaises(RecoveryError) as caught:
            self.recover(remote, apply=True)
        self.assertEqual(caught.exception.code, 'cursor_recovery_outside_dispatch_window')
        self.assertIsNone(self.row(command)['native_message_id'])

    def test_command_change_during_read_prevents_attachment(self):
        command, remote = self.attempt()
        def change():
            self.bridge.commands.db.execute("UPDATE commands SET detail_code='changed-during-proof' WHERE id=?", (command,))
            self.bridge.commands.db.commit()
        remote.before_open = change
        with self.assertRaises(RecoveryError) as caught:
            self.recover(remote, apply=True)
        self.assertEqual(caught.exception.code, 'cursor_recovery_command_changed')
        self.assertIsNone(self.row(command)['native_message_id'])

    def test_detached_command_cannot_be_recovered_or_claim_saved_receipt(self):
        command, remote = self.attempt()
        self.recover(remote, apply=True)
        self.bridge.commands.detach_unconfirmed(fixtures.AGENT)
        before = self.row(command); calls = len(remote.calls)
        with self.assertRaises(RecoveryError) as caught:
            self.recover(remote, apply=True)
        self.assertEqual(caught.exception.code, 'cursor_recovery_no_uncertain_command')
        self.assertEqual(before, self.row(command)); self.assertEqual(len(remote.calls), calls)

    def test_detached_lost_receipt_is_excluded_before_remote_read(self):
        command, remote = self.attempt()
        self.bridge.commands.detach_unconfirmed(fixtures.AGENT)
        before = self.row(command)
        with self.assertRaises(RecoveryError) as caught:
            self.recover(remote, apply=True)
        self.assertEqual(caught.exception.code, 'cursor_recovery_no_uncertain_command')
        self.assertEqual(before, self.row(command)); self.assertEqual(remote.calls, [])

    def test_detachment_during_read_prevents_receipt_attachment(self):
        command, remote = self.attempt()
        before = self.row(command)
        remote.before_open = lambda: self.bridge.commands.detach_unconfirmed(fixtures.AGENT)
        with self.assertRaises(RecoveryError) as caught:
            self.recover(remote, apply=True)
        self.assertEqual(caught.exception.code, 'cursor_recovery_command_changed')
        self.assertEqual(before, self.row(command))

    def test_membership_change_during_read_prevents_attachment(self):
        command, remote = self.attempt()
        def change():
            self.bridge.commands.config['agents'][0]['name'] = 'Changed membership'
        remote.before_open = change
        with self.assertRaises(RecoveryError) as caught:
            self.recover(remote, apply=True)
        self.assertEqual(caught.exception.code, 'cursor_recovery_membership_changed')
        self.assertIsNone(self.row(command)['native_message_id'])

    def test_other_command_owned_native_receipt_cannot_be_reassigned(self):
        command, remote = self.attempt()
        other = self.submit(text='Different command')['request_id']
        self.bridge.commands.db.execute("UPDATE commands SET state='completed',native_message_id=? WHERE id=?", (NATIVE, other))
        self.bridge.commands.db.commit()
        with self.assertRaises(RecoveryError) as caught:
            self.recover(remote, apply=True)
        self.assertEqual(caught.exception.code, 'cursor_recovery_receipt_already_owned')
        self.assertIsNone(self.row(command)['native_message_id'])

    def test_already_projected_native_run_requires_explicit_history_reconciliation(self):
        command, remote = self.attempt()
        def project_during_read():
            self.bridge.history.record(fixtures.AGENT, 'cursor', 'Fixture', 'cloud-already-projected',
                                       'done', delivery='An existing delivery', source_ref=NATIVE,
                                       source_kind='cursor_cloud_native')
        remote.before_open = project_during_read
        with self.assertRaises(RecoveryError) as caught:
            self.recover(remote, apply=True)
        self.assertEqual(caught.exception.code, 'cursor_recovery_requires_history_reconciliation')
        self.assertIsNone(self.row(command)['native_message_id'])
        self.assertEqual(self.bridge.history.db.execute('SELECT count(*) FROM runs').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
