import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from dashboard import Bridge
from history import HistoryStore
from workspaces import Workspaces,workspace_path
from invitations import Invitations
from event_ingest import EventConflict
import test_invitations
sys.path.insert(0,str(Path(__file__).parent/'adapters'))
from cursor_hook import translate

class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.history=HistoryStore(self.root/'history.sqlite3');self.ws=Workspaces(self.history)
    def tearDown(self):self.history.close();self.temp.cleanup()
    def test_canonical_identity_and_same_name_collision(self):
        first=self.root/'one'/'project';first.mkdir(parents=True);other=self.root/'two'/'project';other.mkdir(parents=True)
        alias=self.root/'alias';alias.symlink_to(first,target_is_directory=True)
        one=self.ws.create(str(first));same=self.ws.create(str(alias/'..'/'project'));two=self.ws.create(str(other))
        self.assertEqual(one['id'],same['id']);self.assertNotEqual(one['id'],two['id'])
        rows=self.ws.catalog();self.assertNotEqual(rows[0]['name'],rows[1]['name'])
        for path in ['relative','',None,'/tmp/bad\npath']:
            with self.assertRaises(ValueError):workspace_path(path)
    def test_provider_neutral_manual_override_restart_and_late_observation(self):
        one=self.ws.create(str(self.root/'one'));two=self.ws.create(str(self.root/'two'))
        for agent in ['codex:a','crouton:b','grok_bot:c']:self.ws.observe(agent,one['path'],'fixture',20)
        self.assertEqual({self.ws.for_agent(a)['id'] for a in ['codex:a','crouton:b','grok_bot:c']},{one['id']})
        self.ws.assign('codex:a',two['id']);self.ws.observe('codex:a',one['path'],'fixture',30)
        self.assertEqual(Workspaces(self.history).for_agent('codex:a')['id'],two['id'])
        self.ws.assign('codex:a',mode='auto');self.assertEqual(self.ws.for_agent('codex:a')['id'],one['id'])
        self.ws.observe('codex:a',two['path'],'old',10);self.assertEqual(self.ws.for_agent('codex:a')['id'],one['id'])
        self.ws.observe('codex:a',None,'unknown',40);self.assertEqual(self.ws.for_agent('codex:a')['id'],'workspace-unassigned')
    def test_native_codex_only_reads_selected_workspace_and_tracks_moves(self):
        path=self.root/'source.sqlite';con=sqlite3.connect(path)
        con.execute('CREATE TABLE threads(id TEXT,cwd TEXT)');con.executemany('INSERT INTO threads VALUES (?,?)',[('chosen',str(self.root/'one')),('private',str(self.root/'private'))]);con.commit()
        b=Bridge.__new__(Bridge);b.names={'chosen':'Selected'};b.config={'codex_db':str(path)};b.workspaces=self.ws
        b.codex_targets={'chosen':{'id':'codex:chosen'}}
        b.refresh_codex_workspaces();self.assertEqual(len(self.ws.catalog()),1);self.assertEqual(self.ws.catalog()[0]['name'],'one')
        con.execute('UPDATE threads SET cwd=? WHERE id=?',(str(self.root/'two'),'chosen'));con.commit();con.close()
        b.refresh_codex_workspaces();self.assertEqual(self.ws.for_agent('codex:chosen')['name'],'two')
    def test_crouton_bound_request_and_known_run_location_only(self):
        project=self.root/'project';folder=project/'work'/'run';folder.mkdir(parents=True)
        run='crouton-01-11111111-1111-4111-8111-111111111111'
        (folder/'status.json').write_text(json.dumps({'runId':run,'status':'completed','finishedAt':'2026-01-01T00:00:00Z'}))
        b=Bridge.__new__(Bridge);b.crouton=SimpleNamespace(allowed_roots=[self.root]);b.crouton_names={'crouton-01':'Selected'};b.history=self.history;b.history_seen={};b.workspaces=self.ws
        b.capture_crouton('crouton-01',folder,run);self.assertEqual(self.ws.for_agent('crouton:crouton-01')['id'],'workspace-unassigned')
        known=self.ws.create(str(project));b.history_seen={};b.capture_crouton('crouton-01',folder,run)
        self.assertEqual(self.ws.for_agent('crouton:crouton-01')['id'],known['id'])
        (folder/'request.json').write_text(json.dumps({'run_id':run,'job':{'agent_id':'crouton-01','workspace':str(self.root/'explicit')}}))
        b.history_seen={};b.capture_crouton('crouton-01',folder,run);self.assertEqual(self.ws.for_agent('crouton:crouton-01')['name'],'explicit')
        self.history.record('crouton:crouton-01','crouton','Selected',run,'done',delivery='Existing delivered work')
        with self.history.lock,self.history.db:self.history.db.execute('DELETE FROM agent_workspaces')
        b.config={};b.crouton_ids=['crouton-01'];b.agents={'crouton:crouton-01':{}};b.health={};b.history_seen={}
        b.backfill_known_history();self.assertEqual(self.ws.for_agent('crouton:crouton-01')['name'],'explicit')
    def test_cursor_uses_single_workspace_only(self):
        base={'conversation_id':'chosen','generation_id':'one','hook_event_name':'beforeSubmitPrompt','prompt':'Build'}
        one=translate({**base,'workspace_roots':[str(self.root)]},'chosen');self.assertEqual(one['workspace_path'],str(self.root))
        for roots in [[],['/first','/second'],['relative'],None]:
            self.assertNotIn('workspace_path',translate({**base,'workspace_roots':roots},'chosen'))

