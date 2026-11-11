"""Real local HTTP/CLI transport tests; no model or vendor account is used."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from commands import Commands
from configuration import initialize, load_config, save_config
from dashboard import Bridge, proxy_handler
from agent_inbox import Inbox


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve();self.path=self.root/'config.json'
        initialize(self.path);config=load_config(self.path);config.update(run_id='inbox-fixture',run_dir=str(self.root))
        save_config(self.path,config);self.bridge=Bridge(config)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),proxy_handler(self.bridge));self.port=self.server.server_address[1]
        config['port']=self.port;save_config(self.path,config);self.base='http://127.0.0.1:'+str(self.port)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.children=[]
        self.agents=[];self.credentials=[]
        for index,provider in enumerate(['grok_bot','cursor']):
            invitation=self.bridge.invitations.create({'provider':provider,'name':'Fixture '+provider,'request_id':'inbox-fixture-'+str(index)})
            private=json.loads(self.bridge.invitations.paths(invitation['id'])[0].read_text())
            joined=self.bridge.invitations.claim(invitation['id'],private['token'],{'claim_id':uuid.uuid4().hex})
            self.agents.append(joined['agent_id']);self.credentials.append(Path(joined['conversation']['credential_path']))
    def tearDown(self):
        for child in self.children:
            if child.poll() is None:child.terminate()
            child.communicate(timeout=5)
        self.bridge.stop.set();self.server.shutdown();self.server.server_close();self.thread.join()
        self.bridge.commands.close();self.bridge.history.close();self.temp.cleanup()
    def call(self,path,body=None,agent=None,headers=None):
        h={'Content-Type':'application/json'}
        if agent is None:h['X-Office-Key']=self.bridge.config['topic']
        else:
            c=json.loads(self.credentials[agent].read_text());h.update({'X-Agent-Id':c['agent_id'],'Authorization':'Bearer '+c['token']})
        h.update(headers or {})
        req=urllib.request.Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,headers=h)
        try:
            with urllib.request.urlopen(req,timeout=5) as r:return r.status,json.load(r)
        except urllib.error.HTTPError as e:return e.code,json.load(e)
    def send(self,text='A bounded local fixture',agent=0):
        data={'agent_id':self.agents[agent],'request_id':str(uuid.uuid4()),'text':text}
        code,result=self.call('/api/office/commands',data);self.assertEqual(code,202);return data
    def receive(self,agent=0,**override):
        data={'action':'receive','receiver_id':str(uuid.uuid4()),'request_id':str(uuid.uuid4()),'wait_seconds':0,**override}
        return data,self.call('/api/agents/inbox',data,agent=agent)
    def event(self,delivery,action,agent=0,text='Public fixture reply',event_id=None):
        data={'action':action,'command_id':delivery['command_id'],'receipt_id':delivery['receipt_id']}
        if action!='start':data.update(event_id=event_id or str(uuid.uuid4()),text=text)
        return self.call('/api/agents/inbox',data,agent=agent)
    def cli(self,agent,*args,input=None):
        return subprocess.run([sys.executable,str(Path(__file__).parent/'adapters/inbox.py'),'--credential',str(self.credentials[agent]),*args],input=input,capture_output=True,text=True,timeout=8)
    def test_join_creates_scoped_conversation_for_each_provider_without_native_key(self):
        for agent,path in zip(self.agents,self.credentials):
            cap=self.bridge.commands.capability(agent)
            self.assertTrue(cap['can_send']);self.assertEqual(cap['mode'],'agent_inbox');self.assertFalse(cap['receiver_online'])
            self.assertFalse(cap['native_ready'])
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            guide=path.with_name('receive-guide.txt').read_text();self.assertIn('目前 session',guide)
            if agent.startswith('cursor:'):
                self.assertEqual(cap['reason'],'cursor_local_background_unavailable')
                self.assertIn('不代表入場失敗',guide)
                self.assertNotIn('connect.py',guide)
                self.assertNotIn('inbox.py wait',guide)
            else:self.assertIn('execute=true',guide)
            self.assertNotIn(json.loads(path.read_text())['token'],guide)
        self.assertEqual(len(load_config(self.path)['agents']),2)
        self.assertFalse(load_config(self.path).get('command_bindings'))

    def test_local_cursor_limit_keeps_owner_mailbox_and_disabled_dispatch_takes_precedence(self):
        agent=next(a for a in self.agents if a.startswith('cursor:'))
        cap=self.bridge.commands.capability(agent)
        self.assertTrue(cap['can_send']);self.assertFalse(cap['persistent'])
        self.assertEqual(cap['reason'],'cursor_local_background_unavailable')
        command=str(uuid.uuid4())
        self.bridge.commands.create({'agent_id':agent,'request_id':command,'text':'A local owner message'})
        row=self.bridge.commands.db.execute('SELECT adapter,state FROM commands WHERE id=?',(command,)).fetchone()
        self.assertEqual(tuple(row),('agent_inbox','pending'))
        self.bridge.commands.config.setdefault('disabled_command_agents',[]).append(agent)
        disabled=self.bridge.commands.capability(agent)
        self.assertFalse(disabled['can_send']);self.assertEqual(disabled['reason'],'dispatch_disabled')
        self.assertFalse(self.bridge.inbox.listening(agent))
    def test_actual_join_cli_returns_guide_with_same_identity_and_no_secrets(self):
        inv=self.bridge.invitations.create({'provider':'generic','name':'New generic','request_id':'generic-join-fixture'})
        p=subprocess.run([sys.executable,str(Path(__file__).parent/'adapters/join.py'),'--invite',str(self.bridge.invitations.paths(inv['id'])[0])],capture_output=True,text=True,timeout=8)
        self.assertEqual(p.returncode,0,p.stderr);result=json.loads(p.stdout)
        self.assertTrue(result['joined']);self.assertEqual(result['agent_id'],result['conversation']['agent_id'])
        self.assertTrue(Path(result['conversation']['guide_path']).exists())
        secret=json.loads(Path(result['conversation']['credential_path']).read_text())['token'];self.assertNotIn(secret,p.stdout)
    def test_real_cli_wait_two_rounds_start_fence_public_reply_and_history(self):
        for agent in (0,1):
            for turn in range(2):
                req=self.send('Round '+str(turn)+' password=secret-fixture',agent)
                self.bridge.commands.tick()
                self.assertEqual(self.bridge.commands.list(self.agents[agent])['commands'][0]['state'],'pending')
                p=self.cli(agent,'wait','--max-seconds','2');self.assertEqual(p.returncode,0,p.stderr)
                delivery=json.loads(p.stdout);self.assertEqual(delivery['command_id'],req['request_id']);self.assertFalse(delivery['execute'])
                self.assertIn('secret-fixture',delivery['text'])
                start=self.cli(agent,'start','--command',delivery['command_id']);self.assertTrue(json.loads(start.stdout)['execute'])
                again=self.cli(agent,'start','--command',delivery['command_id']);self.assertFalse(json.loads(again.stdout)['execute'])
                eid=str(uuid.uuid4())
                progress=self.cli(agent,'message','--command',delivery['command_id'],'--event-id',eid,'--text-stdin',input='Public progress')
                self.assertTrue(json.loads(progress.stdout)['accepted'])
                final=self.cli(agent,'complete','--command',delivery['command_id'],'--event-id',str(uuid.uuid4()),'--text-stdin',input='Done '+str(turn)+' api_key=hide-this')
                self.assertEqual(json.loads(final.stdout)['state'],'completed')
            messages=self.bridge.history.messages(set(self.agents),self.agents[agent]);self.assertEqual(len(messages),6)
            self.assertEqual(len([m for m in messages if m['phase']=='final']),2)
            self.assertNotIn('secret-fixture',json.dumps(messages));self.assertNotIn('hide-this',json.dumps(messages))
        self.assertEqual(self.bridge.commands.db.execute('SELECT count(*) FROM commands WHERE payload IS NOT NULL').fetchone()[0],0)
    def test_lost_receive_response_is_recovered_but_never_reassigned_after_start(self):
        first=self.send();data,(_,delivery)=self.receive();self.assertEqual(delivery['state'],'received')
        self.assertEqual(self.call('/api/agents/inbox',data,agent=0)[1]['receipt_id'],delivery['receipt_id'])
        self.assertEqual(self.receive()[1][1]['state'],'unresolved_delivery')
        self.assertTrue(self.event(delivery,'start')[1]['execute'])
        self.bridge.commands.close();self.bridge.commands=Commands(self.bridge.config,self.bridge.history)
        self.bridge.inbox=Inbox(self.bridge);self.bridge.commands.inbox=self.bridge.inbox
        self.assertFalse(self.event(delivery,'start')[1]['execute'])
        self.assertEqual(self.call('/api/agents/inbox',data,agent=0)[1]['state'],'already_started')
        self.send('Next task');self.assertEqual(self.receive()[1][1]['state'],'unresolved_delivery')
        self.assertEqual(self.event(delivery,'complete')[1]['state'],'completed')
        self.assertEqual(self.receive()[1][1]['state'],'received')
        self.assertTrue(self.bridge.commands.create(first)['duplicate'])
    def test_agent_credentials_cannot_read_others_or_owner_controls(self):
        self.send();_,(_,delivery)=self.receive()
        self.assertEqual(self.event(delivery,'start',agent=1)[0],403)
        self.assertEqual(self.receive(agent=1)[1][1]['state'],'waiting')
        self.assertEqual(self.call('/api/agents/inbox',{'action':'receive'})[0],403)
        self.assertEqual(self.call('/api/office/conversation',{'agent_id':self.agents[1]},agent=0)[0],403)
        self.assertEqual(self.call('/api/agents/inbox',{'action':'receive'},agent=0,headers={'Origin':'https://attacker.invalid'})[0],403)
        self.assertEqual(self.call('/api/office/commands',{'agent_id':'generic:unselected','request_id':str(uuid.uuid4()),'text':'no'})[0],404)
    def test_terminal_needs_start_and_duplicate_event_cannot_change_delivery(self):
        self.send();_,(_,delivery)=self.receive()
        self.assertEqual(self.event(delivery,'complete')[0],409);self.event(delivery,'start')
        event_id=str(uuid.uuid4());self.assertFalse(self.event(delivery,'complete',event_id=event_id)[1]['duplicate'])
        self.assertTrue(self.event(delivery,'complete',event_id=event_id)[1]['duplicate'])
        self.assertEqual(self.event(delivery,'complete',event_id=event_id,text='Different')[0],409)
        self.assertEqual(self.event(delivery,'message')[0],409)
    def test_offline_pending_survives_restart_and_can_be_cancelled_before_claim(self):
        req=self.send('password=erase-me')
        self.bridge.commands.close();self.bridge.commands=Commands(self.bridge.config,self.bridge.history)
        self.bridge.inbox=Inbox(self.bridge);self.bridge.commands.inbox=self.bridge.inbox
        self.assertEqual(self.bridge.commands.list(self.agents[0])['commands'][0]['state'],'pending')
        body={'agent_id':self.agents[0],'command_id':req['request_id']}
        self.assertEqual(self.call('/api/office/commands/cancel',body)[1]['command']['state'],'cancelled')
        self.assertTrue(self.call('/api/office/commands/cancel',body)[1]['duplicate'])
        self.assertEqual(self.receive()[1][1]['state'],'waiting')
        self.assertNotIn(b'erase-me',(self.root/'commands.sqlite3').read_bytes())
        self.send();_,(_,delivery)=self.receive();body['command_id']=delivery['command_id']
        self.assertEqual(self.call('/api/office/commands/cancel',body)[0],409)
    def test_concurrent_receivers_cannot_execute_one_delivery_twice(self):
        self.send();data={'action':'receive','receiver_id':str(uuid.uuid4()),'request_id':str(uuid.uuid4()),'wait_seconds':0}
        results=[];threads=[threading.Thread(target=lambda:results.append(self.call('/api/agents/inbox',data,agent=0)[1])) for _ in range(3)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(len({r['receipt_id'] for r in results}),1)
        starts=[];threads=[threading.Thread(target=lambda:starts.append(self.event(results[0],'start')[1]['execute'])) for _ in range(3)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(starts.count(True),1)
    def test_listening_requires_live_wait_and_disappears_after_disconnect(self):
        c=json.loads(self.credentials[0].read_text());sock=socket.create_connection(('127.0.0.1',self.port))
        body=json.dumps({'action':'receive','receiver_id':str(uuid.uuid4()),'request_id':str(uuid.uuid4()),'wait_seconds':10}).encode()
        headers=('POST /api/agents/inbox HTTP/1.1\r\nHost: 127.0.0.1:'+str(self.port)+'\r\nContent-Type: application/json\r\nContent-Length: '+str(len(body))+'\r\nX-Agent-Id: '+c['agent_id']+'\r\nAuthorization: Bearer '+c['token']+'\r\n\r\n').encode()
        sock.sendall(headers+body)
        end=time.monotonic()+2
        while not self.bridge.inbox.listening(self.agents[0]) and time.monotonic()<end:time.sleep(.01)
        self.assertTrue(self.bridge.commands.capability(self.agents[0])['receiver_online']);sock.close()
        end=time.monotonic()+2
        while self.bridge.inbox.listening(self.agents[0]) and time.monotonic()<end:time.sleep(.01)
        self.assertFalse(self.bridge.commands.capability(self.agents[0])['receiver_online'])
    def test_saved_native_routes_take_priority_without_a_second_inbox(self):
        from test_platforms import CursorFixture, SID
        self.bridge.connections.transport=CursorFixture()
        result=self.bridge.connections.connect_cursor({'name':'Native fixture','session_id':SID,'api_key':'fixture-key'})
        guide=self.bridge.inbox.ensure(result['agent_id']);self.assertEqual(guide['capability']['mode'],'cursor_cloud')
        data={'action':'receive','receiver_id':str(uuid.uuid4()),'request_id':str(uuid.uuid4()),'wait_seconds':0}
        self.assertEqual(self.bridge.inbox.receive(result['agent_id'],data)['state'],'native_route')

    def test_explicit_disable_survives_restart_and_blocks_fallback_until_enabled(self):
        req=self.send('Do not consume while disabled')
        cli=[sys.executable,str(Path(__file__).parent/'office.py'),'--config',str(self.path)]
        result=subprocess.run(cli+['disable-commands','--agent',self.agents[0]],capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        self.bridge.config.update(load_config(self.path))
        self.bridge.commands.close();self.bridge.commands=Commands(self.bridge.config,self.bridge.history)
        self.bridge.inbox=Inbox(self.bridge);self.bridge.commands.inbox=self.bridge.inbox
        self.assertFalse(self.bridge.commands.capability(self.agents[0])['can_send'])
        self.assertEqual(self.receive()[1][0],503)
        self.assertEqual(self.call('/api/office/commands',{'agent_id':self.agents[0],'request_id':str(uuid.uuid4()),'text':'Blocked'})[0],503)
        self.assertTrue(self.bridge.commands.capability(self.agents[1])['can_send'])
        result=subprocess.run(cli+['enable-commands','--agent',self.agents[0],'--adapter','agent_inbox'],capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        self.bridge.config.update(load_config(self.path))
        _,(_,delivery)=self.receive();self.assertEqual(delivery['command_id'],req['request_id'])
        self.assertTrue(self.event(delivery,'start')[1]['execute'])
        self.bridge.config['disabled_command_agents']=[self.agents[0]]
        self.assertEqual(self.event(delivery,'complete')[1]['state'],'completed')

    def test_disable_blocks_unstarted_delivery_and_rejects_invalid_targets(self):
        self.send();_,(_,delivery)=self.receive()
        self.bridge.config['disabled_command_agents']=[self.agents[0]]
        self.assertEqual(self.event(delivery,'start')[0],503)
        self.bridge.config['disabled_command_agents']=[]
        self.assertTrue(self.event(delivery,'start')[1]['execute'])
        for invalid in (['generic:unknown'],[self.agents[0],self.agents[0]],{},[None]):
            config=load_config(self.path);config['disabled_command_agents']=invalid;save_config(self.path,config)
            with self.assertRaises(ValueError):load_config(self.path)
            config['disabled_command_agents']=[];save_config(self.path,config)

    def test_restart_uses_latest_inbox_state_without_claiming_unfinished_work_is_idle(self):
        deliveries=[]
        for index,ident in enumerate(self.agents):
            agent=self.bridge.definitions[ident];rid='prior-join-'+str(index)
            for kind in ('turn_started','turn_completed'):
                self.bridge.accept_agent_event(agent,{'source_id':agent['source_id'],'run_id':rid,'event_id':rid+kind,'type':kind,'text':'Prior enrollment'})
            self.send(agent=index);_,(_,delivery)=self.receive(agent=index);self.event(delivery,'start',agent=index);deliveries.append(delivery)
        self.event(deliveries[1],'complete',agent=1)
        restored=Bridge(load_config(self.path))
        try:
            active=restored.sources[self.bridge.definitions[self.agents[0]]['name']]
            done=restored.sources[self.bridge.definitions[self.agents[1]]['name']]
            self.assertEqual(active['run_id'],deliveries[0]['command_id']);self.assertEqual(active['status'],'waiting_user')
            self.assertIn('未確認完成',active['step'])
            self.assertEqual(done['run_id'],deliveries[1]['command_id']);self.assertEqual(done['status'],'done')
        finally:restored.commands.close();restored.history.close()


if __name__=='__main__':unittest.main()
