"""Synthetic admission/reporting integration; never native memory proof."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
import uuid
from unittest.mock import patch
import test_invitations as fixtures
import test_platforms as platform_fixtures


class NativeFixture:
    mode = 'grok_webhook'
    serial = True
    same_session = False

    def available(self, *args):
        return True


class ReportingOnboardingTests(unittest.TestCase):
    setUp = fixtures.InvitationTests.setUp
    invite = fixtures.InvitationTests.invite
    request = fixtures.InvitationTests.request
    claim_headers = fixtures.InvitationTests.claim_headers

    def tearDown(self):
        self.bridge.commands.close()
        fixtures.InvitationTests.tearDown(self)

    def claim(self, invitation):
        ident = uuid.uuid4().hex
        invite_path = self.bridge.invitations.paths(invitation['id'])[0]
        claim_path = invite_path.with_name('claim.json')
        fd = os.open(claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump({'claim_id': ident, 'workspace_path': str(self.root)}, output)
        code, result = self.request('/api/agents/join', {'claim_id': ident, 'workspace_path': str(self.root)},
            key=False, headers=self.claim_headers(invitation))
        self.assertEqual(code, 200)
        return result

    def ready(self, agent):
        definition = self.bridge.definitions[agent]
        self.bridge.commands.enabled = True
        self.bridge.commands.bindings[agent] = {'adapter': 'grok_webhook', 'session_id': definition['source_id']}
        self.bridge.commands.adapters['grok_webhook'] = NativeFixture()

    def join_cli(self, invitation):
        process = subprocess.run([sys.executable, str(Path(__file__).parent / 'adapters/join.py'), '--invite',
            str(self.bridge.invitations.paths(invitation['id'])[0])], cwd=self.root,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        return json.loads(process.stdout)

    def confirm(self, invitation, reporting):
        credential_path = self.bridge.invitations.paths(invitation['id'])[2]
        credential = json.loads(credential_path.read_text())
        data = {'setup_id': reporting['setup_id'], 'policy_version': reporting['policy_version'],
            'policy_hash': reporting['policy_hash'], 'readback_agent_id': credential['agent_id'],
            'readback_policy_version': reporting['policy_version'], 'evidence': 'agent_reported_saved'}
        return self.request('/api/agents/reporting/setup', data, key=False,
            headers={'X-Agent-Id': credential['agent_id'], 'Authorization': 'Bearer ' + credential['token']})

    def test_join_reports_correct_event_manifest_without_exposing_tokens(self):
        invitation = self.invite()
        claimed = self.claim(invitation)
        reporting = claimed['conversation']['reporting']
        self.assertEqual(reporting['state'], 'setup_required')
        self.assertEqual(reporting['method'], 'cooperative')
        manifest = Path(reporting['manifest_path']).read_text()
        original = self.bridge.invitations.paths(invitation['id'])[2]
        inbox = claimed['conversation']['credential_path']
        self.assertIn(str(original), manifest)
        self.assertNotIn(inbox, manifest)
        for path in (original, Path(inbox)):
            self.assertNotIn(json.loads(path.read_text())['token'], manifest)
        self.assertIn(reporting['guide_path'], Path(claimed['conversation']['guide_path']).read_text())
        self.assertIn('3b.', invitation['prompt'])
        self.assertIn('receiver_status=ready', invitation['prompt'])

    def test_ready_native_receiver_cannot_finish_before_cooperative_setup(self):
        invitation = self.invite()
        claimed = self.claim(invitation)
        self.ready(claimed['agent_id'])
        result = self.join_cli(invitation)
        self.assertTrue(result['joined'])
        self.assertEqual(result['receiver_status'], 'ready')
        self.assertEqual(result['reporting_status'], 'setup_required')
        self.assertTrue(result['reporting_setup_required'])
        self.assertFalse(result['full_onboarding_complete'])
        self.assertFalse(result['reporting_execution_verified'])
        self.assertIn('不能就此結束', result['next_step'])
        self.assertEqual(len(self.bridge.definitions), 1)

    def test_confirmed_cooperative_setup_is_distinct_from_manual_work_verification(self):
        invitation = self.invite()
        claimed = self.claim(invitation)
        self.ready(claimed['agent_id'])
        code, confirmation = self.confirm(invitation, claimed['conversation']['reporting'])
        self.assertEqual(code, 200, confirmation)
        result = self.join_cli(invitation)
        self.assertEqual(result['reporting_status'], 'agent_confirmed')
        self.assertFalse(result['reporting_setup_required'])
        self.assertTrue(result['full_onboarding_complete'])
        self.assertFalse(result['reporting_execution_verified'])
        self.assertFalse(result['native_connection']['execution_verified'])
        self.assertEqual(len(self.bridge.definitions), 1)

    def test_selected_legacy_guide_gets_one_upgrade_without_reenrollment(self):
        invitation = self.invite()
        claimed = self.claim(invitation)
        guide = self.bridge.invitations.paths(invitation['id'])[1]
        guide.write_text('Legacy admission instruction; retain original report identity.\n')
        before = set(self.bridge.definitions)
        first = self.bridge.invitations.get(invitation['id'])
        saved = guide.read_text()
        self.assertIn('Legacy admission instruction', saved)
        self.assertIn('OFFICE_PUBLIC_REPORTING_UPGRADE:', saved)
        self.assertIn(first['reporting']['guide_path'], saved)
        self.assertEqual(self.bridge.invitations.get(invitation['id'])['reporting']['setup_id'], first['reporting']['setup_id'])
        self.assertEqual(guide.read_text(), saved)
        self.assertEqual(set(self.bridge.definitions), before)
        self.assertEqual(first['agent_id'], claimed['agent_id'])
        self.assertEqual(guide.stat().st_mode & 0o777, 0o600)

    def test_rejected_invitations_do_not_rewrite_old_guides_or_prepare_reporting(self):
        revoked = self.invite(name='Revoked fixture', request_id='revoked-reporting-fixture')
        revoked_guide = self.bridge.invitations.paths(revoked['id'])[1]
        before = revoked_guide.read_bytes()
        self.bridge.invitations.revoke(revoked['id'])
        with patch.object(self.bridge.invitations, 'reporting_setup', side_effect=AssertionError('must not prepare')):
            result = self.bridge.invitations.get(revoked['id'])
        self.assertTrue(result['requires_new_invitation'])
        self.assertEqual(revoked_guide.read_bytes(), before)
        expired = self.invite(name='Expired fixture', request_id='expired-reporting-fixture')
        expired_guide = self.bridge.invitations.paths(expired['id'])[1]
        before = expired_guide.read_bytes()
        with self.bridge.history.db:
            self.bridge.history.db.execute('UPDATE invitations SET expires=? WHERE id=?', (time.time() - 1, expired['id']))
        with patch.object(self.bridge.invitations, 'reporting_setup', side_effect=AssertionError('must not prepare')):
            result = self.bridge.invitations.get(expired['id'])
        self.assertEqual(result['state'], 'expired')
        self.assertEqual(expired_guide.read_bytes(), before)

    def test_non_grok_native_admission_does_not_install_grok_reporting(self):
        invitation = self.invite(name='Cursor fixture', provider='cursor', request_id='cursor-reporting-fixture')
        claimed = self.claim(invitation)
        self.assertNotIn('reporting', claimed['conversation'])
        self.assertIsNone(self.bridge.invitations.get(invitation['id'])['reporting'])
        guide = self.bridge.invitations.paths(invitation['id'])[1].read_text()
        self.assertNotIn('OFFICE_PUBLIC_REPORTING_UPGRADE:', guide)

    def test_stale_event_credential_does_not_disable_existing_receiving_or_use_inbox(self):
        invitation = self.invite()
        claimed = self.claim(invitation)
        self.ready(claimed['agent_id'])
        credential = self.bridge.invitations.paths(invitation['id'])[2]
        event = json.loads(credential.read_text())
        event['token'] = 'fixture-invalid-replaced-event-token'
        credential.write_text(json.dumps(event))
        guide = self.bridge.invitations.paths(invitation['id'])[1]
        before = guide.read_bytes()
        conversation = self.bridge.inbox.ensure(claimed['agent_id'])
        self.assertTrue(conversation['capability']['persistent'])
        self.assertTrue(conversation['capability']['can_send'])
        self.assertEqual(conversation['reporting']['state'], 'stale')
        self.assertEqual(conversation['reporting']['reason'], 'reporting_credential_invalid')
        current = self.bridge.invitations.get(invitation['id'])
        self.assertEqual(current['reporting']['state'], 'stale')
        self.assertEqual(guide.read_bytes(), before)

    def test_concurrent_legacy_guide_edit_is_preserved_and_upgrade_reports_conflict(self):
        from invitations import InvitationError
        invitation = self.invite()
        self.claim(invitation)
        guide = self.bridge.invitations.paths(invitation['id'])[1]
        guide.write_text('Legacy guide before external edit.\n')
        original_read = os.read
        def edited_read(fd, size):
            value = original_read(fd, size)
            guide.write_text('External user edit must survive.\n')
            return value
        with patch('invitations.os.read', side_effect=edited_read):
            with self.assertRaises(InvitationError) as error:
                self.bridge.invitations.get(invitation['id'])
        self.assertEqual(error.exception.code, 'invitation_guide_changed')
        self.assertEqual(guide.read_text(), 'External user edit must survive.\n')

    def test_oversized_legacy_guide_is_not_read_or_modified(self):
        from invitations import InvitationError
        invitation = self.invite()
        self.claim(invitation)
        guide = self.bridge.invitations.paths(invitation['id'])[1]
        original = b'x' * (256 * 1024 + 1)
        guide.write_bytes(original)
        with patch('invitations.os.read', side_effect=AssertionError('oversized read')):
            with self.assertRaises(InvitationError) as error:
                self.bridge.invitations.get(invitation['id'])
        self.assertEqual(error.exception.code, 'invitation_guide_unavailable')
        self.assertEqual(guide.read_bytes(), original)


class LegacyReportingReceivingTests(unittest.TestCase):
    setUp = platform_fixtures.PlatformTests.setUp
    tearDown = platform_fixtures.PlatformTests.tearDown
    def test_legacy_manual_enrollment_retains_native_route_without_guessing_event_credential(self):
        from configuration import load_config, save_config
        config = load_config(self.path)
        agent = {'id': 'grok_bot:legacy-direct-root', 'source_id': 'legacy-direct-root',
            'provider': 'grok_bot', 'driver': 'push', 'name': 'Legacy fixture',
            'token_sha256': hashlib.sha256(b'fixture-legacy-event-token').hexdigest()}
        config['agents'].append(agent)
        config.setdefault('command_bindings', {})[agent['id']] = {'adapter': 'grok_webhook', 'session_id': agent['source_id']}
        save_config(self.path, config)
        self.bridge.refresh_connections(config)
        self.bridge.commands.adapters['grok_webhook'] = NativeFixture()
        conversation = self.bridge.inbox.ensure(agent['id'])
        self.assertTrue(conversation['capability']['persistent'])
        self.assertTrue(conversation['capability']['can_send'])
        self.assertEqual(conversation['reporting']['state'], 'unsupported')
        self.assertEqual(conversation['reporting']['reason'], 'reporting_original_credential_unknown')
        self.assertIsNone(conversation['reporting'].get('manifest_path'))


class ReportingResultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = str(Path(__file__).parent / 'adapters')
        sys.path.insert(0, directory)
        try:
            spec = importlib.util.spec_from_file_location('reporting_join_fixture', Path(directory) / 'join.py')
            cls.join = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.join)
        finally:
            sys.path.remove(directory)

    def test_ready_does_not_override_pending_stale_or_revoked_reporting(self):
        for state in ('setup_required', 'stale', 'revoked', 'unsupported'):
            with self.subTest(state=state):
                result = self.join.reporting_result({'reporting': {'method': 'cooperative', 'state': state}}, 'ready')
                self.assertFalse(result['full_onboarding_complete'])
                self.assertTrue(result['reporting_setup_required'])
                self.assertEqual(result['reporting_status'], state)
        result = self.join.reporting_result({'reporting': {'method': 'cooperative', 'state': 'agent_confirmed'}}, 'rejoin_required')
        self.assertFalse(result['full_onboarding_complete'])
        self.assertEqual(result['reporting_status'], 'revoked')

    def test_unavailable_reporting_paths_do_not_instruct_guessing_or_using_inbox(self):
        result = self.join.receiver_next_step('ready', {'method': 'cooperative', 'state': 'unsupported',
            'reason': 'reporting_original_credential_unknown'})
        self.assertIn('不要猜測路徑', result)
        self.assertIn('保留既有收訊入口', result)
        self.assertNotIn('現在讀取 conversation.reporting.guide_path', result)

    def test_missing_or_malformed_grok_reporting_metadata_never_completes_old_server_admission(self):
        ident = 'grok_bot:inv-' + 'a' * 32
        for metadata in (None, [], {}, {'method': 'native'},
                {'method': 'cooperative', 'state': 'agent_confirmed', 'agent_id': 'grok_bot:other'},
                {'method': 'cooperative', 'state': 'agent_confirmed', 'agent_id': ident}):
            with self.subTest(metadata=metadata):
                conversation = {'agent_id': ident, 'reporting': metadata, 'capability': {'persistent': True}}
                result = self.join.reporting_result(conversation, 'ready', ident)
                self.assertEqual(result['reporting_status'], 'setup_required')
                self.assertFalse(result['full_onboarding_complete'])
                self.assertTrue(result['reporting_setup_required'])
                next_step = self.join.receiver_next_step('ready', metadata, ident)
                self.assertNotIn('正常完成目前工作即可', next_step)
                self.assertIn('不要猜測路徑', next_step)


if __name__ == '__main__':
    unittest.main()
