"""Synthetic metadata-only Cursor setup; no vendor, model, or native App calls."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from configuration import load_config
from connections import ConnectionError
from hermes_acp import private_json
from platform_adapters import cursor_headers
import test_platforms as fixtures


A = 'bc-00000000-0000-4000-8000-000000000001'
B = 'bc-00000000-0000-4000-8000-000000000002'
C = 'bc-00000000-0000-4000-8000-000000000003'
KEY_A = 'fixture-account-a-private-key'
KEY_B = 'fixture-account-b-private-key'
KEY_NEW = 'fixture-account-a-replacement-key'


class MetadataTransport:
    def __init__(self):
        self.calls = []
        self.wrong_target = False

    def json(self, method, url, headers, data=None):
        assert method == 'GET' and data is None
        self.calls.append((url.rsplit('/', 1)[-1], dict(headers)))
        return {'id': B if self.wrong_target else url.rsplit('/', 1)[-1], 'status': 'IDLE'}


class CursorConnectionIsolationTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp
    tearDown = fixtures.PlatformTests.tearDown

    def setup_transport(self):
        transport = MetadataTransport()
        self.bridge.connections.transport = transport
        return transport

    def connect(self, sid=A, key=KEY_A):
        data = {'name': 'Fixture ' + sid[-1], 'session_id': sid}
        if key is not None:
            data['api_key'] = key
        return self.bridge.connections.connect_cursor(data)

    def credential(self, sid):
        config = load_config(self.path)
        return Path(config['command_adapters']['cursor_cloud']['sessions'][sid]['credential_file'])

    def active(self, sid=A, state='running'):
        ident = str(uuid.uuid4())
        self.bridge.commands.create({'request_id': ident, 'agent_id': 'cursor:' + sid,
                                     'text': 'Synthetic command; never dispatched.'})
        self.bridge.commands.db.execute('UPDATE commands SET state=?,native_message_id=?,run_id=? WHERE id=?',
                                       (state, 'fixture-receipt', ident, ident))
        self.bridge.commands.db.commit()
        return ident

    def install_legacy(self, *sessions):
        legacy = self.root / 'platform-secrets/cursor.json'
        private_json(legacy, {'api_key': KEY_A})
        config = load_config(self.path)
        for sid in sessions:
            config['command_adapters']['cursor_cloud']['sessions'][sid]['credential_file'] = str(legacy)
        self.bridge.connections.save(config)
        return legacy

    def test_two_agents_have_independent_credentials_and_saved_key_status(self):
        self.setup_transport()
        self.assertFalse(self.bridge.connections.status()['cursor_key_saved'])
        self.connect()
        before = self.credential(A).read_bytes()
        self.connect(B, KEY_B)
        self.assertNotEqual(self.credential(A), self.credential(B))
        self.assertEqual(self.credential(A).read_bytes(), before)
        self.assertEqual(json.loads(self.credential(B).read_text())['api_key'], KEY_B)
        for sid in (A, B):
            self.assertEqual(self.credential(sid).stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root / 'platform-secrets/cursor.json').exists())
        self.assertTrue(self.bridge.connections.status()['cursor_key_saved'])
        for key in (KEY_A, KEY_B):
            self.assertNotIn(key, self.path.read_text())

    def test_rotating_one_target_preserves_other_target_and_old_working_file(self):
        self.setup_transport()
        self.connect()
        self.connect(B, KEY_B)
        old_a, old_b = self.credential(A), self.credential(B)
        old_a_bytes, old_b_bytes = old_a.read_bytes(), old_b.read_bytes()
        self.connect(A, KEY_NEW)
        self.assertNotEqual(self.credential(A), old_a)
        self.assertEqual(old_a.read_bytes(), old_a_bytes)
        self.assertEqual(self.credential(B), old_b)
        self.assertEqual(old_b.read_bytes(), old_b_bytes)
        self.assertEqual(json.loads(self.credential(A).read_text())['api_key'], KEY_NEW)

    def test_legacy_rotation_changes_only_selected_binding(self):
        self.setup_transport()
        self.connect()
        self.connect(B, KEY_B)
        legacy = self.install_legacy(A, B)
        before = legacy.read_bytes()
        self.connect(B, KEY_B)
        self.assertEqual(self.credential(A), legacy)
        self.assertNotEqual(self.credential(B), legacy)
        self.assertEqual(legacy.read_bytes(), before)
        self.assertEqual(json.loads(self.credential(B).read_text())['api_key'], KEY_B)

    def test_new_target_can_read_legacy_key_without_overwriting_it(self):
        transport = self.setup_transport()
        legacy = self.root / 'platform-secrets/cursor.json'
        private_json(legacy, {'api_key': KEY_A})
        before = legacy.read_bytes()
        self.connect(C, None)
        self.assertNotEqual(self.credential(C), legacy)
        self.assertEqual(legacy.read_bytes(), before)
        self.assertEqual(transport.calls[-1][1], cursor_headers(KEY_A))

    def test_existing_target_prefers_its_scoped_key_over_legacy_default(self):
        transport = self.setup_transport()
        self.connect()
        private_json(self.root / 'platform-secrets/cursor.json', {'api_key': KEY_B})
        before = self.path.read_bytes()
        self.connect(A, None)
        self.assertEqual(transport.calls[-1][1], cursor_headers(KEY_A))
        self.assertEqual(self.path.read_bytes(), before)

    def test_changed_key_is_refused_while_target_has_any_exposed_work(self):
        transport = self.setup_transport()
        self.connect()
        command = self.active()
        config_before = self.path.read_bytes()
        credential_before = self.credential(A).read_bytes()
        calls_before = len(transport.calls)
        for state in ('sending', 'queued', 'running', 'waiting_user', 'delivery_unknown', 'pending'):
            with self.subTest(state=state):
                self.bridge.commands.db.execute('UPDATE commands SET state=? WHERE id=?', (state, command))
                self.bridge.commands.db.commit()
                with self.assertRaises(ConnectionError) as caught:
                    self.connect(A, KEY_NEW)
                self.assertEqual(caught.exception.code, 'connection_has_active_commands')
                self.assertEqual(caught.exception.status, 409)
                self.assertEqual(self.path.read_bytes(), config_before)
                self.assertEqual(self.credential(A).read_bytes(), credential_before)
        self.assertEqual(len(transport.calls), calls_before)

    def test_same_details_are_idempotent_during_work_and_do_not_rewrite_files(self):
        self.setup_transport()
        self.connect()
        command = self.active()
        config_before = self.path.read_bytes()
        credential = self.credential(A)
        credential_before = credential.read_bytes()
        self.connect()
        self.assertEqual(self.path.read_bytes(), config_before)
        self.assertEqual(credential.read_bytes(), credential_before)
        row = self.bridge.commands.db.execute('SELECT state FROM commands WHERE id=?', (command,)).fetchone()
        self.assertEqual(row['state'], 'running')
        self.assertEqual(self.bridge.commands.db.execute('SELECT count(*) FROM commands').fetchone()[0], 1)

    def test_disabled_target_cannot_reconnect_or_rotate_key(self):
        transport = self.setup_transport()
        self.connect()
        config = load_config(self.path)
        config['disabled_command_agents'] = ['cursor:' + A]
        self.bridge.connections.save(config)
        before = self.path.read_bytes()
        calls_before = len(transport.calls)
        for key in (KEY_A, KEY_NEW):
            with self.assertRaises(ConnectionError) as caught:
                self.connect(A, key)
            self.assertEqual(caught.exception.code, 'dispatch_disabled')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(transport.calls), calls_before)

    def test_failed_config_save_keeps_existing_target_credential_and_config(self):
        self.setup_transport()
        self.connect()
        before = self.path.read_bytes()
        credential = self.credential(A)
        old = credential.read_bytes()
        with patch.object(self.bridge.connections, 'save', side_effect=OSError('fixture save failure')):
            with self.assertRaises(OSError):
                self.connect(A, KEY_NEW)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.credential(A), credential)
        self.assertEqual(credential.read_bytes(), old)

    def test_mismatched_native_target_cannot_change_a_saved_route(self):
        transport = self.setup_transport()
        self.connect()
        before = self.path.read_bytes()
        credential_before = self.credential(A).read_bytes()
        transport.wrong_target = True
        with self.assertRaises(ConnectionError) as caught:
            self.connect(A, KEY_NEW)
        self.assertEqual(caught.exception.code, 'cursor_agent_not_available')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.credential(A).read_bytes(), credential_before)

    def test_metadata_reply_after_logout_cannot_reenroll_retired_target(self):
        transport = self.setup_transport()
        self.connect()
        after_logout = {}
        def delayed_metadata(method, url, headers, data=None):
            self.bridge.members.logout({'agent_id': 'cursor:' + A})
            after_logout['config'] = self.path.read_bytes()
            after_logout['files'] = sorted(self.bridge.connections.root.glob('cursor-*.json'))
            return transport.json(method, url, headers, data)
        self.bridge.connections.transport = type('Delayed', (), {'json': staticmethod(delayed_metadata)})()
        with self.assertRaises(ConnectionError) as caught:
            self.connect()
        self.assertEqual(caught.exception.code, 'agent_membership_changed')
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.path.read_bytes(), after_logout['config'])
        self.assertEqual(sorted(self.bridge.connections.root.glob('cursor-*.json')), after_logout['files'])
        self.assertNotIn('cursor:' + A, self.bridge.definitions)
        self.assertEqual(load_config(self.path)['agents'], [])

    def test_old_metadata_cannot_overwrite_same_identity_after_logout_and_fresh_rejoin(self):
        transport = self.setup_transport()
        self.connect()
        old_agent = dict(self.bridge.definitions['cursor:' + A])
        after_rejoin = {}
        def delayed_metadata(method, url, headers, data=None):
            self.bridge.members.logout({'agent_id': 'cursor:' + A})
            self.bridge.connections.transport = transport
            self.assertTrue(self.connect()['connected'])
            self.assertEqual(self.bridge.definitions['cursor:' + A], old_agent)
            after_rejoin['config'] = self.path.read_bytes()
            after_rejoin['files'] = sorted(self.bridge.connections.root.glob('cursor-*.json'))
            return transport.json(method, url, headers, data)
        self.bridge.connections.transport = type('Delayed', (), {'json': staticmethod(delayed_metadata)})()
        with self.assertRaises(ConnectionError) as caught:
            self.connect(A, KEY_NEW)
        self.assertEqual(caught.exception.code, 'agent_membership_changed')
        self.assertEqual(self.path.read_bytes(), after_rejoin['config'])
        self.assertEqual(sorted(self.bridge.connections.root.glob('cursor-*.json')), after_rejoin['files'])
        self.assertEqual(json.loads(self.credential(A).read_text())['api_key'], KEY_A)

    def test_explicit_connection_started_after_logout_is_allowed(self):
        self.setup_transport()
        self.connect()
        self.bridge.members.logout({'agent_id': 'cursor:' + A})
        self.assertTrue(self.connect(A, KEY_NEW)['connected'])
        self.assertIn('cursor:' + A, self.bridge.definitions)
        self.assertEqual(json.loads(self.credential(A).read_text())['api_key'], KEY_NEW)

    def test_other_target_logout_during_metadata_lookup_does_not_block_this_target(self):
        transport = self.setup_transport()
        self.connect()
        self.connect(B, KEY_B)
        target_before = self.credential(A).read_bytes()
        def delayed_metadata(method, url, headers, data=None):
            self.bridge.members.logout({'agent_id': 'cursor:' + B})
            return transport.json(method, url, headers, data)
        self.bridge.connections.transport = type('Delayed', (), {'json': staticmethod(delayed_metadata)})()
        self.assertTrue(self.connect()['connected'])
        self.assertIn('cursor:' + A, self.bridge.definitions)
        self.assertNotIn('cursor:' + B, self.bridge.definitions)
        self.assertEqual(self.credential(A).read_bytes(), target_before)


if __name__ == '__main__':
    unittest.main()
