"""Only synthetic selected-Agent manifests/receipts; no native Bot actions."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from history import HistoryStore
from public_reporting import PublicReporting, ReportingError, POLICY_VERSION, EVIDENCE


class PublicReportingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.source = 'inv-' + '1' * 32
        self.agent = {'id': 'grok_bot:' + self.source, 'provider': 'grok_bot', 'driver': 'push',
            'source_id': self.source, 'name': 'Fixture', 'token_sha256': hashlib.sha256(b'fixture-token').hexdigest()}
        self.config = {'data_dir': str(self.root), 'port': 4318, 'agents': [self.agent]}
        self.history = HistoryStore(self.root / 'history.sqlite3')
        self.bridge = SimpleNamespace(config=self.config, definitions={self.agent['id']: self.agent},
            commands=SimpleNamespace(lock=threading.RLock()), history=self.history)
        self.credential_path = self.root / 'invitations' / self.source[4:] / 'credential.json'
        self.credential_path.parent.mkdir(parents=True, mode=0o700)
        self.credential = {'endpoint': 'http://127.0.0.1:4318/api/agents/events',
            'agent_id': self.agent['id'], 'source_id': self.source, 'token': 'fixture-token'}
        self.write(self.credential_path, self.credential)
        self.service = PublicReporting(self.bridge)

    def tearDown(self):
        self.history.close()
        self.tmp.cleanup()

    @staticmethod
    def write(path, data):
        path.write_text(json.dumps(data))
        path.chmod(0o600)

    def body(self, result):
        return {'setup_id': result['setup_id'], 'policy_version': result['policy_version'],
            'policy_hash': result['policy_hash'], 'readback_agent_id': self.agent['id'],
            'readback_policy_version': result['policy_version'], 'evidence': EVIDENCE}

    def helper(self):
        path = Path(__file__).parent / 'adapters/reporting_setup.py'
        spec = importlib.util.spec_from_file_location('fixture_reporting_helper', path)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        return helper

    def test_constructor_and_fresh_status_do_not_create_a_table_directory_or_run(self):
        before = list(self.history.db.iterdump())
        result = self.service.status(self.agent['id'])
        self.assertEqual(result['state'], 'setup_required')
        self.assertEqual(result['method'], 'cooperative')
        self.assertIsNone(result['setup_id'])
        self.assertEqual(list(self.history.db.iterdump()), before)
        self.assertFalse((self.root / 'reporting').exists())

    def test_ensure_creates_a_stable_private_token_free_manifest_with_original_event_credential(self):
        first = self.service.ensure(self.agent['id'])
        path = Path(first['manifest_path'])
        manifest = json.loads(path.read_text())
        stamp = path.stat().st_mtime_ns
        second = self.service.ensure(self.agent['id'])
        self.assertEqual(first, second)
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        self.assertEqual(manifest['credential_path'], str(self.credential_path))
        self.assertNotIn('inbox', manifest['credential_path'])
        self.assertEqual(manifest['agent_id'], self.agent['id'])
        self.assertEqual(manifest['policy_version'], POLICY_VERSION)
        self.assertIn('readback', Path(first['guide_path']).read_text())
        for file in [path, Path(first['guide_path'])]:
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('fixture-token', file.read_text())
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(path.parent.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.history.db.execute('SELECT count(*) FROM runs').fetchone()[0], 0)

    def test_confirm_records_only_cooperative_saved_evidence_and_repeats_idempotently(self):
        staged = self.service.ensure(self.agent['id'])
        confirmed = self.service.confirm(self.agent['id'], self.body(staged))
        self.assertEqual(confirmed['state'], 'agent_confirmed')
        self.assertEqual(confirmed['evidence'], 'agent_reported_saved')
        self.assertFalse(confirmed['native_observer'])
        self.assertFalse(confirmed['manual_work_verified'])
        self.assertEqual(self.service.confirm(self.agent['id'], self.body(staged)), confirmed)
        self.assertEqual(self.service.ensure(self.agent['id']), confirmed)
        before = list(self.history.db.iterdump())
        self.assertEqual(self.service.status(self.agent['id']), confirmed)
        self.assertEqual(list(self.history.db.iterdump()), before)

    def test_wrong_nonce_hash_version_agent_or_evidence_does_not_confirm(self):
        result = self.service.ensure(self.agent['id'])
        for field, value in [('setup_id', 'wrong'), ('policy_hash', 'f' * 64), ('policy_version', 'old'),
                ('readback_agent_id', 'grok_bot:another'), ('readback_policy_version', 'old'),
                ('evidence', 'native_observer'), ('readback_agent_id', '另一位 Agent')]:
            with self.subTest(field=field), self.assertRaises(ReportingError):
                self.service.confirm(self.agent['id'], {**self.body(result), field: value})
        self.assertEqual(self.service.status(self.agent['id'])['state'], 'setup_required')

    def test_extra_confirmation_fields_or_missing_setup_are_rejected(self):
        with self.assertRaises(ReportingError):
            self.service.confirm(self.agent['id'], self.body({'setup_id': 'a', 'policy_version': POLICY_VERSION, 'policy_hash': 'b'}))
        result = self.service.ensure(self.agent['id'])
        with self.assertRaises(ReportingError):
            self.service.confirm(self.agent['id'], {**self.body(result), 'token': 'not-accepted'})

    def test_new_selected_credential_generation_invalidates_old_receipt(self):
        result = self.service.ensure(self.agent['id'])
        self.service.confirm(self.agent['id'], self.body(result))
        self.agent['token_sha256'] = hashlib.sha256(b'rotated-fixture-token').hexdigest()
        self.credential['token'] = 'rotated-fixture-token'
        self.write(self.credential_path, self.credential)
        self.assertEqual(self.service.status(self.agent['id'])['state'], 'stale')
        with self.assertRaises(ReportingError):
            self.service.confirm(self.agent['id'], self.body(result))
        fresh = self.service.ensure(self.agent['id'])
        self.assertNotEqual(fresh['setup_id'], result['setup_id'])
        self.assertEqual(fresh['state'], 'setup_required')
        with self.assertRaises(ReportingError):
            self.service.confirm(self.agent['id'], self.body(result))

    def test_policy_change_invalidates_receipt_and_new_setup_uses_new_hash(self):
        old = self.service.ensure(self.agent['id'])
        self.service.confirm(self.agent['id'], self.body(old))
        with patch('public_reporting.grok_public_reporting_policy', return_value='new fixture policy'):
            self.assertEqual(self.service.status(self.agent['id'])['state'], 'stale')
            new = self.service.ensure(self.agent['id'])
            self.assertNotEqual(new['policy_hash'], old['policy_hash'])
            self.assertNotEqual(new['setup_id'], old['setup_id'])
            self.assertEqual(new['state'], 'setup_required')

    def test_removed_agent_is_revoked_without_recreating_files_or_tables(self):
        result = self.service.ensure(self.agent['id'])
        self.service.confirm(self.agent['id'], self.body(result))
        self.bridge.definitions.clear()
        before = list(self.history.db.iterdump())
        self.assertEqual(self.service.status(self.agent['id'])['state'], 'revoked')
        self.assertEqual(list(self.history.db.iterdump()), before)
        with self.assertRaises(ReportingError):
            self.service.ensure(self.agent['id'])
        with self.assertRaises(ReportingError):
            self.service.confirm(self.agent['id'], self.body(result))

    def test_wrong_provider_and_unknown_legacy_credential_are_explicitly_unsupported(self):
        self.agent['provider'] = 'generic'
        self.assertEqual(self.service.status(self.agent['id'])['state'], 'unsupported')
        self.agent['provider'] = 'grok_bot'
        self.agent['source_id'] = 'legacy-source'
        self.assertEqual(self.service.status(self.agent['id'])['reason'], 'reporting_original_credential_unknown')
        self.assertFalse((self.root / 'reporting').exists())

    def test_inbox_endpoint_wrong_identity_or_bad_token_cannot_be_reporter_binding(self):
        for field, value in [('endpoint', 'http://127.0.0.1:4318/api/agents/inbox'),
                ('endpoint', 'https://example.invalid/api/agents/events'),
                ('agent_id', 'grok_bot:wrong'), ('token', 'wrong')]:
            credential = {**self.credential, field: value}
            self.write(self.credential_path, credential)
            with self.subTest(field=field), self.assertRaises(ReportingError):
                self.service.ensure(self.agent['id'])
        self.assertFalse((self.root / 'reporting').exists())

    def test_symlink_loose_credential_and_symlink_manifest_are_rejected(self):
        self.credential_path.chmod(0o644)
        with self.assertRaises(ReportingError):
            self.service.ensure(self.agent['id'])
        self.credential_path.chmod(0o600)
        result = self.service.ensure(self.agent['id'])
        path = Path(result['manifest_path'])
        path.unlink()
        path.symlink_to(self.credential_path)
        with self.assertRaises(ReportingError):
            self.service.ensure(self.agent['id'])
        self.assertEqual(self.service.status(self.agent['id'])['state'], 'stale')

    def test_changed_manifest_paths_are_not_blessed_by_matching_nonce(self):
        result = self.service.ensure(self.agent['id'])
        manifest = json.loads(Path(result['manifest_path']).read_text())
        manifest['reporter'] = '/wrong/reporter.py'
        self.write(Path(result['manifest_path']), manifest)
        self.assertEqual(self.service.status(self.agent['id'])['state'], 'stale')
        with self.assertRaises(ReportingError):
            self.service.confirm(self.agent['id'], self.body(result))

    def test_failed_sidecar_write_rolls_back_setup_receipt(self):
        with patch.object(self.service, '_write', side_effect=ReportingError('fixture_write_failure', 409)):
            with self.assertRaises(ReportingError):
                self.service.ensure(self.agent['id'])
        self.assertIsNone(self.service._row(self.agent['id']))

    def test_helper_uses_original_event_token_and_exact_setup_endpoint(self):
        result = self.service.ensure(self.agent['id'])
        helper = self.helper()
        seen = []
        def urlopen(request, timeout):
            seen.append(request)
            body = json.loads(request.data)
            confirmed = self.service.confirm(self.agent['id'], body)
            return io.BytesIO(json.dumps(confirmed).encode())
        with patch.object(helper, 'open_local', side_effect=urlopen):
            confirmed = helper.confirm(result['manifest_path'], POLICY_VERSION, self.agent['id'])
        self.assertEqual(confirmed['state'], 'agent_confirmed')
        self.assertEqual(seen[0].full_url, 'http://127.0.0.1:4318/api/agents/reporting/setup')
        self.assertEqual(seen[0].get_header('Authorization'), 'Bearer fixture-token')
        self.assertEqual(json.loads(seen[0].data), self.body(result))
        self.assertNotIn('fixture-token', json.dumps(confirmed))

    def test_helper_wrong_readback_manifest_target_or_remote_endpoint_never_posts(self):
        result = self.service.ensure(self.agent['id'])
        helper = self.helper()
        with patch.object(helper, 'open_local') as network:
            for version, agent in [('old', self.agent['id']), (POLICY_VERSION, 'another-agent')]:
                with self.assertRaises(ReportingError):
                    helper.confirm(result['manifest_path'], version, agent)
            manifest_path = Path(result['manifest_path'])
            manifest = json.loads(manifest_path.read_text())
            manifest['credential_path'] = str(self.root / 'outside.json')
            self.write(manifest_path, manifest)
            with self.assertRaises(ReportingError):
                helper.confirm(manifest_path, POLICY_VERSION, self.agent['id'])
            self.assertFalse(network.called)


if __name__ == '__main__':
    unittest.main()
