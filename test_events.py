import hashlib
import json
from pathlib import Path
import sys
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from history import HistoryStore
from dashboard import Bridge, CodexTail, proxy_handler, utc
from configuration import initialize,load_config,save_config
from event_ingest import ingest,EventConflict
sys.path.insert(0,str(Path(__file__).parent/'adapters'))
from cursor_hook import translate

class EventTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)
        self.store=HistoryStore(self.path/'history.sqlite3')
        self.agent={'id':'cursor:chosen','source_id':'chosen','provider':'cursor','name':'Cursor chosen'}
        self.allowed={self.agent['id']}
    def tearDown(self):self.store.close();self.temp.cleanup()
    def event(self,event_id,kind='message',run='run-one',**extra):
        return {'event_id':event_id,'type':kind,'source_id':'chosen','run_id':run,**extra}
    def test_public_conversation_dedupe_restart_and_terminal_fence(self):
        start=self.event('s','turn_started',text='Build report; password=secret-value')
        self.assertFalse(ingest(self.store,self.agent,start)['duplicate'])
        message=self.event('m',role='assistant',phase='commentary',text='Checking the fixtures.')
        ingest(self.store,self.agent,message)
        ingest(self.store,self.agent,self.event('f','turn_completed',text='Report delivered.'))
        self.store.close();self.store=HistoryStore(self.path/'history.sqlite3')
        self.assertTrue(ingest(self.store,self.agent,message)['duplicate'])
        with self.assertRaises(EventConflict):ingest(self.store,self.agent,self.event('restart','turn_started'))
        with self.assertRaises(EventConflict):ingest(self.store,self.agent,{**message,'text':'different'})
        rows=self.store.messages(self.allowed,self.agent['id']);self.assertEqual(len(rows),3)
        self.assertEqual(rows[1]['text'],'Checking the fixtures.')
        self.assertNotIn(b'secret-value',(self.path/'history.sqlite3').read_bytes())
        self.assertEqual(self.store.list(self.allowed)[0]['status'],'done')
    def test_superseded_runs_cannot_finish_current_work(self):
        ingest(self.store,self.agent,self.event('start-old','turn_started'))
        ingest(self.store,self.agent,self.event('start-new','turn_started',run='new-run'))
        with self.assertRaises(EventConflict):ingest(self.store,self.agent,self.event('late','turn_completed'))
        rows=self.store.list(self.allowed);self.assertEqual({r['status'] for r in rows},{'working','unknown'})
    def test_unknown_source_and_hidden_content_are_rejected(self):
        for event in [self.event('one','turn_started',source_id='other'), self.event('two',role='assistant',phase='analysis',text='Hidden'),
                      self.event('three',role='tool',phase='commentary',text='Secret tool'),self.event('four','turn_started',run='invalid:run')]:
            with self.assertRaises(ValueError):ingest(self.store,self.agent,event)
        self.assertEqual(self.store.list(self.allowed),[])
    def test_cursor_hook_mapping_is_selected_and_observational(self):
        base={'conversation_id':'chosen','generation_id':'generation-one'}
        start=translate({**base,'hook_event_name':'beforeSubmitPrompt','prompt':'Build it'},'chosen')
        ingest(self.store,self.agent,start)
        ingest(self.store,self.agent,translate({**base,'hook_event_name':'afterAgentResponse','text':'Done'},'chosen'))
        ingest(self.store,self.agent,translate({**base,'hook_event_name':'stop','status':'completed'},'chosen'))
        self.assertEqual(self.store.list(self.allowed)[0]['status'],'done')
        self.assertIsNone(translate({**base,'hook_event_name':'afterAgentThought','text':'Hidden'},'chosen'))
        self.assertIsNone(translate({**base,'hook_event_name':'beforeSubmitPrompt'},'another'))
    def test_grok_public_events_use_the_same_contract(self):
        grok={'id':'grok_bot:chosen','source_id':'chosen','provider':'grok_bot','name':'Grok test'}
        ingest(self.store,grok,self.event('g-start','turn_started',text='Check the report'))
        ingest(self.store,grok,self.event('g-update',role='assistant',phase='commentary',text='Reading the public data.'))
        ingest(self.store,grok,self.event('g-done','turn_completed',text='Delivered.'))
        self.assertEqual(len(self.store.messages({grok['id']},grok['id'])),3)
    def test_message_pagination_never_leaks_other_agent(self):
        for i in range(125):self.store.message('cursor:chosen','r','assistant','commentary',str(i),event_id=str(i))
        self.store.message('cursor:other','r','assistant','commentary','PRIVATE OTHER',event_id='other')
        page=self.store.messages(self.allowed,'cursor:chosen');self.assertEqual(len(page),100)
        earlier=self.store.messages(self.allowed,'cursor:chosen',before=page[0]['seq']);self.assertEqual(len(earlier),25)
        self.assertEqual(self.store.messages(self.allowed,'cursor:other'),[])
        self.assertEqual(self.store.messages(self.allowed,'cursor:chosen',after=page[-1]['seq']),[])
    def test_codex_public_progress_and_final_are_captured_once(self):
        path=self.path/'selected.jsonl';path.write_text(json.dumps({'type':'session_meta','payload':{'id':'chosen'}})+'\n')
        tail=CodexTail('chosen',path,on_message=lambda **e:self.store.message('codex:chosen',**e))
        def add(kind,payload):
            with path.open('a') as file:file.write(json.dumps({'type':kind,'timestamp':utc(),'payload':payload})+'\n')
        add('event_msg',{'type':'task_started','turn_id':'run-one'})
        for role,phase,text in [('user',None,'Build it'),('assistant','analysis','HIDDEN'),('assistant','commentary','Checking it'),('assistant','final_answer','Delivered')]:
            add('response_item',{'type':'message','role':role,'phase':phase,'content':[{'text':text}]})
        add('event_msg',{'type':'task_complete','turn_id':'run-one','last_agent_message':'Delivered'})
        tail.read();rows=self.store.messages({'codex:chosen'},'codex:chosen')
        self.assertEqual([r['text'] for r in rows],['Build it','Checking it','Delivered'])
        self.assertNotIn(b'HIDDEN',(self.path/'history.sqlite3').read_bytes())

