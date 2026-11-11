"""Synthetic selected-chat onboarding checks; never evidence of a live App."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import uuid

import test_invitations as fixtures


class CursorOnboardingTests(unittest.TestCase):
    setUp=fixtures.InvitationTests.setUp
    request=fixtures.InvitationTests.request
    claim_headers=fixtures.InvitationTests.claim_headers

    def tearDown(self):
        self.bridge.commands.close()
        fixtures.InvitationTests.tearDown(self)

    def admission(self, provider='cursor'):
        invitation=fixtures.InvitationTests.invite(self,provider=provider)
        claim_id=uuid.uuid4().hex
        path=self.bridge.invitations.paths(invitation['id'])[0]
        claim=path.with_name('claim.json')
        fd=os.open(claim,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as stream:
            json.dump({'claim_id':claim_id,'workspace_path':str(self.root)},stream)
        status,joined=self.request('/api/agents/join',{'claim_id':claim_id,'workspace_path':str(self.root)},
            key=False,headers=self.claim_headers(invitation))
        self.assertEqual(status,200,joined)
        self.bridge.commands.enabled=True
        return invitation,joined

    def original(self, invitation):
        path=self.bridge.invitations.paths(invitation['id'])[2]
        return path,json.loads(path.read_text())

    def conversation(self, ident):
        status,value=self.request('/api/office/conversation',{'agent_id':ident})
        self.assertEqual(status,200,value)
        return value

    def join_cli(self, invitation):
        process=subprocess.run([sys.executable,str(Path(__file__).parent/'adapters/join.py'),'--invite',
            str(self.bridge.invitations.paths(invitation['id'])[0])],cwd=self.root,
            stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=10)
        self.assertEqual(process.returncode,0,process.stderr)
        return json.loads(process.stdout)

    def test_one_prompt_requires_public_sync_despite_unsupported_receiving(self):
        invitation,joined=self.admission()
        self.assertIn('3c.',invitation['prompt'])
        self.assertIn('即使 receiver_status=unsupported',invitation['prompt'])
        result=self.join_cli(invitation)
        self.assertTrue(result['joined'])
        self.assertEqual(result['receiver_status'],'unsupported')
        self.assertEqual(result['observation_status'],'setup_required')
        self.assertTrue(result['observation_setup_required'])
        self.assertFalse(result['public_sync_onboarding_complete'])
        self.assertFalse(result['full_onboarding_complete'])
        self.assertIn('不能因 receiver_status=unsupported 就此結束',result['next_step'])
        self.assertIn('本次原聊天公開最終回覆',result['next_step'])
        self.assertEqual(len(self.bridge.definitions),1)

    def test_descriptor_uses_only_original_event_credential_and_install_relative_program(self):
        invitation,joined=self.admission()
        observation=joined['conversation']['observation']
        original,credential=self.original(invitation)
        inbox=Path(joined['conversation']['credential_path'])
        argv=observation['setup_command']
        self.assertEqual(argv[2:4],['--credential',str(original)])
        self.assertEqual(argv[-3],'cursor-children')
        self.assertNotIn(str(inbox),argv)
        self.assertTrue(Path(observation['guide_path']).is_relative_to(self.root))
        guide=Path(observation['guide_path']).read_text()
        self.assertIn(str(original),guide)
        self.assertNotIn(str(inbox),guide)
        self.assertIn('一次性批准',guide)
        self.assertIn('afterAgentResponse',guide)
        for secret in (credential['token'],json.loads(inbox.read_text())['token'],self.bridge.config['topic']):
            self.assertNotIn(secret,json.dumps(observation))
            self.assertNotIn(secret,guide)
        self.assertFalse(observation['configured'])
        self.assertFalse(observation['execution_verified'])
        self.assertFalse(observation['background_delivery'])

    def test_pending_native_marker_never_counts_as_configured(self):
        invitation,joined=self.admission()
        _,credential=self.original(invitation)
        pending=self.bridge.child_events.register_pending(joined['agent_id'],credential['token'])
        self.assertTrue(pending['pending'])
        self.assertTrue(pending['activation_marker'].startswith('OFFICE_CURSOR_BIND_'))
        result=self.join_cli(invitation)
        self.assertEqual(result['observation_status'],'binding_required')
        self.assertFalse(result['conversation']['observation']['configured'])
        self.assertFalse(result['public_sync_onboarding_complete'])
        self.assertIn('activation_marker',result['next_step'])
        self.assertFalse(result['native_connection']['connected'])

    def test_cold_join_immediately_authorizes_scoped_observer_without_background_poll(self):
        invitation,joined=self.admission()
        _,credential=self.original(invitation)
        status,pending=self.request('/api/agents/observers/pending',{},key=False,headers={
            'X-Agent-Id':joined['agent_id'],'Authorization':'Bearer '+credential['token']})
        self.assertEqual(status,200,pending)
        self.assertTrue(pending['pending'])
        self.assertEqual(pending['agent_id'],joined['agent_id'])
        self.assertEqual(self.conversation(joined['agent_id'])['observation']['state'],'binding_required')

    def test_server_verified_binding_completes_sync_only_and_is_reused(self):
        invitation,joined=self.admission()
        _,credential=self.original(invitation)
        pending=self.bridge.child_events.register_pending(joined['agent_id'],credential['token'])
        observer=json.loads(Path(pending['credential_path']).read_text())
        bound=self.bridge.child_events.bind(pending['observer_id'],observer['token'],
            {'native_session_id':'cursor-fixture-original-session','activation_marker':pending['activation_marker']})
        before=Path(bound['credential_path']).read_bytes()
        result=self.join_cli(invitation)
        self.assertEqual(result['observation_status'],'configured')
        self.assertTrue(result['public_sync_onboarding_complete'])
        self.assertFalse(result['observation_setup_required'])
        self.assertFalse(result['observation_execution_verified'])
        self.assertFalse(result['full_onboarding_complete'])
        self.assertIn('公開同步綁定已由伺服器確認',result['next_step'])
        self.assertIn('OFFICE 只能保存留言',result['next_step'])
        self.assertEqual(Path(bound['credential_path']).read_bytes(),before)

    def test_missing_current_observer_file_does_not_report_configured(self):
        invitation,joined=self.admission()
        binding=self.bridge.child_events.create_observer({'agent_id':joined['agent_id'],
            'provider':'cursor','native_session_id':'cursor-fixture-original-session'})
        Path(binding['credential_path']).unlink()
        observation=self.conversation(joined['agent_id'])['observation']
        self.assertEqual(observation['state'],'stale')
        self.assertFalse(observation['configured'])
        self.assertEqual(observation['reason'],'native_observer_credential_unavailable')

    def test_inbox_credential_cannot_be_reused_as_original_event_authority(self):
        invitation,joined=self.admission()
        original,_=self.original(invitation)
        original.write_bytes(Path(joined['conversation']['credential_path']).read_bytes())
        observation=self.conversation(joined['agent_id'])['observation']
        self.assertEqual(observation['state'],'stale')
        self.assertIsNone(observation['setup_command'])
        self.assertIsNone(observation['guide_path'])
        self.assertFalse(observation['configured'])

    def test_missing_original_never_reads_or_restores_logged_out_credential(self):
        invitation,joined=self.admission()
        original,_=self.original(invitation)
        retired=original.with_name('credential.logged-out-'+uuid.uuid4().hex+'.json')
        retired.write_text('RETIRED_PRIVATE_NON_JSON_DO_NOT_READ')
        original.unlink()
        observation=self.conversation(joined['agent_id'])['observation']
        self.assertEqual(observation['state'],'stale')
        self.assertIsNone(observation['setup_command'])
        self.assertEqual(retired.read_text(),'RETIRED_PRIVATE_NON_JSON_DO_NOT_READ')
        self.assertFalse(original.exists())

    def test_valid_json_non_object_credential_is_stale_without_setup_authority(self):
        invitation,joined=self.admission()
        original,_=self.original(invitation)
        for value in ([],None,'not-a-credential',42):
            with self.subTest(value=value):
                original.write_text(json.dumps(value))
                observation=self.conversation(joined['agent_id'])['observation']
                self.assertEqual(observation['state'],'stale')
                self.assertIsNone(observation['setup_command'])
                self.assertIsNone(observation['guide_path'])

    def test_logout_revokes_observation_and_old_invitation_cannot_reactivate(self):
        invitation,joined=self.admission()
        _,credential=self.original(invitation)
        self.bridge.child_events.register_pending(joined['agent_id'],credential['token'])
        self.assertEqual(self.request('/api/office/agents/logout',{'agent_id':joined['agent_id']})[0],200)
        status,value=self.request('/api/office/conversation',{'agent_id':joined['agent_id']})
        self.assertEqual(status,404)
        status,old=self.request('/api/office/invitations?id='+invitation['id'])
        self.assertEqual(status,200)
        self.assertTrue(old['requires_new_invitation'])
        self.assertNotIn(joined['agent_id'],self.bridge.definitions)

    def test_other_providers_keep_their_existing_contract(self):
        for provider in ('codex','generic','grok_bot'):
            with self.subTest(provider=provider):
                invitation=fixtures.InvitationTests.invite(self,name='Other '+provider,provider=provider,
                    request_id='other-provider-'+provider)
                claim_id=uuid.uuid4().hex
                status,value=self.request('/api/agents/join',{'claim_id':claim_id},key=False,
                    headers=self.claim_headers(invitation))
                self.assertEqual(status,200,value)
                self.assertNotIn('observation',value['conversation'])
                if provider=='grok_bot':self.assertIn('reporting',value['conversation'])

    def test_cursor_cloud_binding_uses_its_route_without_local_hook_setup(self):
        class CloudFixture:
            mode='cursor_cloud'
            serial=True
            same_session=True
            def available(self,*args):return True
        invitation,joined=self.admission()
        self.bridge.commands.bindings[joined['agent_id']]={'adapter':'cursor_cloud','session_id':'bc-fixture'}
        self.bridge.commands.adapters['cursor_cloud']=CloudFixture()
        conversation=self.conversation(joined['agent_id'])
        self.assertEqual(conversation['capability']['mode'],'cursor_cloud')
        self.assertNotIn('observation',conversation)
        result=self.join_cli(invitation)
        self.assertEqual(result['observation_status'],'not_required')
        self.assertFalse(result['observation_setup_required'])
        self.assertEqual(result['receiver_status'],'ready')


class CursorObservationResultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory=str(Path(__file__).parent/'adapters')
        sys.path.insert(0,directory)
        try:
            spec=importlib.util.spec_from_file_location('cursor_onboarding_join_fixture',Path(directory)/'join.py')
            cls.join=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.join)
        finally:
            sys.path.remove(directory)

    def test_cursor_metadata_fails_closed_and_cloud_is_excluded(self):
        ident='cursor:inv-'+'a'*32
        for value in (None,[],{}, {'method':'native_hook','agent_id':'cursor:wrong','state':'configured'},
                {'method':'native_hook','agent_id':ident,'state':'configured','configured':False}):
            with self.subTest(value=value):
                result=self.join.observation_result({'observation':value},'unsupported',ident)
                self.assertEqual(result['observation_status'],'setup_required')
                self.assertTrue(result['observation_setup_required'])
        cloud=self.join.observation_result({'capability':{'mode':'cursor_cloud'}},'ready',ident)
        self.assertEqual(cloud['observation_status'],'not_required')
        self.assertFalse(cloud['observation_setup_required'])
        for ident in ('codex:fixture','generic:fixture','grok_bot:fixture'):
            self.assertEqual(self.join.observation_result({},'ready',ident)['observation_status'],'not_required')

    def test_revocation_overrides_previously_configured_observation(self):
        ident='cursor:inv-'+'a'*32
        observation={'method':'native_hook','agent_id':ident,'state':'configured','configured':True}
        result=self.join.observation_result({'observation':observation},'rejoin_required',ident)
        self.assertEqual(result['observation_status'],'revoked')
        self.assertFalse(result['public_sync_onboarding_complete'])
        text=self.join.receiver_next_step('rejoin_required',agent_id=ident,observation=observation)
        self.assertIn('不要重用舊指引',text)


if __name__=='__main__':
    unittest.main()
