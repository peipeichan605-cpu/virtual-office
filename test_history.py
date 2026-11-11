import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from dashboard import CodexTail, Bridge, proxy_handler, utc
from history import HistoryStore
from workspaces import Workspaces

class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'history.sqlite3'
        self.store = HistoryStore(self.path, secrets=('gbw-abcdefghijklmnopqrstuv',))
        self.allowed = {'codex:selected'}
    def tearDown(self):
        self.store.close()
        self.temp.cleanup()
    def crouton_fixture(self, status='completed'):
        root=Path(self.temp.name).resolve();folder=root/'run';folder.mkdir()
        run='crouton-01-11111111-1111-4111-8111-111111111111'
        (folder/'status.json').write_text(json.dumps({'runId':run,'status':status,'finishedAt':utc()}))
        bridge=Bridge.__new__(Bridge);bridge.crouton=SimpleNamespace(allowed_roots=[root])
        bridge.crouton_names={'crouton-01':'Selected Crouton'};bridge.history=self.store;bridge.history_seen={}
        bridge.workspaces=Workspaces(self.store)
        return bridge,folder,run
    def test_raw_result_does_not_override_bound_status_or_become_dialogue(self):
        bridge,folder,run=self.crouton_fixture('failed')
        (folder/'result.json').write_text(json.dumps({'status':'done','text':'RAW TOOL CONTENT'}))
        bridge.capture_crouton('crouton-01',folder,run)
        allowed={'crouton:crouton-01'};row=self.store.list(allowed)[0]
        self.assertEqual(row['status'],'error')
        self.assertEqual(self.store.messages(allowed,'crouton:crouton-01'),[])
        self.assertNotIn(b'RAW TOOL CONTENT',self.path.read_bytes())
        (folder/'result.json').write_text(json.dumps({'run_id':run,'agent_id':'crouton-01','status':'failed','text':'Public failure report.'}))
        bridge.capture_crouton('crouton-01',folder,run)
        self.assertEqual(self.store.messages(allowed,'crouton:crouton-01')[0]['text'],'Public failure report.')
    def test_result_for_another_run_is_rejected(self):
        bridge,folder,run=self.crouton_fixture()
        (folder/'result.json').write_text(json.dumps({'run_id':'another-run','status':'completed','text':'Wrong delivery'}))
        with self.assertRaises(ValueError):bridge.capture_crouton('crouton-01',folder,run)
        self.assertEqual(self.store.list({'crouton:crouton-01'}),[])
    def test_redaction_persistence_and_terminal_replay(self):
        secret = 'sk-proj-' + 's' * 30
        ident = self.store.record('codex:selected','codex','Selected','run1','working',prompt='Do the test; password=hidden-value')
        self.store.record('codex:selected','codex','Selected','run1','done',delivery='Delivered report '+secret+' gbw-abcdefghijklmnopqrstuv alice@example.com')
        self.store.record('codex:selected','codex','Selected','run1','working')
        self.store.close();self.store=HistoryStore(self.path)
        row=self.store.detail(ident,self.allowed)
        self.assertEqual(row['status'],'done')
        self.assertIn('Delivered report',row['delivery'])
        for value in [secret,'hidden-value','abcdefghijklmnopqrstuv','alice@example.com']:
            self.assertNotIn(value,json.dumps(row))
            self.assertNotIn(value.encode(),self.path.read_bytes())
        self.assertEqual(len(self.store.list(self.allowed)),1)
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)
    def test_unselected_rows_and_source_paths_stay_private(self):
        ident=self.store.record('codex:other','codex','Other','other-run','done',delivery='private',source_ref='/private/source.jsonl')
        self.assertIsNone(self.store.detail(ident,self.allowed))
        self.assertEqual(self.store.list(self.allowed),[])
        row=self.store.detail(ident,{'codex:other'})
        self.assertNotIn('source_ref',row)
    def test_only_user_task_and_final_answer_are_captured(self):
        path=Path(self.temp.name)/'rollout.jsonl'
        path.write_text(json.dumps({'type':'session_meta','payload':{'id':'selected'}})+'\n')
        record=lambda **e:self.store.record('codex:selected','codex','Selected',**e)
        tail=CodexTail('selected',path,on_history=record)
        def add(kind,payload):
            with path.open('a') as f:f.write(json.dumps({'type':kind,'timestamp':utc(),'payload':payload})+'\n')
        add('event_msg',{'type':'task_started','turn_id':'turn-1'})
        add('response_item',{'type':'message','role':'user','content':[{'type':'input_text','text':'<environment_context>INTERNAL CONTEXT</environment_context>'}]})
        add('response_item',{'type':'message','role':'user','content':[{'type':'input_text','text':'Make a report.'}]})
        add('response_item',{'type':'reasoning','text':'HIDDEN REASONING'})
        add('response_item',{'type':'message','role':'assistant','phase':'analysis','content':[{'type':'output_text','text':'HIDDEN ANALYSIS'}]})
        add('response_item',{'type':'function_call','arguments':'PRIVATE TOOL DATA'})
        add('event_msg',{'type':'task_complete','turn_id':'old-run','last_agent_message':'WRONG RUN'})
        add('event_msg',{'type':'task_complete','turn_id':'turn-1','last_agent_message':'Report delivered.'})
        tail.read()
        rows=self.store.list(self.allowed)
        self.assertEqual(len(rows),1)
        row=self.store.detail(rows[0]['id'],self.allowed)
        self.assertEqual(row['prompt'],'Make a report.')
        self.assertEqual(row['delivery'],'Report delivered.')
        for word in ['HIDDEN','INTERNAL CONTEXT','PRIVATE TOOL','WRONG RUN']:
            self.assertNotIn(word,self.path.read_bytes().decode(errors='ignore'))

