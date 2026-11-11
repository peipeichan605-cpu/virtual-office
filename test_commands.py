import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from commands import Commands, CommandError
from command_adapters import CodexAppAdapter, DeliveryError
from configuration import initialize, load_config, save_config
from dashboard import Bridge, proxy_handler
from history import HistoryStore

SESSION='00000000-0000-4000-8000-000000000001'
AGENT='codex:'+SESSION


class Adapter:
    reason=None
    def __init__(self):self.calls=[];self.effect=None
    def available(self):return True
    def submit(self,session,text):
        self.calls.append((session,text))
        if self.effect:return self.effect(session,text)
        return {'native_message_id':'test-receipt','wake_requested':True}


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.history=HistoryStore(self.root/'history.sqlite3',secrets=('private-office-key',))
        self.config={'data_dir':str(self.root),'topic':'private-office-key','command_bindings':{AGENT:{'adapter':'codex_app','session_id':SESSION}}}
        self.adapter=Adapter();self.c=Commands(self.config,self.history,adapters={'codex_app':self.adapter})
    def tearDown(self):self.c.close();self.history.close();self.temp.cleanup()
    def request(self,text='Calculate 17 * 23',**fields):return {'request_id':str(uuid.uuid4()),'agent_id':AGENT,'text':text,**fields}
    def state(self):return self.c.list(AGENT)['commands'][0]['state']
    def test_retry_across_restart_only_delivers_once_and_payload_is_erased(self):
        req=self.request('password=fictional-sensitive-value')
        self.c.create(req);self.assertTrue(self.c.create(req)['duplicate'])
        self.assertNotIn('fictional-sensitive-value',json.dumps(self.c.list(AGENT)))
        self.c.tick();self.assertEqual(self.state(),'queued');self.assertEqual(len(self.adapter.calls),1)
        self.assertNotIn(b'fictional-sensitive-value',(self.root/'commands.sqlite3').read_bytes())
        self.c.close();self.c=Commands(self.config,self.history,adapters={'codex_app':self.adapter})
        self.assertTrue(self.c.create(req)['duplicate']);self.c.tick();self.assertEqual(len(self.adapter.calls),1)
        with self.assertRaises(CommandError):self.c.create({**req,'text':'Different command'})
    def test_original_session_prompt_and_terminal_evidence_are_required(self):
        req=self.request();self.c.create(req);self.c.tick();body=self.adapter.calls[0][1]
        self.c.observe_run(AGENT,'unrelated','done');self.assertEqual(self.state(),'queued')
        self.c.observe_prompt('codex:other','r1',body);self.assertEqual(self.state(),'queued')
        self.c.observe_prompt(AGENT,'r1',body+' altered');self.assertEqual(self.state(),'queued')
        self.assertEqual(self.c.observe_prompt(AGENT,'r1',body),req['text']);self.assertEqual(self.state(),'running')
        self.c.observe_run(AGENT,'r2','done');self.assertEqual(self.state(),'running')
        self.c.observe_run(AGENT,'r1','done');self.assertEqual(self.state(),'completed')
        self.c.observe_prompt(AGENT,'r1',body);self.c.observe_run(AGENT,'r1','error');self.assertEqual(self.state(),'completed')
    def test_observation_can_beat_queue_process_receipt(self):
        req=self.request();self.c.create(req)
        def early(session,text):
            self.c.observe_prompt(AGENT,'early-run',text);self.c.observe_run(AGENT,'early-run','done')
            return {'native_message_id':'receipt','wake_requested':True}
        self.adapter.effect=early;self.c.tick();self.assertEqual(self.state(),'completed')
    def test_timeout_is_unknown_and_never_automatically_retried(self):
        self.c.create(self.request())
        def timeout(*args):raise DeliveryError('delivery_unknown',uncertain=True)
        self.adapter.effect=timeout;self.c.tick();self.c.tick()
        self.assertEqual(self.state(),'delivery_unknown');self.assertEqual(len(self.adapter.calls),1)
        self.c.observe_prompt(AGENT,'late-run',self.adapter.calls[0][1]);self.assertEqual(self.state(),'running')
    def test_restart_during_send_does_not_replay(self):
        self.c.create(self.request());self.c.db.execute("UPDATE commands SET state='sending'");self.c.db.commit()
        self.c.close();self.c=Commands(self.config,self.history,adapters={'codex_app':self.adapter});self.c.tick()
        self.assertEqual(self.state(),'delivery_unknown');self.assertEqual(self.adapter.calls,[])
        self.assertIsNone(self.c.db.execute('SELECT payload FROM commands').fetchone()[0])
    def test_pending_survives_restart_and_existing_terminal_run_reconciles(self):
        req=self.request();self.c.create(req);self.c.close();self.c=Commands(self.config,self.history,adapters={'codex_app':self.adapter})
        self.c.tick();self.c.observe_prompt(AGENT,'r1',self.adapter.calls[0][1])
        self.history.record(AGENT,'codex','test','r1','done',delivery='391')
        self.c.close();self.c=Commands(self.config,self.history,adapters={'codex_app':self.adapter});self.assertEqual(self.state(),'completed')
    def test_unbound_agent_and_changed_binding_cannot_dispatch(self):
        with self.assertRaises(CommandError):self.c.create(self.request(agent_id='generic:other'))
        self.c.create(self.request());self.c.bindings={};self.c.tick()
        self.assertEqual(self.state(),'failed');self.assertEqual(self.adapter.calls,[])
    def test_disabled_runtime_cannot_submit_or_dispatch(self):
        self.c.enabled=False
        with self.assertRaises(CommandError):self.c.create(self.request())
        self.c.tick();self.assertEqual(self.adapter.calls,[])
    def test_concurrent_duplicate_submissions_share_one_command(self):
        req=self.request();threads=[threading.Thread(target=self.c.create,args=(req,)) for _ in range(6)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.c.tick();self.assertEqual(len(self.c.list(AGENT)['commands']),1);self.assertEqual(len(self.adapter.calls),1)


class AdapterTests(unittest.TestCase):
    def test_first_capability_probe_runs_even_with_small_monotonic_clock(self):
        a=CodexAppAdapter({'binary':'/test/codex'})
        with patch('command_adapters.sys.platform','darwin'),patch('command_adapters.time.monotonic',return_value=2),patch('command_adapters.Path.is_file',return_value=False),patch('command_adapters.os.access',return_value=True),patch('command_adapters.subprocess.run',return_value=subprocess.CompletedProcess([],0,'Queue a message for an existing session --thread','')) as run:
            self.assertTrue(a.available());self.assertTrue(a.available());self.assertEqual(run.call_count,1)
    def adapter(self):
        a=CodexAppAdapter();a.binary='/test/codex';a.available=lambda:True;return a
    def test_native_queue_never_opens_app_and_receipt_does_not_mean_executed(self):
        a=self.adapter();text='Literal $(command) `backticks`\nNext line'
        queued=subprocess.CompletedProcess([],0,'Queued message '+str(uuid.uuid4())+' for thread '+SESSION+'.\n','')
        with patch('command_adapters.subprocess.run',return_value=queued) as run:
            receipt=a.submit(SESSION,text)
        self.assertEqual(receipt['delivery_mode'],'background_queue');self.assertNotIn('completed',receipt)
        self.assertEqual(run.call_args_list[0].args[0],['/test/codex','queue','--thread',SESSION,'--message',text])
        self.assertNotIn('shell',run.call_args_list[0].kwargs)
        self.assertEqual(run.call_count,1,'Delivery must not open or focus the App')
    def test_unrecognized_receipt_is_uncertain_and_never_opens_wrong_session(self):
        a=self.adapter()
        with patch('command_adapters.subprocess.run',return_value=subprocess.CompletedProcess([],0,'Something else','')) as run:
            with self.assertRaises(DeliveryError) as result:a.submit(SESSION,'Hello')
        self.assertTrue(result.exception.uncertain);self.assertEqual(run.call_count,1)


class CommandHttpTests(unittest.TestCase):
    def test_owner_only_allowlist_idempotency_and_no_prompt_leak(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'config.json';initialize(path,4444);config=load_config(path)
            config['agents']=[{'provider':'codex','source_id':SESSION,'name':'Selected'}]
            config['command_bindings']={AGENT:{'adapter':'codex_app','session_id':SESSION}}
            save_config(path,config);config=load_config(path);config['run_dir']=d;config['run_id']='test'
            bridge=Bridge(config);adapter=Adapter();bridge.commands.adapters={'codex_app':adapter}
            server=ThreadingHTTPServer(('127.0.0.1',0),proxy_handler(bridge));bridge.config['port']=server.server_address[1]
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def call(body=None,owner=True,origin=None):
                headers={'Content-Type':'application/json'}
                if owner:headers['X-Office-Key']=config['topic']
                else:headers.update({'Authorization':'Bearer agent-credential','X-Agent-Id':AGENT})
                if origin:headers['Origin']=origin
                url='http://127.0.0.1:'+str(server.server_address[1])+'/api/office/commands'+('' if body else '?agent='+AGENT)
                req=urllib.request.Request(url,data=json.dumps(body).encode() if body else None,headers=headers)
                try:
                    with urllib.request.urlopen(req,timeout=3) as r:return r.status,json.load(r)
                except urllib.error.HTTPError as e:return e.code,json.load(e)
            request={'agent_id':AGENT,'request_id':str(uuid.uuid4()),'text':'password=private-test-content'}
            try:
                self.assertEqual(call(request,owner=False)[0],403)
                self.assertEqual(call(request,origin='https://attacker.invalid')[0],403)
                self.assertEqual(call({**request,'agent_id':'codex:unselected'})[0],404)
                self.assertEqual(call(request)[0],202);self.assertTrue(call(request)[1]['duplicate'])
                self.assertNotIn('private-test-content',json.dumps(call()))
                self.assertEqual(adapter.calls,[])
                bridge.commands.tick();self.assertEqual(len(adapter.calls),1)
            finally:server.shutdown();server.server_close();thread.join();bridge.commands.close();bridge.history.close()
    def test_config_cannot_route_selected_identity_to_another_session(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'config.json';initialize(p);config=load_config(p)
            config['agents']=[{'provider':'codex','source_id':SESSION,'name':'Selected'}]
            config['command_bindings']={AGENT:{'adapter':'codex_app','session_id':str(uuid.uuid4())}}
            save_config(p,config)
            with self.assertRaises(ValueError):load_config(p)

if __name__=='__main__':unittest.main()