class WorkspaceApiTests(unittest.TestCase):
    setUp=test_invitations.InvitationTests.setUp
    tearDown=test_invitations.InvitationTests.tearDown
    request=test_invitations.InvitationTests.request
    invite=test_invitations.InvitationTests.invite
    claim_headers=test_invitations.InvitationTests.claim_headers
    def test_old_invitation_table_migrates_without_losing_pending_claim(self):
        invite=self.invite()
        with self.bridge.history.lock,self.bridge.history.db:
            self.bridge.history.db.execute('ALTER TABLE invitations RENAME TO invitations_new')
            self.bridge.history.db.execute('CREATE TABLE invitations AS SELECT id,request_id,name,provider,source_id,token_hash,state,created,expires,claim_id,credential_hash FROM invitations_new')
            self.bridge.history.db.execute('DROP TABLE invitations_new')
        restored=Invitations(self.bridge.config,self.bridge.history,self.bridge.activate_push_agent,self.bridge.workspaces)
        self.assertEqual(restored.get(invite['id'])['state'],'pending')
        self.assertIsNone(restored.get(invite['id'])['workspace_id'])
    def test_duplicate_and_rejected_events_cannot_move_workspace(self):
        invite=self.invite();_,joined=self.request('/api/agents/join',{'claim_id':'a'*32},key=False,headers=self.claim_headers(invite))
        agent=self.bridge.definitions[joined['agent_id']]
        event={'type':'turn_started','event_id':'start-one','run_id':'one','source_id':agent['source_id'],'workspace_path':str(self.root/'one')}
        self.bridge.accept_agent_event(agent,event)
        self.bridge.accept_agent_event(agent,{**event,'event_id':'start-two','run_id':'two','workspace_path':str(self.root/'two')})
        self.assertTrue(self.bridge.accept_agent_event(agent,event)['duplicate'])
        with self.assertRaises(EventConflict):self.bridge.accept_agent_event(agent,{**event,'event_id':'late-old-run','workspace_path':str(self.root/'bad')})
        with self.assertRaises(ValueError):self.bridge.accept_agent_event(agent,{**event,'event_id':'invalid','run_id':'invalid','workspace_path':'relative'})
        self.assertEqual(self.bridge.workspaces.for_agent(agent['id'])['name'],'two')
        self.assertEqual(len(self.bridge.history.list(self.bridge.agents)),2)
    def test_owner_auth_scoped_assignment_and_history_persist(self):
        self.assertEqual(self.request('/api/office/workspaces',key=False)[0],403)
        self.assertEqual(self.request('/api/office/workspaces',{'path':str(self.root)},key=False)[0],403)
        code,workspace=self.request('/api/office/workspaces',{'path':str(self.root),'name':'Team project'});self.assertEqual(code,200)
        invite=self.invite();headers=self.claim_headers(invite)
        _,joined=self.request('/api/agents/join',{'claim_id':'a'*32,'workspace_path':str(self.root)},key=False,headers=headers)
        agent=joined['agent_id'];self.bridge.history.record(agent,'grok_bot','Fixture Agent','saved','done',delivery='Keep this delivery')
        body={'agent_id':agent,'mode':'manual','workspace_id':workspace['id']}
        self.assertEqual(self.request('/api/office/workspaces/assign',body,key=False,headers=headers)[0],403)
        self.assertEqual(self.request('/api/office/workspaces/assign',{**body,'agent_id':'grok_bot:unselected'})[0],404)
        self.assertEqual(self.request('/api/office/workspaces/assign',body)[0],200)
        _,data=self.request('/api/office/work');self.assertEqual(data['agents'][0]['workspace']['id'],workspace['id'])
        self.assertNotIn(str(self.root),json.dumps(data))
        self.assertEqual(self.bridge.history.list(self.bridge.agents)[0]['delivery_preview'],'Keep this delivery')
    def test_invitation_workspace_is_pinned_and_replay_cannot_undo_owner_move(self):
        one=self.bridge.workspaces.create(str(self.root/'one'));two=self.bridge.workspaces.create(str(self.root/'two'))
        body={'name':'Pinned','provider':'generic','request_id':'fixture-pinned-one','workspace_id':one['id']}
        code,invite=self.request('/api/office/invitations',body);self.assertEqual(code,200)
        self.assertEqual(self.request('/api/office/invitations',{**body,'workspace_id':two['id']})[0],409)
        headers=self.claim_headers(invite);claim={'claim_id':'a'*32,'workspace_path':str(self.root/'reported')}
        code,joined=self.request('/api/agents/join',claim,key=False,headers=headers);self.assertEqual(code,200)
        agent=joined['agent_id'];self.assertEqual(self.bridge.workspaces.for_agent(agent)['id'],one['id'])
        self.bridge.workspaces.assign(agent,two['id']);self.request('/api/agents/join',claim,key=False,headers=headers)
        self.assertEqual(self.bridge.workspaces.for_agent(agent)['id'],two['id'])
    def test_real_join_and_emitter_report_cwd_and_preserve_replay(self):
        project=self.root/'current-project';project.mkdir();other=self.root/'another-project';other.mkdir()
        invite=self.invite();path=self.bridge.invitations.paths(invite['id'])[0]
        script=Path(__file__).parent.resolve()/'adapters'
        command=[sys.executable,str(script/'join.py'),'--invite',str(path)]
        first=subprocess.run(command,cwd=project,capture_output=True,text=True,timeout=15);self.assertEqual(first.returncode,0,first.stderr)
        agent=json.loads(first.stdout)['agent_id'];self.assertEqual(self.bridge.workspaces.for_agent(agent)['name'],'current-project')
        repeat=subprocess.run(command,cwd=other,capture_output=True,text=True,timeout=15);self.assertEqual(repeat.returncode,0,repeat.stderr)
        self.assertEqual(self.bridge.workspaces.for_agent(agent)['name'],'current-project')
        credential=self.bridge.invitations.paths(invite['id'])[2]
        result=subprocess.run([sys.executable,str(script/'emit.py'),'--credential',str(credential),'turn_started','--run','new-work'],cwd=other,capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(self.bridge.workspaces.for_agent(agent)['name'],'another-project')
        self.assertEqual(len(self.bridge.history.list(self.bridge.agents)),2)

if __name__=='__main__':unittest.main()