class PrivateApiTests(unittest.TestCase):
    def test_key_origin_and_selected_members_are_required(self):
        with tempfile.TemporaryDirectory() as d:
            b=Bridge.__new__(Bridge);b.config={'port':0,'topic':'fixture-private-key'};b.health={};b.sources={}
            b.agents={'codex:selected':{'id':'codex:selected','name':'Selected','provider':'codex','native_url':None}}
            b.history=HistoryStore(Path(d)/'history.sqlite3')
            b.commands=MagicMock();b.commands.capability.return_value={'can_send':False,'same_session':False}
            b.workspaces=Workspaces(b.history)
            ident=b.history.record('codex:selected','codex','Selected','one','done',delivery='Actual delivery')
            other=b.history.record('codex:other','codex','Other','two','done',delivery='PRIVATE OTHER')
            server=ThreadingHTTPServer(('127.0.0.1',0),proxy_handler(b));b.config['port']=server.server_address[1]
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def get(path,key=None,origin=None):
                headers={}
                if key:headers['X-Office-Key']=key
                if origin:headers['Origin']=origin
                req=urllib.request.Request('http://127.0.0.1:%s%s'%(b.config['port'],path),headers=headers)
                try:
                    with urllib.request.urlopen(req,timeout=2) as r:return r.status,json.load(r)
                except urllib.error.HTTPError as e:return e.code,json.load(e)
            try:
                self.assertEqual(get('/api/office/work')[0],403)
                self.assertEqual(get('/api/office/work','wrong')[0],403)
                self.assertEqual(get('/api/office/work','fixture-private-key','https://attacker.example')[0],403)
                code,data=get('/api/office/work','fixture-private-key');self.assertEqual(code,200)
                self.assertEqual(len(data['agents']),1);self.assertEqual(len(data['finished']),1)
                self.assertNotIn('PRIVATE OTHER',json.dumps(data))
                self.assertEqual(get('/api/office/run?id='+other,'fixture-private-key')[0],404)
                self.assertEqual(get('/api/office/run?id='+ident,'fixture-private-key')[1]['delivery'],'Actual delivery')
            finally:
                server.shutdown();server.server_close();thread.join();b.history.close()

if __name__=='__main__':unittest.main()
