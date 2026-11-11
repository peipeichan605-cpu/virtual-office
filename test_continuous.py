"""Continuous native enrollment; real loopback HTTP, fixture runtime, no models."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from configuration import initialize,load_config,save_config
from connections import ConnectionError as OfficeConnectionError
from dashboard import Bridge,proxy_handler
from history import timestamp

SID='00000000-0000-4000-8000-000000000071'

class NativeFixture:
    mode='background_queue'
    reason=None
    def __init__(self):self.calls=[]
    def available(self):return True
    def submit(self,sid,text):
        self.calls.append((sid,text))
        return {'native_message_id':str(uuid.uuid4())}

class ContinuousTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.home=self.root/'codex-home';self.home.mkdir()
        self.environment=patch.dict(os.environ,{'CODEX_HOME':str(self.home)});self.environment.start()
        self.rollout=self.root/'selected-rollout.jsonl'
        self.rollout.write_text(json.dumps({'type':'session_meta','payload':{'id':SID}})+'\n')
        with sqlite3.connect(self.home/'state_5.sqlite') as db:
            db.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, archived INTEGER)')
            db.execute('INSERT INTO threads VALUES (?,?,?,0)',(SID,str(self.rollout),str(self.root)))
        self.path=self.root/'config.json';initialize(self.path)
        config=load_config(self.path);config.update(run_id='fixture',run_dir=str(self.root/'run'))
        save_config(self.path,config);self.bridge=Bridge(config)
        self.adapter=NativeFixture();self.bridge.commands.adapters['codex_app']=self.adapter
        self.server=ThreadingHTTPServer(('127.0.0.1',0),proxy_handler(self.bridge))
        config['port']=self.server.server_address[1];save_config(self.path,config)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base='http://127.0.0.1:'+str(config['port'])
        invitation=self.bridge.invitations.create({'provider':'codex','name':'Continuous fixture','request_id':'continuous-fixture'})
        self.invite,self.guide,self.credential=self.bridge.invitations.paths(invitation['id'])

    def tearDown(self):
        self.bridge.stop.set();self.server.shutdown();self.server.server_close();self.thread.join()
        self.bridge.commands.close();self.bridge.history.close();self.environment.stop();self.tmp.cleanup()

    def join(self):
        env={**os.environ,'CODEX_THREAD_ID':SID}
        result=subprocess.run([sys.executable,str(Path(__file__).parent/'adapters/join.py'),'--invite',str(self.invite)],cwd=self.root,env=env,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def post(self,data,credential=None,identity=None):
        c=json.loads((credential or self.credential).read_text())
        headers={'Content-Type':'application/json','X-Agent-Id':identity or c['agent_id'],'Authorization':'Bearer '+c['token']}
        request=urllib.request.Request(self.base+'/api/agents/connect',data=json.dumps(data).encode(),headers=headers)
        try:
            with urllib.request.urlopen(request,timeout=5) as response:return response.status,json.load(response)
        except urllib.error.HTTPError as error:return error.code,json.load(error)

    def connect_payload(self,**changes):return {'adapter':'codex_app','session_id':SID,'workspace_path':str(self.root),**changes}

    def test_join_automatically_binds_current_session_without_a_waiter_or_model_call(self):
        result=self.join();agent=result['agent_id'];cap=result['conversation']['capability']
        self.assertTrue(result['joined']);self.assertTrue(all(e['accepted'] for e in result['events']))
        self.assertTrue(cap['persistent']);self.assertTrue(cap['same_session']);self.assertFalse(cap['requires_active_turn'])
        self.assertEqual(self.adapter.calls,[]);self.assertFalse(self.bridge.inbox.listening(agent))
        guide=Path(result['conversation']['guide_path']).read_text();self.assertNotIn('wait --max-seconds',guide)
        config=load_config(self.path);self.assertEqual(len(config['agents']),1)
        self.assertEqual(config['command_bindings'][agent]['session_id'],SID)
        self.assertEqual(self.bridge.agents[agent]['native_url'],'codex://threads/'+SID)
        again=self.join();self.assertTrue(all(e['duplicate'] for e in again['events']))
        self.assertTrue(again['native_connection']['connected'])
        self.assertTrue(again['conversation']['capability']['persistent'])
        self.assertEqual(len(load_config(self.path)['agents']),1)

    def test_native_delivery_and_history_stay_on_invited_identity_across_restart(self):
        result=self.join();agent=result['agent_id'];self.bridge.load_paths()
        for index in range(2):
            command=str(uuid.uuid4());body='Fixture message '+str(index)
            self.bridge.commands.create({'agent_id':agent,'request_id':command,'text':body});self.bridge.commands.tick()
            sid,text=self.adapter.calls[-1];self.assertEqual(sid,SID)
            rid='native-turn-'+str(index);at=timestamp()
            records=[{'type':'event_msg','timestamp':at,'payload':{'type':'task_started','turn_id':rid}},
                {'type':'response_item','timestamp':at,'payload':{'type':'message','role':'user','content':[{'type':'input_text','text':text}]}},
                {'type':'response_item','timestamp':at,'payload':{'type':'message','role':'assistant','phase':'final_answer','content':[{'type':'output_text','text':'Fixture reply '+str(index)}]}},
                {'type':'event_msg','timestamp':at,'payload':{'type':'task_complete','turn_id':rid,'last_agent_message':'Fixture reply '+str(index)}}]
            with self.rollout.open('a') as out:
                for record in records:out.write(json.dumps(record)+'\n')
            self.bridge.tails[SID].read()
            row=self.bridge.commands.db.execute('SELECT state FROM commands WHERE id=?',(command,)).fetchone()
            self.assertEqual(row['state'],'completed')
        rows=self.bridge.history.messages({agent},agent)
        self.assertTrue(any(r['text']=='Fixture reply 1' for r in rows))
        self.assertEqual(self.bridge.history.messages({'codex:'+SID},'codex:'+SID),[])
        restored=Bridge(load_config(self.path))
        try:
            restored.commands.adapters['codex_app']=NativeFixture()
            # Advancing well past one hour does not expire a native binding.
            with patch('time.time',return_value=4102444800):
                self.assertTrue(restored.commands.capability(agent)['persistent'])
            restored.commands.tick();self.assertEqual(restored.commands.adapters['codex_app'].calls,[])
            self.assertEqual(restored.inbox.receive(agent,{'action':'receive','receiver_id':str(uuid.uuid4()),'request_id':str(uuid.uuid4()),'wait_seconds':0})['state'],'native_route')
        finally:restored.commands.close();restored.history.close()

    def test_rejoin_reports_saved_route_unavailable_then_ready_without_rebinding(self):
        first=self.join();agent=first['agent_id']
        expected={agent:{'adapter':'codex_app','session_id':SID}}
        self.assertEqual(load_config(self.path)['command_bindings'],expected)
        self.adapter.available=lambda:False
        with patch.object(self.bridge.connections,'connect_codex',side_effect=AssertionError('Saved native route must not be rebound')) as attach:
            unavailable=self.join();native=unavailable['native_connection']
            self.assertTrue(unavailable['joined'])
            self.assertTrue(all(e['accepted'] and e['duplicate'] for e in unavailable['events']))
            self.assertEqual(native['receiver_status'],'configured_unavailable')
            self.assertFalse(native['connected']);self.assertTrue(native['persistent'])
            self.assertFalse(native['execution_verified'])
            self.assertFalse(unavailable['conversation']['capability']['native_ready'])
            self.assertEqual(load_config(self.path)['command_bindings'],expected)
            self.assertEqual(self.adapter.calls,[])
            self.assertFalse(self.bridge.inbox.listening(agent))
            self.assertNotIn('wait --max-seconds',Path(unavailable['conversation']['guide_path']).read_text())

            self.adapter.available=lambda:True
            restored=self.join();native=restored['native_connection']
            self.assertEqual(native['receiver_status'],'ready')
            self.assertTrue(native['connected']);self.assertTrue(native['persistent'])
            self.assertFalse(native['execution_verified'])
            self.assertTrue(restored['conversation']['capability']['native_ready'])
            self.assertEqual(load_config(self.path)['command_bindings'],expected)
            self.assertEqual(len(load_config(self.path)['agents']),1)
            self.assertEqual(self.adapter.calls,[])
            self.assertFalse(self.bridge.inbox.listening(agent))
            attach.assert_not_called()

    def test_cursor_join_in_mixed_environment_does_not_attach_codex(self):
        invitation=self.bridge.invitations.create({'provider':'cursor','name':'Selected local fixture','request_id':'mixed-provider-fixture'})
        invite,_,_=self.bridge.invitations.paths(invitation['id'])
        env={**os.environ,'CODEX_THREAD_ID':SID}
        with patch.object(self.bridge.connections,'connect_codex',side_effect=AssertionError('Cursor must not attach a Codex session')) as attach:
            process=subprocess.run([sys.executable,str(Path(__file__).parent/'adapters/join.py'),'--invite',str(invite)],
                cwd=self.root,env=env,capture_output=True,text=True,timeout=10)
            self.assertEqual(process.returncode,0,process.stderr)
            result=json.loads(process.stdout);agent=result['agent_id'];native=result['native_connection']
            self.assertTrue(result['joined']);self.assertTrue(agent.startswith('cursor:'))
            self.assertEqual(len(result['events']),2)
            self.assertTrue(all(event['accepted'] for event in result['events']))
            self.assertEqual(native['receiver_status'],'unsupported')
            self.assertEqual(native['reason'],'cursor_local_background_unavailable')
            self.assertFalse(native['connected']);self.assertFalse(native['persistent'])
            self.assertFalse(native['execution_verified'])
            self.assertFalse(result['conversation']['capability']['persistent'])
            self.assertEqual(load_config(self.path).get('command_bindings',{}),{})
            self.assertEqual(self.adapter.calls,[])
            self.assertFalse(self.bridge.inbox.listening(agent))
            attach.assert_not_called()

    def test_credential_revoked_before_native_attach_requires_new_invitation(self):
        with patch.object(self.bridge.connections,'connect_agent',side_effect=OfficeConnectionError('agent_credential_revoked',401)) as attach:
            result=self.join()
        native=result['native_connection'];agent=result['agent_id']
        self.assertTrue(result['joined'])
        self.assertEqual(len(result['events']),2)
        self.assertTrue(all(event['accepted'] for event in result['events']))
        self.assertTrue(native['requires_rejoin'])
        self.assertEqual(native['reason'],'agent_credential_revoked')
        self.assertEqual(native['receiver_status'],'rejoin_required')
        self.assertEqual(result['receiver_status'],'rejoin_required')
        self.assertFalse(native['connected']);self.assertFalse(native['persistent'])
        self.assertFalse(native['native_ready']);self.assertFalse(native['execution_verified'])
        self.assertIn('新邀請',result['next_step'])
        self.assertNotIn('conversation.guide_path',result['next_step'])
        self.assertEqual(load_config(self.path).get('command_bindings',{}),{})
        self.assertEqual(self.adapter.calls,[])
        self.assertFalse(self.bridge.inbox.listening(agent))
        attach.assert_called_once()

    def test_scoped_credentials_cannot_connect_other_agents_or_other_runtimes(self):
        result=self.join()
        self.assertEqual(self.post(self.connect_payload(),identity='codex:other')[0],403)
        self.assertEqual(self.post({**self.connect_payload(),'agent_id':'codex:other'})[0],409)
        self.assertEqual(self.post({'adapter':'arbitrary_process','command':['/bin/sh']})[0],409)
        self.assertEqual(self.post(self.connect_payload(session_id=str(uuid.uuid4())))[0],409)
        self.assertTrue(self.post(self.connect_payload(),credential=Path(result['conversation']['credential_path']))[1]['connected'])

    def test_wrong_workspace_missing_session_and_archived_session_do_not_bind(self):
        for mutation in ('wrong_workspace','missing_session','archived'):
            with self.subTest(mutation=mutation):
                if mutation=='archived':
                    with sqlite3.connect(self.home/'state_5.sqlite') as db:db.execute('UPDATE threads SET archived=1')
                payload=self.connect_payload(workspace_path='/unrelated') if mutation=='wrong_workspace' else self.connect_payload(session_id=str(uuid.uuid4())) if mutation=='missing_session' else self.connect_payload()
                private=json.loads(self.invite.read_text())
                self.bridge.invitations.claim(private['invitation_id'],private['token'],{'claim_id':'a'*32})
                self.assertEqual(self.post(payload)[0],409)
                self.assertEqual(load_config(self.path).get('command_bindings',{}),{})

    def test_existing_inbox_work_is_not_lost_during_upgrade(self):
        private=json.loads(self.invite.read_text());result=self.bridge.invitations.claim(private['invitation_id'],private['token'],{'claim_id':'a'*32})
        agent=result['agent_id'];command=str(uuid.uuid4())
        self.bridge.commands.create({'agent_id':agent,'request_id':command,'text':'Keep this pending message'})
        code,result=self.post(self.connect_payload());self.assertEqual(code,200)
        row=self.bridge.commands.db.execute('SELECT adapter,state,payload FROM commands WHERE id=?',(command,)).fetchone()
        self.assertEqual(tuple(row),('codex_app','pending','Keep this pending message'))
        self.bridge.commands.tick();self.bridge.commands.tick();self.assertEqual(len(self.adapter.calls),1)
        self.assertEqual(self.adapter.calls[0][1],'[OFFICE:'+command+']\nKeep this pending message')

    def test_delivered_inbox_work_blocks_route_switch(self):
        private=json.loads(self.invite.read_text());result=self.bridge.invitations.claim(private['invitation_id'],private['token'],{'claim_id':'a'*32})
        agent=result['agent_id'];command=str(uuid.uuid4())
        self.bridge.commands.create({'agent_id':agent,'request_id':command,'text':'Already received'})
        self.bridge.inbox.receive(agent,{'action':'receive','receiver_id':str(uuid.uuid4()),'request_id':str(uuid.uuid4()),'wait_seconds':0})
        code,error=self.post(self.connect_payload());self.assertEqual(code,409);self.assertEqual(error['error'],'connection_has_active_commands')
        self.assertEqual(self.bridge.commands.db.execute('SELECT adapter,state FROM commands WHERE id=?',(command,)).fetchone()['adapter'],'agent_inbox')

    def test_same_native_session_cannot_be_selected_under_two_aliases(self):
        first=self.join();inv=self.bridge.invitations.create({'provider':'codex','name':'Second fixture','request_id':'second-fixture'})
        invite,_,credential=self.bridge.invitations.paths(inv['id']);private=json.loads(invite.read_text())
        self.bridge.invitations.claim(inv['id'],private['token'],{'claim_id':'b'*32})
        code,error=self.post(self.connect_payload(),credential=credential)
        self.assertEqual(code,409);self.assertEqual(error['error'],'native_session_already_selected')
        self.assertEqual(len(load_config(self.path)['command_bindings']),1)

    def test_unavailable_runtime_keeps_pending_work_and_dispatches_once_on_return(self):
        result=self.join();agent=result['agent_id'];self.adapter.available=lambda:False
        cap=self.bridge.commands.capability(agent)
        self.assertTrue(cap['can_send']);self.assertFalse(cap['native_ready'])
        command=str(uuid.uuid4())
        self.bridge.commands.create({'agent_id':agent,'request_id':command,'text':'Wait for the native runtime'})
        self.bridge.commands.tick();self.assertEqual(self.adapter.calls,[])
        self.assertEqual(self.bridge.commands.list(agent)['commands'][0]['state'],'pending')
        self.adapter.available=lambda:True
        self.bridge.commands.tick();self.bridge.commands.tick()
        self.assertEqual(len(self.adapter.calls),1)
        self.assertEqual(self.bridge.commands.list(agent)['commands'][0]['state'],'queued')

    def test_grok_can_self_attach_only_after_first_report_and_without_owner_key(self):
        inv=self.bridge.invitations.create({'provider':'grok_bot','name':'Grok persistent fixture','request_id':'grok-native-fixture'})
        invite,_,credential=self.bridge.invitations.paths(inv['id']);private=json.loads(invite.read_text())
        joined=self.bridge.invitations.claim(inv['id'],private['token'],{'claim_id':'c'*32})
        payload={'adapter':'grok_webhook','url':'https://api.cursor.com/fixture/webhook','key':'fixture-webhook-key'}
        self.assertEqual(self.post(payload,credential=credential)[1]['error'],'grok_return_route_required')
        agent=self.bridge.definitions[joined['agent_id']]
        for kind in ('turn_started','turn_completed'):
            self.bridge.accept_agent_event(agent,{'source_id':agent['source_id'],'event_id':'join:'+kind,'type':kind,'run_id':'grok-join','text':'Local report'})
        code,result=self.post(payload,credential=credential)
        self.assertEqual(code,200);self.assertTrue(result['conversation']['capability']['persistent'])
        self.assertNotIn(payload['key'],json.dumps(result));self.assertNotIn(payload['url'],json.dumps(result))
        self.assertEqual(self.bridge.commands.binding(agent['id'])['adapter'],'grok_webhook')
        self.assertEqual(self.post({**payload,'url':'https://api.cursor.com/another/webhook'},credential=credential)[1]['error'],'native_binding_conflict')
        self.assertEqual(self.post(payload,credential=credential)[0],200)

    def test_restart_reconciles_binding_saved_before_pending_queue_migration(self):
        private=json.loads(self.invite.read_text());result=self.bridge.invitations.claim(private['invitation_id'],private['token'],{'claim_id':'a'*32})
        agent=result['agent_id'];command=str(uuid.uuid4())
        self.bridge.commands.create({'agent_id':agent,'request_id':command,'text':'Survive the binding cutover'})
        config=load_config(self.path);config['command_bindings']={agent:{'adapter':'codex_app','session_id':SID}};save_config(self.path,config)
        restored=Bridge(load_config(self.path))
        try:
            adapter=NativeFixture();restored.commands.adapters['codex_app']=adapter
            row=restored.commands.db.execute('SELECT adapter,state,payload FROM commands WHERE id=?',(command,)).fetchone()
            self.assertEqual(tuple(row),('codex_app','pending','Survive the binding cutover'))
            restored.commands.tick();restored.commands.tick();self.assertEqual(len(adapter.calls),1)
        finally:restored.commands.close();restored.history.close()

if __name__=='__main__':unittest.main()
