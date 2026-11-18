"""Credential migration keeps durable SDK receipts idempotent; all clients are fake."""
from contextlib import contextmanager
import hashlib
import hmac
import json
from pathlib import Path
import unittest

from configuration import load_config, save_config
from connections import ConnectionError
from security_keys import integrity_fingerprint, owner_key, upgrade_security
import test_codex_background_connections as background
import test_codex_background_model as model
import test_cursor_local as cursor
import test_grok_sdk_connections as grok


@contextmanager
def fixture(kind):
    case = kind()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def legacy_configuration(case):
    config = load_config(case.path)
    for key in ('owner_key', 'internal_hmac_key', 'legacy_integrity_keys'):
        config.pop(key, None)
    save_config(case.path, config)
    case.bridge.refresh_connections(config)
    return config


def migrate(case):
    config = upgrade_security(load_config(case.path))
    save_config(case.path, config)
    case.bridge.refresh_connections(config)
    return config


class SdkIntegrityMigrationTests(unittest.TestCase):
    def assert_conflict(self, callback):
        with self.assertRaises(ConnectionError) as error:
            callback()
        self.assertEqual(error.exception.code, 'request_id_conflict')

    def test_grok_existing_receipt_survives_migration_without_native_reads(self):
        with fixture(grok.GrokSdkConnectionTests) as case:
            old = legacy_configuration(case)
            request = case.request()
            result = case.connections.connect(request)
            payload = json.dumps({key: request[key] for key in ('bot_id', 'session_id', 'name')}, sort_keys=True).encode()
            stored = case.bridge.history.db.execute('SELECT fingerprint FROM grok_sdk_connections').fetchone()[0]
            self.assertEqual(stored, hmac.new(old['topic'].encode(), payload, hashlib.sha256).hexdigest())
            config = migrate(case)
            self.assertNotEqual(stored, integrity_fingerprint(config, payload))
            self.assertNotEqual(owner_key(config), old['topic'])
            case.client.calls.clear()
            case.client.auth['state'] = 'expired'
            self.assertEqual(case.connections.connect(request), result)
            self.assertEqual(case.client.calls, [])
            self.assert_conflict(lambda: case.connections.connect({**request, 'name': 'Changed request'}))
            self.assertEqual(len(load_config(case.path)['agents']), 1)

    def test_codex_registration_survives_migration_without_recreating_lifecycle(self):
        with fixture(background.BackgroundRegistrationTests) as case:
            legacy_configuration(case)
            request, result = case.create()
            lifecycle = Path(case.config_session(result)['lifecycle_file'])
            before = lifecycle.read_bytes()
            old = case.bridge.history.db.execute('SELECT fingerprint FROM codex_background_creations').fetchone()[0]
            config = migrate(case)
            payload = json.dumps({'name': request['name'], 'cwd': str(case.root), 'model': None, 'create_folder': False}, sort_keys=True).encode()
            self.assertNotEqual(old, integrity_fingerprint(config, payload))
            probes = len(case.probes)
            self.assertEqual(case.connections.create(request), result)
            self.assertEqual(lifecycle.read_bytes(), before)
            self.assertEqual(len(case.probes), probes)
            self.assert_conflict(lambda: case.connections.create({**request, 'name': 'Changed request'}))
            self.assertEqual(len(load_config(case.path)['agents']), 1)

    def test_codex_model_receipt_survives_migration_without_reapplying(self):
        with fixture(model.BackgroundModelRepairTests) as case:
            legacy_configuration(case)
            case.ready()
            request = case.request()
            result = case.connections.update_model(request)
            before = case.lifecycle.read_bytes()
            probes = case.probe_calls
            old = case.bridge.history.db.execute('SELECT fingerprint FROM codex_background_model_changes').fetchone()[0]
            config = migrate(case)
            payload = json.dumps({'agent_id': request['agent_id'], 'model': request['model']}, sort_keys=True).encode()
            self.assertNotEqual(old, integrity_fingerprint(config, payload))
            self.assertEqual(case.connections.update_model(request), result)
            self.assertEqual(case.lifecycle.read_bytes(), before)
            self.assertEqual(case.probe_calls, probes)
            self.assert_conflict(lambda: case.connections.update_model({**request, 'model': 'different-model'}))
            self.assertEqual(case.bridge.history.db.execute('SELECT COUNT(*) FROM codex_background_model_changes').fetchone()[0], 1)

    def test_cursor_reservation_survives_migration_without_second_reserve(self):
        with fixture(cursor.CursorLocalTests) as case:
            legacy_configuration(case)
            request = case.creation()
            result = case.bridge.connections.create_cursor_local(request)
            self.assertEqual(len(case.fake.created), 1)
            stored = case.bridge.history.db.execute('SELECT fingerprint FROM cursor_sdk_creations').fetchone()[0]
            migrate(case)
            self.assertEqual(case.bridge.connections.create_cursor_local(request), result)
            self.assertEqual(len(case.fake.created), 1)
            self.assertEqual(case.fake.sent, [])
            self.assert_conflict(lambda: case.bridge.connections.create_cursor_local({**request, 'name': 'Changed request'}))
            self.assertEqual(case.bridge.history.db.execute('SELECT fingerprint FROM cursor_sdk_creations').fetchone()[0], stored)

    def test_new_receipts_use_internal_key_and_survive_owner_key_rotation(self):
        with fixture(background.BackgroundRegistrationTests) as case:
            old = legacy_configuration(case)
            config = migrate(case)
            request, result = case.create()
            payload = json.dumps({'name': request['name'], 'cwd': str(case.root), 'model': None, 'create_folder': False}, sort_keys=True).encode()
            stored = case.bridge.history.db.execute('SELECT fingerprint FROM codex_background_creations').fetchone()[0]
            self.assertEqual(stored, integrity_fingerprint(config, payload))
            self.assertNotEqual(stored, hmac.new(old['topic'].encode(), payload, hashlib.sha256).hexdigest())
            config = load_config(case.path)
            config['owner_key'] = 'rotated-fixture-owner-key-' + 'x' * 24
            save_config(case.path, config)
            case.bridge.refresh_connections(config)
            probes = len(case.probes)
            self.assertEqual(case.connections.create(request), result)
            self.assertEqual(len(case.probes), probes)


if __name__ == '__main__':
    unittest.main()
