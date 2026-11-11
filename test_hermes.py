"""Protocol/queue integration tests; the fixture is explicitly not a live model."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import uuid
from commands import Commands, CommandError
from configuration import initialize, load_config, save_config
from hermes_acp import AcpClient, AcpError, connect_session
from history import HistoryStore

SESSION = '00000000-0000-4000-8000-000000000002'
AGENT = 'hermes:' + SESSION
FAKE = r'''
import json,os,sys,time
from pathlib import Path
SID='00000000-0000-4000-8000-000000000002'
state=Path(os.environ['HERMES_HOME'])/'fixture-session.json'
def emit(v): print(json.dumps(dict(jsonrpc='2.0',**v)),flush=True)
def update(kind,**fields):emit({'method':'session/update','params':{'sessionId':SID,'update':dict(sessionUpdate=kind,**fields)}})
for line in sys.stdin:
 m=json.loads(line);method=m.get('method');p=m.get('params',{});result={}
 if method=='initialize':result={'protocolVersion':1,'agentInfo':{'name':'hermes-agent-fixture'},'agentCapabilities':{'loadSession':True}}
 elif method=='session/new':state.write_text('[]');result={'sessionId':SID}
 elif method=='session/load':
  if not state.exists():emit({'id':m['id'],'error':{'code':-1,'message':'missing'}});continue
  update('agent_message_chunk',content={'type':'text','text':'OLD_REPLAY_MUST_NOT_BE_NEW'})
 elif method=='session/prompt':
  text=p['prompt'][0]['text'];past=json.loads(state.read_text());past.append(text);state.write_text(json.dumps(past))
  if 'crash-now' in text:sys.exit(2)
  update('agent_thought_chunk',content={'type':'text','text':'PRIVATE_REASONING_MUST_NOT_SAVE'})
  emit({'method':'session/update','params':{'sessionId':'other-session','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'WRONG_SESSION'}}}})
  if 'approval' in text:
   emit({'id':'permission-1','method':'session/request_permission','params':{'sessionId':SID,'toolCall':{'title':'Approved fixture operation password=hidden-value'},'options':[{'optionId':'yes','kind':'allow_once','name':'Allow once'},{'optionId':'no','kind':'reject_once','name':'Deny'}]}})
   decision=json.loads(sys.stdin.readline())
   if decision.get('result',{}).get('outcome',{}).get('optionId')!='yes':
    update('agent_message_chunk',content={'type':'text','text':'Operation declined.'});emit({'id':m['id'],'result':{'stopReason':'end_turn'}});continue
  update('tool_call',toolCallId='tool-1',status='in_progress',rawInput={'secret':'RAW_TOOL_MUST_NOT_SAVE'})
  update('tool_call_update',toolCallId='tool-1',status='completed',rawOutput='RAW_RESULT_MUST_NOT_SAVE')
  if 'empty-result' not in text:
   update('agent_message_chunk',content={'type':'text','text':'turn '+str(len(past))+': '})
   time.sleep(.05)
   update('agent_message_chunk',content={'type':'text','text':'done password=hidden-value'})
  result={'stopReason':'end_turn'}
 emit({'id':m['id'],'result':result})
'''


class HermesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        script=self.root/'fake.py';script.write_text(FAKE)
        self.settings={'command':[sys.executable,str(script)],'home':str(self.root),'cwd':str(self.root),'timeout_seconds':30}
        self.assertEqual(connect_session(self.settings,self.root/'connect'),SESSION)
        self.history=HistoryStore(self.root/'history.sqlite3',secrets=('office-private-key',))
        self.config={'data_dir':str(self.root),'topic':'office-private-key',
            'agents':[{'id':AGENT,'provider':'hermes','driver':'hermes','source_id':SESSION,'name':'Hermes'}],
            'command_bindings':{AGENT:{'adapter':'hermes_acp','session_id':SESSION}},
            'command_adapters':{'hermes_acp':{'sessions':{SESSION:self.settings}}}}
        self.commands=Commands(self.config,self.history)

    def tearDown(self):
        self.commands.close();self.history.close();self.temp.cleanup()

    def submit(self,text):
        req={'request_id':str(uuid.uuid4()),'agent_id':AGENT,'text':text}
        self.commands.create(req);return req

    def wait(self,ident,states):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            self.commands.tick()
            row=next(r for r in self.commands.list(AGENT)['commands'] if r['id']==ident)
            if row['state'] in states:return row
            time.sleep(.015)
        self.fail('Native fixture did not reach '+repr(states)+'; '+repr(row))

    def test_two_queued_turns_use_same_native_session_and_survive_reconnect(self):
        first=self.submit('Remember 703');second=self.submit('Use the previous value')
        self.assertTrue(self.commands.create(first)['duplicate'])
        self.wait(first['request_id'],{'completed'});self.wait(second['request_id'],{'completed'})
        self.assertEqual(len(json.loads((self.root/'fixture-session.json').read_text())),2)
        rows=self.history.messages({AGENT},AGENT)
        self.assertEqual(len(rows),4)
        self.assertEqual([r['phase'] for r in rows],['prompt','final','prompt','final'])
        joined=json.dumps(rows)
        for secret in ['PRIVATE_REASONING','WRONG_SESSION','OLD_REPLAY','RAW_TOOL','RAW_RESULT','hidden-value','[OFFICE:']:
            self.assertNotIn(secret,joined)
        self.commands.close();self.commands=Commands(self.config,self.history)
        third=self.submit('Continue after reconnect');self.wait(third['request_id'],{'completed'})
        self.assertEqual(len(json.loads((self.root/'fixture-session.json').read_text())),3)
        self.assertIn('turn 3',self.history.messages({AGENT},AGENT)[-1]['text'])

    def test_native_error_and_shutdown_can_close_same_owned_client_concurrently(self):
        client=AcpClient(self.settings,self.root/'concurrent-close');client.initialize()
        failures=[]
        def stop():
            try:client.close()
            except Exception as error:failures.append(type(error).__name__)
        workers=[threading.Thread(target=stop) for _ in range(4)]
        for worker in workers:worker.start()
        for worker in workers:worker.join()
        self.assertEqual(failures,[])
        self.assertIsNotNone(client.child.poll())
        self.assertEqual(json.loads((self.root/'concurrent-close/process.json').read_text())['state'],'stopped')

    def test_disconnect_is_unknown_and_blocks_followers_without_retry(self):
        first=self.submit('crash-now');second=self.submit('Do not run while uncertain')
        row=self.wait(first['request_id'],{'delivery_unknown'})
        self.assertEqual(row['detail_code'],'hermes_disconnected')
        for _ in range(5):self.commands.tick()
        self.assertEqual(len(json.loads((self.root/'fixture-session.json').read_text())),1)
        self.assertEqual(next(r for r in self.commands.list(AGENT)['commands'] if r['id']==second['request_id'])['state'],'pending')

    def test_native_permission_requires_explicit_matching_once_decision(self):
        req=self.submit('Request approval')
        self.wait(req['request_id'],{'waiting_user'})
        permissions=self.commands.list(AGENT)['approvals'];self.assertEqual(len(permissions),1)
        self.assertNotIn('hidden-value',json.dumps(permissions))
        with self.assertRaises(CommandError):self.commands.decide({'agent_id':'hermes:other','approval_id':permissions[0]['id'],'decision':'allow_once'})
        with self.assertRaises(CommandError):self.commands.decide({'agent_id':AGENT,'approval_id':permissions[0]['id'],'decision':'allow_always'})
        self.commands.decide({'agent_id':AGENT,'approval_id':permissions[0]['id'],'decision':'allow_once'})
        self.wait(req['request_id'],{'completed'})

    def test_native_permission_rejection_and_empty_reply_are_not_success(self):
        req=self.submit('Request approval');self.wait(req['request_id'],{'waiting_user'})
        permission=self.commands.list(AGENT)['approvals'][0]
        self.commands.decide({'agent_id':AGENT,'approval_id':permission['id'],'decision':'reject_once'})
        self.assertEqual(self.wait(req['request_id'],{'failed'})['detail_code'],'permission_denied')
        req=self.submit('empty-result');self.wait(req['request_id'],{'failed'})

    def test_stream_revisions_advance_cursor_without_duplicating_message(self):
        self.history.stream_message(AGENT,'run','0','Hello')
        first=self.history.messages({AGENT},AGENT)[0]
        self.history.stream_message(AGENT,'run','0','Hello again',True)
        rows=self.history.messages({AGENT},AGENT,after=first['seq'])
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['event_id'],first['event_id'])
        self.assertEqual(rows[0]['phase'],'final');self.assertEqual(len(self.history.messages({AGENT},AGENT)),1)

    def test_configuration_requires_selected_native_session_and_local_argv(self):
        p=self.root/'config.json';initialize(p);config=load_config(p);config.update({k:v for k,v in self.config.items() if k!='topic'})
        save_config(p,config);self.assertEqual(load_config(p)['command_bindings'][AGENT]['session_id'],SESSION)
        config['command_adapters']['hermes_acp']['sessions'][SESSION]['command']='hermes acp; unexpected'
        save_config(p,config)
        with self.assertRaises(ValueError):load_config(p)

    def test_restarted_inflight_native_command_is_not_replayed(self):
        req=self.submit('Never replay')
        self.commands.db.execute("UPDATE commands SET state='running',run_id=id,payload=NULL")
        self.commands.db.commit();self.commands.close();self.commands=Commands(self.config,self.history)
        self.commands.tick()
        self.assertEqual(self.commands.list(AGENT)['commands'][0]['state'],'delivery_unknown')
        self.assertEqual(json.loads((self.root/'fixture-session.json').read_text()),[])

    def test_same_native_session_cannot_be_loaded_by_second_office(self):
        req=self.submit('Keep this session');self.wait(req['request_id'],{'completed'})
        with self.assertRaises(AcpError) as caught:
            connect_session(self.settings,self.root/'second-office',SESSION)
        self.assertEqual(caught.exception.code,'hermes_session_in_use')
        self.assertEqual(len(json.loads((self.root/'fixture-session.json').read_text())),1)


if __name__=='__main__':unittest.main()
