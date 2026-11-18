"""Credential migration and private persistence; only temporary fake data."""
import copy
import json
import os
from pathlib import Path
import stat
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

from configuration import initialize, load_config, save_config
from security_keys import (owner_key, matches_owner_key, integrity_key, integrity_fingerprint,
    integrity_candidates, matches_integrity, security_secrets, upgrade_security,
    upgraded_security, validate_security_keys)


class SecurityKeyTests(unittest.TestCase):
    def legacy(self):
        return {'topic': 'fixture-legacy-topic-1234567890',
                'feed': 'https://ntfy.sh/fixture-legacy-topic-1234567890',
                'agents': [], 'host': '127.0.0.1', 'port': 4318}

    def test_explicit_upgrade_separates_keys_and_preserves_original(self):
        before = self.legacy(); original = copy.deepcopy(before)
        migrated = upgrade_security(before)
        self.assertEqual(before, original)
        self.assertEqual(migrated['topic'], before['topic'])
        self.assertIsNone(migrated['feed'])
        self.assertEqual(migrated['legacy_integrity_keys'], [before['topic']])
        self.assertEqual(len({owner_key(migrated), integrity_key(migrated), before['topic']}), 3)
        self.assertTrue(upgraded_security(migrated))
        self.assertFalse(upgraded_security(before))
        self.assertEqual(upgrade_security(migrated), migrated)

    def test_old_receipt_matches_after_migration_without_reusing_old_owner_key(self):
        before = self.legacy(); payload = b'generic:fixture\0one bounded task'
        receipt = integrity_fingerprint(before, payload)
        migrated = upgrade_security(before)
        self.assertTrue(matches_integrity(migrated, payload, receipt))
        self.assertFalse(matches_integrity(migrated, payload+b' changed', receipt))
        self.assertNotEqual(owner_key(migrated), before['topic'])
        self.assertNotEqual(integrity_fingerprint(migrated, payload), receipt)
        self.assertEqual(integrity_candidates(migrated, payload)[1], receipt)
        current = integrity_fingerprint(migrated, payload)
        self.assertFalse(matches_integrity(before, payload, current))
        for invalid in (None, '', 'x'*64, '中'*64):
            self.assertFalse(matches_integrity(migrated, payload, invalid))

    def test_all_private_keys_are_available_to_redaction(self):
        migrated = upgrade_security(self.legacy())
        self.assertEqual(set(security_secrets(migrated)),
                         {migrated['topic'], migrated['owner_key'], migrated['internal_hmac_key']})

    def test_owner_comparison_accepts_only_exact_ascii_owner_value(self):
        config = upgrade_security(self.legacy())
        self.assertTrue(matches_owner_key(config, owner_key(config)))
        for invalid in ('\u00e9', '\u4e2d', 'x'*1000, None, b'bytes', config['topic'], integrity_key(config)):
            self.assertFalse(matches_owner_key(config, invalid))

    def test_invalid_or_overlapping_security_keys_fail_closed(self):
        valid = upgrade_security(self.legacy())
        changes = [{'owner_key': ''}, {'internal_hmac_key': valid['topic']},
                   {'owner_key': valid['internal_hmac_key']}, {'legacy_integrity_keys': [{}]},
                   {'legacy_integrity_keys': [valid['owner_key']]},
                   {'legacy_integrity_keys': [valid['topic'], valid['topic']]},
                   {'legacy_integrity_keys': ['short']}]
        for change in changes:
            with self.subTest(change=list(change)):
                candidate = dict(valid, **change)
                self.assertFalse(upgraded_security(candidate))
                with self.assertRaises(ValueError): validate_security_keys(candidate)
        partial = dict(self.legacy(), owner_key=valid['owner_key'])
        with self.assertRaises(ValueError): upgrade_security(partial)

    def test_new_install_has_three_independent_keys_and_no_legacy_authority(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'private'/'config.json'
            config = initialize(path)
            self.assertTrue(upgraded_security(config))
            self.assertIsNone(config['feed'])
            self.assertEqual(len({config['topic'], config['owner_key'], config['internal_hmac_key']}), 3)
            self.assertNotIn('legacy_integrity_keys', config)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_atomic_write_ignores_predictable_legacy_temp_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'config.json'; outside = Path(folder)/'unrelated.txt'
            outside.write_text('preserve me')
            path.with_suffix('.tmp').symlink_to(outside)
            save_config(path, self.legacy())
            self.assertEqual(outside.read_text(), 'preserve me')
            self.assertEqual(load_config(path)['topic'], self.legacy()['topic'])
            self.assertEqual(list(path.parent.glob('.config.json-*.tmp')), [])

    def test_failed_replace_preserves_config_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'config.json'; save_config(path, self.legacy())
            original = path.read_bytes()
            with patch('configuration.os.replace', side_effect=OSError('fixture failure')):
                with self.assertRaises(OSError): save_config(path, upgrade_security(self.legacy()))
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.glob('.config.json-*.tmp')), [])

    def test_config_file_and_immediate_parent_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); path = root/'config.json'; save_config(path, self.legacy())
            link = root/'link.json'; link.symlink_to(path)
            for action in (lambda: load_config(link), lambda: save_config(link, self.legacy())):
                with self.assertRaises(ValueError): action()
            alias = root/'alias'; target = root/'private'; target.mkdir(mode=0o700)
            alias.symlink_to(target, target_is_directory=True)
            with self.assertRaises(ValueError): save_config(alias/'new.json', self.legacy())
            self.assertFalse((target/'new.json').exists())

    def test_world_readable_config_or_directory_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); path = root/'config.json'; save_config(path, self.legacy())
            path.chmod(0o644)
            with self.assertRaises(ValueError): load_config(path)
            path.chmod(0o600); root.chmod(0o755)
            for action in (lambda: load_config(path), lambda: save_config(path, self.legacy())):
                with self.assertRaises(ValueError): action()
            root.chmod(0o700)

    def test_existing_connection_check_survives_integrity_migration(self):
        from test_connection_checks import ConnectionCheckTests
        fixture = ConnectionCheckTests(); fixture.setUp()
        try:
            result = fixture.start()
            fixture.commands.config = upgrade_security(fixture.commands.config)
            replay = fixture.start(result['test_id'])
            self.assertTrue(replay['duplicate'])
            self.assertEqual(replay['test_id'], result['test_id'])
            self.assertEqual(replay['state'], 'queued')
            fixture.checks.expire_pending()
            row = fixture.commands.db.execute('SELECT state FROM commands WHERE id=?', (result['test_id'],)).fetchone()
            self.assertEqual(row['state'], 'pending')
            self.assertEqual(fixture.adapter.calls, [])
        finally:
            fixture.tearDown()

    def test_sensitive_cli_changes_refuse_a_running_listener_without_writing(self):
        import office
        with tempfile.TemporaryDirectory() as folder, socket.socket() as listener:
            listener.bind(('127.0.0.1', 0)); listener.listen(4)
            path = Path(folder)/'config.json'
            config = self.legacy(); config['port'] = listener.getsockname()[1]
            save_config(path, config); before = path.read_bytes()
            for action in (['security-upgrade'], ['mobile-configure', '--origin', 'https://office.fixture.ts.net', '--identity', 'fixture@example.test']):
                with patch.object(sys, 'argv', ['office.py', '--config', str(path), *action]):
                    with self.assertRaisesRegex(ValueError, 'Stop Office'):
                        office.main()
                self.assertEqual(path.read_bytes(), before)

    def test_offline_check_includes_configured_mobile_listener(self):
        from office import require_offline
        with socket.socket() as listener, socket.socket() as unused:
            listener.bind(('127.0.0.1', 0)); listener.listen(1)
            unused.bind(('127.0.0.1', 0)); port = unused.getsockname()[1]; unused.close()
            config = {'port': port, 'mobile': {'port': listener.getsockname()[1], 'enabled': False}}
            with self.assertRaisesRegex(ValueError, 'Stop Office'):
                require_offline(config)

    def test_offline_check_fails_closed_when_socket_state_is_uncertain(self):
        import errno
        from office import require_offline
        with patch('office.socket.socket') as factory:
            connection = factory.return_value.__enter__.return_value
            connection.connect_ex.return_value = errno.EACCES
            with self.assertRaisesRegex(ValueError, 'Stop Office'):
                require_offline({'port': 4318})
            connection.connect_ex.return_value = errno.ECONNREFUSED
            require_offline({'port': 4318})


if __name__ == '__main__':
    unittest.main()
