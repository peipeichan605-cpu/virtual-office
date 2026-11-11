import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from configuration import initialize,load_config,save_config
from dashboard import Bridge,proxy_handler
from invitations import InvitationError

class InvitationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve();self.path=self.root/'config.json'
        initialize(self.path);config=load_config(self.path);config.update(run_id='fixture',run_dir=str(self.root/'run'))
        save_config(self.path,config);self.bridge=Bridge(config,dry_run=True)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),proxy_handler(self.bridge));self.port=self.server.server_address[1]
        config['port']=self.port;save_config(self.path,config);self.base='http://127.0.0.1:'+str(self.port)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.bridge.history.close();self.temp.cleanup()
    def request(self,path,body=None,key=True,headers=None):
        h={'Content-Type':'application/json'}
        if key:h['X-Office-Key']=self.bridge.config['topic']
        h.update(headers or {})
        req=urllib.request.Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,headers=h)
        try:
            with urllib.request.urlopen(req,timeout=3) as response:return response.status,json.load(response)
        except urllib.error.HTTPError as error:return error.code,json.load(error)
    def invite(self,name='Fixture Agent',provider='grok_bot',request_id='request-fixture-one'):
        code,data=self.request('/api/office/invitations',{'name':name,'provider':provider,'request_id':request_id})
        self.assertEqual(code,200);return data
    def claim_headers(self,invitation):
        path=self.bridge.invitations.paths(invitation['id'])[0];private=json.loads(path.read_text())
        return {'Authorization':'Bearer '+private['token'],'X-Invitation-Id':invitation['id']}
    def test_owner_auth_and_origin_are_required(self):
        body={'name':'Private fixture','provider':'generic','request_id':'request-fixture-one'}
        self.assertEqual(self.request('/api/office/invitations',body,key=False)[0],403)
        self.assertEqual(self.request('/api/office/invitations',body,headers={'Origin':'https://attacker.example'})[0],403)
        invite=self.invite();self.assertEqual(self.request('/api/office/invitations?id='+invite['id'],key=False)[0],403)
        self.assertEqual(self.request('/api/agents/join',{'claim_id':'a'*32},key=False,headers={'X-Invitation-Id':invite['id'],'Authorization':'Bearer wrong'})[0],403)
    def test_pending_invitation_has_no_avatar_and_prompt_has_no_secrets(self):
        invite=self.invite();self.assertEqual(invite['state'],'pending');self.assertEqual(len(self.bridge.agents),0)
        self.assertIn('不代表入場失敗',invite['prompt'])
        self.assertIn('persistent=true 亦不能單獨證明平台目前可用',invite['prompt'])
        self.assertEqual(load_config(self.path)['agents'],[])
        paths=self.bridge.invitations.paths(invite['id']);private=json.loads(paths[0].read_text());credential=json.loads(paths[2].read_text())
        for secret in [private['token'],credential['token'],self.bridge.config['topic']]:self.assertNotIn(secret,json.dumps(invite))
        for path in paths:self.assertEqual(path.stat().st_mode&0o777,0o600)
        again=self.invite(request_id='request-fixture-two');self.assertEqual(again['id'],invite['id'])
        changed=self.request('/api/office/invitations',{'name':'Changed','provider':'generic','request_id':'request-fixture-one'})
        self.assertEqual(changed[0],409)
    def test_one_invitation_prompt_continues_through_receiving_setup_for_selected_provider(self):
        generated=[]
        for provider in ('codex','grok_bot','cursor','generic'):
            with self.subTest(provider=provider):
                invitation=self.invite(name='Selected '+provider+' fixture',provider=provider,request_id='combined-'+provider+'-fixture')
                invite,guide,credential=self.bridge.invitations.paths(invitation['id'])
                prompt=invitation['prompt'];saved_guide=guide.read_text()
                self.assertIn('這份 Prompt 同時包含入場及持續收訊設定',prompt)
                self.assertIn(str(guide),prompt)
                self.assertIn(str(invite),saved_guide)
                self.assertIn('adapters/join.py',saved_guide)
                for text in (prompt,saved_guide):
                    self.assertIn('在同一個原 session',text)
                    self.assertIn('毋須人類再貼第二段 Prompt',text)
                    for required in ('joined=true','兩個 accepted=true','run_id','首次回報','conversation.guide_path',
                            '身份綁定及公開同步','接續設定本平台的持續收訊入口','保留目前 session 身份','receiver_status'):
                        self.assertIn(required,text)
                    self.assertLess(text.index('1. 執行入場及首次回報'),text.index('2. 先檢查 receiver_status'))
                    self.assertLess(text.index('2. 先檢查 receiver_status'),text.index('3. 接續設定本平台的持續收訊入口'))
                    for state in ('ready','configured_unavailable','setup_required','unsupported','disabled','rejoin_required'):
                        self.assertIn(state,text)
                    self.assertIn('只能保存留言、未能背景喚醒',text)
                    self.assertIn('一次性批准',text);self.assertIn('等人類批准',text)
                    self.assertIn('不要繞過批准',text)
                    self.assertIn('requires_rejoin=true，停止使用舊指引',text)
                    self.assertIn('產生新邀請',text)
                    self.assertIn('未有相配的接收、執行及交付證據',text)
                    self.assertIn('不宣稱端到端完成',text)
                    self.assertIn('說明入口是否保留同一 session',text)
                    self.assertIn('若只綁定同一 Bot，清楚說明未保證原聊天',text)
                private=json.loads(invite.read_text());reporting=json.loads(credential.read_text())
                for secret in (private['token'],reporting['token'],self.bridge.config['topic']):
                    self.assertNotIn(secret,prompt);self.assertNotIn(secret,saved_guide)
                generated.append((guide,prompt,saved_guide))
        for own_guide,prompt,saved_guide in generated:
            for other_guide,_,_ in generated:
                if other_guide!=own_guide:
                    self.assertNotIn(str(other_guide),prompt)
                    self.assertNotIn(str(other_guide),saved_guide)
        self.assertEqual(load_config(self.path)['agents'],[])
        self.assertEqual(len(self.bridge.agents),0)
    def test_grok_connection_card_is_scoped_and_shared_with_receiver_guide(self):
        first=self.invite(name='Selected Bot Alpha',request_id='grok-card-alpha')
        second=self.invite(name='Selected Bot Beta',request_id='grok-card-beta')
        title=first['grok_setup']['suggested_routine_name']
        self.assertNotEqual(title,second['grok_setup']['suggested_routine_name'])
        self.assertEqual(title,self.request('/api/office/invitations?id='+first['id'])[1]['grok_setup']['suggested_routine_name'])
        for text in (first['prompt'],self.bridge.invitations.paths(first['id'])[1].read_text()):
            self.assertIn(title,text)
            self.assertIn('OFFICE 連接卡',text)
            self.assertIn('直接列出地址本身',text)
            self.assertIn('我無法讀取收訊地址',text)
            self.assertIn('不要在聊天列印 key',text)
            self.assertIn('不要另建、改名或覆寫 routine',text)
            self.assertNotIn(second['grok_setup']['suggested_routine_name'],text)
        code,joined=self.request('/api/agents/join',{'claim_id':'b'*32},key=False,headers=self.claim_headers(first))
        self.assertEqual(code,200)
        receiver=joined['conversation']
        self.assertEqual(receiver['grok_setup'],first['grok_setup'])
        self.assertIn(title,Path(receiver['guide_path']).read_text())
        native=self.invite(name='Codex card fixture',provider='codex',request_id='codex-card-fixture')
        self.assertNotIn('grok_setup',native)
        self.assertNotIn('OFFICE 連接卡',native['prompt'])

    def test_actual_join_command_registers_without_restart_and_replays_safely(self):
        invite=self.invite(provider='cursor');path=self.bridge.invitations.paths(invite['id'])[0]
        command=[sys.executable,str(Path(__file__).parent/'adapters/join.py'),'--invite',str(path)]
        first=subprocess.run(command,capture_output=True,text=True,timeout=15);self.assertEqual(first.returncode,0,first.stderr)
        receipt=json.loads(first.stdout);self.assertTrue(receipt['joined']);self.assertTrue(all(e['accepted'] for e in receipt['events']))
        self.assertFalse(receipt['conversation']['capability']['persistent'])
        self.assertEqual(receipt['native_connection']['reason'],'cursor_local_background_unavailable')
        guide=Path(receipt['conversation']['guide_path']).read_text()
        self.assertIn('不代表入場失敗',guide)
        self.assertNotIn('connect.py',guide)
        self.assertNotIn('inbox.py wait',guide)
        self.assertEqual(len(self.bridge.agents),1);agent=next(iter(self.bridge.agents));self.assertEqual(self.bridge.sources['Fixture Agent']['status'],'done')
        self.assertEqual(len(self.bridge.history.messages({agent},agent)),2)
        self.assertEqual(self.bridge.invitations.get(invite['id'])['state'],'connected')
        second=subprocess.run(command,capture_output=True,text=True,timeout=15);self.assertEqual(second.returncode,0,second.stderr)
        self.assertTrue(all(e['duplicate'] for e in json.loads(second.stdout)['events']))
        self.assertEqual(len(self.bridge.history.messages({agent},agent)),2)
        headers=self.claim_headers(invite)
        self.assertEqual(self.request('/api/agents/join',{'claim_id':'b'*32},key=False,headers=headers)[0],409)
        restored=Bridge(load_config(self.path),dry_run=True)
        try:self.assertEqual(restored.invitations.get(invite['id'])['state'],'connected');self.assertEqual(len(restored.agents),1)
        finally:restored.history.close()
    def test_registered_does_not_mean_first_report_received(self):
        invite=self.invite();headers=self.claim_headers(invite)
        code,result=self.request('/api/agents/join',{'claim_id':'a'*32},key=False,headers=headers)
        self.assertEqual(code,200);self.assertTrue(result['registered'])
        self.assertEqual(self.bridge.invitations.get(invite['id'])['state'],'claimed')
        self.assertFalse(self.bridge.invitations.get(invite['id'])['first_report_received'])
        self.assertEqual(self.bridge.sources['Fixture Agent']['status'],'waiting_user')
        self.assertEqual(self.request('/api/office/invitations/revoke',{'id':invite['id']})[0],409)
    def test_revoked_and_expired_invitations_cannot_claim(self):
        invite=self.invite();headers=self.claim_headers(invite)
        self.request('/api/office/invitations/revoke',{'id':invite['id']})
        revoked=self.bridge.invitations.get(invite['id'])
        self.assertTrue(revoked['requires_new_invitation'])
        self.assertNotIn('agent-guide.txt',revoked['prompt'])
        self.assertNotIn('conversation.guide_path',revoked['prompt'])
        self.assertNotIn('join.py',revoked['prompt'])
        self.assertNotIn('這份 Prompt 同時包含',revoked['prompt'])
        self.assertIn('不要執行舊指引',revoked['prompt'])
        self.assertEqual(self.request('/api/agents/join',{'claim_id':'a'*32},key=False,headers=headers)[0],410)
        next_invite=self.invite(request_id='request-fixture-two');headers=self.claim_headers(next_invite)
        with self.bridge.history.lock,self.bridge.history.db:self.bridge.history.db.execute('UPDATE invitations SET expires=? WHERE id=?',(time.time()-1,next_invite['id']))
        self.assertEqual(self.bridge.invitations.get(next_invite['id'])['state'],'expired')
        self.assertTrue(self.bridge.invitations.get(next_invite['id'])['requires_new_invitation'])
        expired=self.bridge.invitations.get(next_invite['id'])
        for executable_reference in ('agent-guide.txt','conversation.guide_path','join.py','這份 Prompt 同時包含'):
            self.assertNotIn(executable_reference,expired['prompt'])
        self.assertIn('不要執行舊指引',expired['prompt'])
        self.assertEqual(self.request('/api/agents/join',{'claim_id':'a'*32},key=False,headers=headers)[0],410)
        self.assertEqual(len(self.bridge.agents),0)
    def test_cursor_logout_requires_fresh_invitation_without_auto_reenrollment(self):
        old=self.invite(provider='cursor')
        command=[sys.executable,str(Path(__file__).parent/'adapters/join.py'),'--invite',str(self.bridge.invitations.paths(old['id'])[0])]
        joined=subprocess.run(command,capture_output=True,text=True,timeout=15)
        self.assertEqual(joined.returncode,0,joined.stderr)
        receipt=json.loads(joined.stdout)
        old_channel=Path(receipt['conversation']['credential_path'])
        self.bridge.members.logout({'agent_id':old['agent_id']})
        self.assertFalse(old_channel.exists())
        self.assertTrue(list(old_channel.parent.glob('credential.logged-out-*.json')))
        self.assertTrue(self.bridge.invitations.get(old['id'])['requires_new_invitation'])
        self.assertEqual(self.request('/api/agents/join',{'claim_id':'a'*32},key=False,headers=self.claim_headers(old))[0],410)
        fresh=self.invite(provider='cursor',request_id='request-fresh-after-logout')
        self.assertNotEqual(fresh['id'],old['id'])
        self.assertEqual(fresh['state'],'pending')
        self.assertFalse(fresh['requires_new_invitation'])
        self.assertEqual(len(self.bridge.agents),0)
        self.assertEqual(load_config(self.path)['agents'],[])
    def test_new_enrollment_preserves_previous_members_and_history(self):
        first=self.invite();self.request('/api/agents/join',{'claim_id':'a'*32},key=False,headers=self.claim_headers(first));agent=next(iter(self.bridge.agents))
        self.bridge.history.record(agent,'grok_bot','Fixture Agent','existing-run','done',delivery='Existing delivery')
        second=self.invite(name='Another Agent',provider='generic',request_id='request-fixture-two')
        self.request('/api/agents/join',{'claim_id':'b'*32},key=False,headers=self.claim_headers(second))
        self.assertEqual(len(self.bridge.agents),2);self.assertEqual(len(load_config(self.path)['agents']),2)
        self.assertEqual(len(self.bridge.history.list(self.bridge.agents)),1)
        with self.assertRaises(InvitationError):self.bridge.invitations.create({'name':' Fixture Agent ','provider':'generic','request_id':'request-fixture-three'})
    def test_concurrent_claims_only_grant_one_identity(self):
        invite=self.invite();headers=self.claim_headers(invite);results=[]
        def claim(char):results.append(self.request('/api/agents/join',{'claim_id':char*32},key=False,headers=headers)[0])
        threads=[threading.Thread(target=claim,args=(char,)) for char in ['a','b']]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(sorted(results),[200,409]);self.assertEqual(len(load_config(self.path)['agents']),1)

if __name__=='__main__':unittest.main()
