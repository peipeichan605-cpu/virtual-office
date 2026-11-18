"""Setup projections use private temporary fixtures; no real VPN or account."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import mobile_setup
import phone_tsnet as frontend


class MobileSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.base.chmod(0o700)
        self.config = {'data_dir': str(self.base), 'port': 4318,
                       'config_path': str(self.base / 'config.json'),
                       'mobile': {'port': 4319, 'public_origin': 'https://office.fixture.ts.net',
                                  'identity': 'owner@example.invalid', 'enabled': True}}

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, *, mode='serve', phase='ready'):
        root = self.base / 'phone-tsnet'
        root.mkdir(mode=0o700)
        run = root / 'runs' / 'phone-tsnet-20260101T000000Z-123456789abc'
        run.mkdir(mode=0o700, parents=True)
        (root / 'runs').chmod(0o700)
        (root / 'state').mkdir(mode=0o700)
        (root / 'bin').mkdir(mode=0o700)
        binary = root / 'bin/office-phone-123456789abcdef0'
        binary.write_text('fixture only')
        binary.chmod(0o700)
        self.identity = {'uid': 500, 'started': 'fixture-start', 'command_sha256': 'fixture'}
        self.record = {'run_id': run.name, 'pid': 12345, 'mode': mode,
                       'config_path': str(run / 'config.json'),
                       'status_path': str(run / 'worker-status.json'),
                       'binary': str(binary), 'process_identity': self.identity}
        self.settings = frontend.validate_settings(self.config, mode=mode, hostname='office',
                        **({'origin': self.config['mobile']['public_origin'],
                            'identity': self.config['mobile']['identity'], 'node_ids': ['nSecretPhone']}
                           if mode == 'serve' else {}))
        self.settings.update(state_dir=str(root / 'state'), status_path=self.record['status_path'])
        self.worker = {'version': 1, 'pid': 12345, 'phase': phase,
                       'updated_at': datetime.now(timezone.utc).isoformat(),
                       'auth_url': 'https://login.tailscale.com/a/secret',
                       'own': {'identity': self.config['mobile']['identity'],
                               'dns_name': 'office.fixture.ts.net', 'node_id': 'nSecretOffice'},
                       'peers': [{'node_id': 'nSecretPhone', 'hostname': 'private-phone'}]}
        self.root = root
        self.save()
        self.patch = mock.patch.object(frontend, '_process_identity', return_value=self.identity)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def save(self):
        frontend._write_json(self.root / 'active.json', self.record)
        frontend._write_json(Path(self.record['config_path']), self.settings)
        frontend._write_json(Path(self.record['status_path']), self.worker)

    def test_absent_setup_does_not_create_files_or_claim_connection(self):
        self.assertEqual(list(self.base.iterdir()), [])
        with mock.patch.object(frontend, '_lock', side_effect=AssertionError('no lock')):
            result = mobile_setup.connection_setup(self.config)
        self.assertEqual(list(self.base.iterdir()), [])
        self.assertFalse(result['pairing_available'])
        self.assertFalse(result['local_listener_ready'])
        self.assertFalse(result['phone_verified'])
        self.assertFalse(result['end_to_end_verified'])

    def test_external_configured_transport_is_unverified_not_blocked(self):
        result = mobile_setup.connection_setup(self.config, configured=True)
        self.assertEqual(result['transport']['state'], 'unverified')
        self.assertTrue(result['pairing_available'])
        self.assertFalse(result['local_listener_ready'])

    def test_managed_ready_is_local_only_and_contains_no_private_values(self):
        self.fixture()
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with mock.patch.object(frontend, '_lock', side_effect=AssertionError('no lock')), \
             mock.patch.object(frontend, '_is_running', side_effect=AssertionError('no reap')):
            result = mobile_setup.connection_setup(self.config, configured=True)
        self.assertEqual(result['transport']['state'], 'ready')
        self.assertTrue(result['local_listener_ready'])
        self.assertTrue(result['pairing_available'])
        self.assertFalse(result['end_to_end_verified'])
        for secret in ('example.invalid', 'Secret', 'private-phone', 'login.tailscale',
                       str(self.base), '12345', 'fixture-start'):
            self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.assertFalse((self.root / 'lifecycle.lock').exists())

    def test_disabled_configuration_cannot_be_ready(self):
        self.fixture()
        result = mobile_setup.connection_setup(self.config, configured=False)
        self.assertFalse(result['configuration_ready'])
        self.assertFalse(result['local_listener_ready'])
        self.assertFalse(result['pairing_available'])

    def test_managed_mismatch_never_reports_ready(self):
        self.fixture()
        changes = ({'backend_port': 4999}, {'core_port': 4998},
                   {'expected_origin': 'https://other.fixture.ts.net'},
                   {'expected_identity': 'other@example.invalid'}, {'allowed_node_ids': []},
                   {'state_dir': str(self.base)}, {'mode': 'login'})
        original = dict(self.settings)
        for change in changes:
            with self.subTest(change=change):
                self.settings = {**original, **change}; self.save()
                result = mobile_setup.connection_setup(self.config, configured=True)
                self.assertFalse(result['local_listener_ready'])
                self.assertFalse(result['pairing_available'])

    def test_worker_authenticated_identity_must_match(self):
        self.fixture()
        for own in (None, {}, {'identity': 'other', 'dns_name': 'office.fixture.ts.net'},
                    {'identity': 'owner@example.invalid', 'dns_name': 'wrong.fixture.ts.net'}):
            self.worker['own'] = own; self.save()
            result = mobile_setup.connection_setup(self.config, configured=True)
            self.assertEqual(result['transport']['state'], 'configuration_mismatch')
            self.assertFalse(result['pairing_available'])

    def test_stale_worker_not_ready(self):
        self.fixture()
        self.worker['updated_at'] = (datetime.now(timezone.utc) - timedelta(seconds=40)).isoformat()
        self.save()
        result = mobile_setup.connection_setup(self.config, configured=True)
        self.assertEqual(result['transport']['state'], 'status_stale')
        self.assertFalse(result['pairing_available'])

    def test_login_phase_is_not_private_listener(self):
        self.fixture(mode='login', phase='login_required')
        result = mobile_setup.connection_setup(self.config, configured=True)
        self.assertEqual(result['transport']['state'], 'login_required')
        self.assertFalse(result['local_listener_ready'])
        self.assertFalse(result['pairing_available'])

    def test_missing_process_or_identity_cannot_look_ready(self):
        self.fixture()
        for absent, expected in ((True, 'not_running'), (False, 'process_identity_unknown')):
            with mock.patch.object(frontend, '_process_identity', return_value=None), \
                 mock.patch.object(frontend, '_pid_absent', return_value=absent):
                result = mobile_setup.connection_setup(self.config, configured=True)
            self.assertEqual(result['transport']['state'], expected)
            self.assertFalse(result['pairing_available'])

    def test_lifecycle_change_during_observation_is_not_ready(self):
        self.fixture()
        with mock.patch.object(frontend, '_active', side_effect=[self.record, None]):
            result = mobile_setup.connection_setup(self.config, configured=True)
        self.assertEqual(result['transport']['state'], 'status_changed')
        self.assertFalse(result['pairing_available'])

    def test_symlink_or_unsafe_record_fails_closed(self):
        self.fixture()
        (self.root / 'active.json').chmod(0o644)
        result = mobile_setup.connection_setup(self.config, configured=True)
        self.assertEqual(result['transport']['state'], 'status_unavailable')
        self.assertFalse(result['pairing_available'])

    def test_helper_prompt_uses_current_installation_and_no_credentials(self):
        config = {**self.config, 'owner_key': 'DO_NOT_PRINT', 'mobile': {'identity': 'PRIVATE'}}
        prompt = mobile_setup.host_setup_prompt(config, installation_root=self.base / 'installation')
        self.assertIn(json.dumps(str(self.base / 'installation')), prompt)
        self.assertIn(json.dumps(config['config_path']), prompt)
        self.assertIn('docs/MOBILE-TSNET.md', prompt)
        self.assertIn('Android', prompt)
        self.assertIn('iPhone', prompt)
        self.assertNotIn('DO_NOT_PRINT', prompt)
        self.assertNotIn('PRIVATE', prompt)
        self.assertIsNone(mobile_setup.host_setup_prompt({}))
        self.assertIsNone(mobile_setup.host_setup_prompt({'config_path': 'relative'}))
        self.assertIsNone(mobile_setup.host_setup_prompt({'config_path': '/tmp/unsafe\npath'}))


if __name__ == '__main__':
    unittest.main()