class PortableHttpTests(unittest.TestCase):
    def test_default_data_directory_moves_with_the_config(self):
        with tempfile.TemporaryDirectory() as d:
            old=Path(d)/'first/config.json';initialize(old)
            config=load_config(old);save_config(old,config)
            moved=Path(d)/'second';shutil.copytree(old.parent,moved)
            loaded=load_config(moved/'config.json')
            self.assertEqual(loaded['data_dir'],str(moved.resolve()))
            self.assertNotIn('data_dir',json.loads((moved/'config.json').read_text()))
    def test_names_cannot_collapse_into_the_same_rendered_identity(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'config.json';initialize(path);config=load_config(path)
            config['agents']=[{'provider':'codex','source_id':'one','name':'Café'},
                              {'provider':'codex','source_id':'two','name':'Cafe\u0301 '}]
            save_config(path,config)
            with self.assertRaises(ValueError):load_config(path)
    def test_hook_outage_never_blocks_prompt_or_exposes_text(self):
        script=Path(__file__).parent/'adapters/cursor_hook.py'
        private_text='private synthetic prompt body'
        result=subprocess.run([sys.executable,str(script),'--credential','/missing-office-fixture.json'],
            input=json.dumps({'hook_event_name':'beforeSubmitPrompt','prompt':private_text}),text=True,capture_output=True)
        self.assertEqual(result.returncode,0)
        self.assertEqual(json.loads(result.stdout),{'continue':True})
        self.assertNotIn(private_text,result.stderr)
        invalid=subprocess.run([sys.executable,str(script),'--credential','/missing-office-fixture.json'],input='[]',text=True,capture_output=True)
        self.assertEqual(invalid.returncode,0);self.assertEqual(json.loads(invalid.stdout),{'continue':True})
    def test_clean_config_http_scope_events_and_relocation(self):
        with tempfile.TemporaryDirectory(prefix='office relocated ') as d:
            path=Path(d)/'local/config.json';initialize(path,4444)
            config=load_config(path);self.assertEqual(config['agents'],[]);self.assertIsNone(config['feed'])
            token='test-scoped-token'
            config['agents']=[{'provider':'cursor','source_id':'chosen','name':'Cursor selected','driver':'push','token_sha256':hashlib.sha256(token.encode()).hexdigest()}]
            save_config(path,config);config=load_config(path)
            config['run_dir']=str(Path(d)/'run');Path(config['run_dir']).mkdir()
            config['run_id']='fixture';b=Bridge(config)
            server=ThreadingHTTPServer(('127.0.0.1',0),proxy_handler(b));b.config['port']=server.server_address[1]
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def call(route,event=None,credential=token,agent='cursor:chosen',owner=False,origin=None):
                headers={'Authorization':'Bearer '+credential,'X-Agent-Id':agent}
                if owner:headers['X-Office-Key']=config['topic']
                if origin:headers['Origin']=origin
                req=urllib.request.Request('http://127.0.0.1:'+str(b.config['port'])+route,data=json.dumps(event).encode() if event else None,headers=headers)
                try:
                    with urllib.request.urlopen(req,timeout=3) as r:return r.status,json.load(r)
                except urllib.error.HTTPError as error:return error.code,json.load(error)
            event={'source_id':'chosen','run_id':'run-one','event_id':'start','type':'turn_started','text':'Make a report'}
            try:
                self.assertEqual(call('/api/agents/events',event,credential='wrong')[0],403)
                self.assertEqual(call('/api/agents/events',event,agent='cursor:other')[0],403)
                self.assertEqual(call('/api/agents/events',event,origin='https://attacker.example')[0],403)
                self.assertEqual(call('/api/agents/events',event)[0],200)
                self.assertTrue(call('/api/agents/events',event)[1]['duplicate'])
                self.assertEqual(call('/api/office/messages?agent=cursor%3Achosen')[0],403)
                code,data=call('/api/office/messages?agent=cursor%3Achosen',owner=True)
                self.assertEqual(code,200);self.assertEqual(data['messages'][0]['text'],'Make a report')
                self.assertEqual(call('/api/office/messages?agent=cursor%3Aother',owner=True)[0],404)
                self.assertEqual(call('/api/office/work',owner=True)[1]['agents'][0]['state'],'working')
                self.assertEqual(b.outbound.qsize(),0)
            finally:server.shutdown();server.server_close();thread.join();b.history.close()

if __name__=='__main__':unittest.main()
