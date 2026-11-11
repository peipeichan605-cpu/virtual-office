"""Contract fixtures, not evidence of an authenticated live vendor run."""
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from command_adapters import DeliveryError
from commands import Commands, CommandError
from configuration import initialize, load_config, save_config
from connections import Connections, ConnectionError
from dashboard import Bridge, proxy_handler
from history import HistoryStore
from platform_adapters import CursorCloudAdapter, GrokWebhookAdapter, HTTPS, RemoteError, secret_file, webhook_url

SID='bc-00000000-0000-4000-8000-000000000003'
AGENT='cursor:'+SID
KEY='fixture-key-PRIVATE-987654'


class Response(io.BytesIO):
    status=200


class Connection:
    def close(self):pass


class CursorFixture:
    def __init__(self):
        self.calls=[];self.runs={};self.posts=[];self.interrupted=False;self.wrong=False
    def json(self, method, url, headers, data=None):
        self.calls.append((method,url))
        if method=='POST':
            self.posts.append(data)
            native='run-'+str(uuid.uuid4());self.runs[native]={'id':native,'agentId':SID,'status':'FINISHED','result':'round %s completed password=hidden-value'%len(self.posts)}
            return {'run':{'id':native,'agentId':SID,'status':'CREATING'}}
        if '/runs/' in url:
            run=dict(self.runs[url.rsplit('/',1)[1]])
            if self.interrupted:run['status']='RUNNING';run.pop('result',None)
            if self.wrong:run['agentId']='bc-wrong-target'
            return run
        return {'id':SID,'status':'IDLE'}
    def open(self, method, url, headers, body=None):
        self.calls.append((method,url));native=url.split('/')[-2]
        def frame(kind,data):return ('event: '+kind+'\ndata: '+json.dumps(data)+'\n\n').encode()
        data=frame('status',{'runId':native,'status':'RUNNING'})
        data+=frame('thinking',{'text':'PRIVATE_REASONING_MUST_NOT_SAVE'})
        data+=frame('assistant',{'text':'Working. '})
        data+=frame('tool_call',{'name':'run_terminal_cmd','status':'running','args':{'secret':'RAW_TOOL_MUST_NOT_SAVE'},'result':'RAW_RESULT_MUST_NOT_SAVE'})
        data+=frame('assistant',{'text':'round %s '%len(self.posts)})
        if not self.interrupted:
            data+=frame('result',{'runId':native if not self.wrong else 'run-wrong','status':'FINISHED','text':self.runs[native]['result']})
            data+=frame('done',{})
        return Connection(),Response(data)


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.path=self.root/'config.json';initialize(self.path,48418)
        self.config=load_config(self.path);self.config['run_id']='platform-fixture';self.config['run_dir']=str(self.root)
        self.bridge=Bridge(self.config)
        self.transport=CursorFixture();self.bridge.connections.transport=self.transport
    def tearDown(self):
        self.bridge.commands.close();self.bridge.history.close();self.temp.cleanup()
    def connect(self):
        result=self.bridge.connections.connect_cursor({'name':'Cursor fixture','session_id':SID,'api_key':KEY})
        self.bridge.commands.adapters['cursor_cloud'].transport=self.transport
        return result
    def submit(self,agent=AGENT,text='Do the bounded fixture task'):
        req={'request_id':str(uuid.uuid4()),'agent_id':agent,'text':text}
        self.bridge.commands.create(req);return req
    def wait(self,request,states):
        end=time.monotonic()+4
        while time.monotonic()<end:
            self.bridge.commands.tick()
            row=next(r for r in self.bridge.commands.list(request['agent_id'])['commands'] if r['id']==request['request_id'])
            if row['state'] in states:return row
            time.sleep(.01)
        self.fail('fixture did not reach '+repr(states)+': '+repr(row))
    def grok(self):
        invite=self.bridge.invitations.create({'request_id':'fixture-grok-0001','name':'Grok fixture','provider':'grok_bot'})
        file,guide,credential=self.bridge.invitations.paths(invite['id']);claim=json.loads(file.read_text())
        self.bridge.invitations.claim(invite['id'],claim['token'],{'claim_id':uuid.uuid4().hex})
        agent=self.bridge.definitions[invite['agent_id']]
        for kind in ('turn_started','turn_completed'):
            self.bridge.accept_agent_event(agent,{'source_id':agent['source_id'],'event_id':'join:'+kind,'type':kind,'run_id':'join-fixture','text':'Local return route test'})
        self.bridge.connections.connect_grok({'agent_id':agent['id'],'url':'https://api.cursor.com/fixture/webhook','key':KEY})
        return agent
    def test_cursor_self_serve_two_turns_same_agent_stream_and_private_history(self):
        result=self.connect();self.assertTrue(result['same_session']);self.assertFalse(result['execution_verified'])
        self.assertEqual(len(self.bridge.agents),1,'Must not import other account agents')
        first=self.submit(text='Remember fixture 917');second=self.submit(text='Continue with previous fixture value')
        self.wait(first,{'completed'});self.wait(second,{'completed'})
        self.assertEqual(len(self.transport.posts),2)
        self.assertTrue(all('/'+SID+'/runs' in url for method,url in self.transport.calls if method=='POST'))
        self.assertEqual(set(self.transport.posts[0]),{'prompt'},'No provider, model, tool or permission overrides')
        self.assertTrue(self.bridge.commands.create(first)['duplicate'])
        self.bridge.commands.tick();self.assertEqual(len(self.transport.posts),2)
        rows=self.bridge.history.messages({AGENT},AGENT)
        self.assertEqual(len([r for r in rows if r['phase']=='prompt']),2)
        self.assertEqual(len([r for r in rows if r['phase']=='final']),2)
        public=json.dumps(rows)
        for secret in ('PRIVATE_REASONING','RAW_TOOL','RAW_RESULT','hidden-value','[OFFICE:',KEY):self.assertNotIn(secret,public)
        self.assertIn('round 2',rows[-1]['text'])
        self.assertEqual(self.bridge.commands.db.execute('SELECT count(*) FROM commands WHERE payload IS NOT NULL').fetchone()[0],0)
        self.assertNotIn(KEY,self.path.read_text())
        credential=load_config(self.path)['command_adapters']['cursor_cloud']['sessions'][SID]['credential_file']
        self.assertEqual(Path(credential).stat().st_mode&0o777,0o600)
    def test_cursor_restart_resumes_only_exact_native_get_and_deduplicates(self):
        self.connect();self.transport.interrupted=True
        request=self.submit();self.wait(request,{'delivery_unknown'})
        follower=self.submit(text='Must wait for reconciliation')
        self.bridge.commands.tick();self.assertEqual(len(self.transport.posts),1)
        self.bridge.commands.close();self.transport.interrupted=False
        config=load_config(self.path)
        self.bridge.commands=Commands(config,self.bridge.history)
        self.bridge.commands.adapters['cursor_cloud'].transport=self.transport
        self.wait(request,{'completed'});self.wait(follower,{'completed'})
        self.assertEqual(len(self.transport.posts),2)
        self.assertTrue(self.bridge.commands.create(request)['duplicate'])
        self.assertEqual(len([r for r in self.bridge.history.messages({AGENT},AGENT) if r['phase']=='prompt']),2)
        first_rows=[r for r in self.bridge.history.messages({AGENT},AGENT) if r['run_id']==request['request_id']]
        self.assertEqual(len(first_rows),3)
        self.assertEqual(first_rows[1]['text'],'Working.')
        self.assertEqual(first_rows[2]['phase'],'final')
    def test_cursor_wrong_target_cannot_finish_run(self):
        self.connect();self.transport.wrong=True
        req=self.submit();self.wait(req,{'delivery_unknown'})
        self.assertEqual(len(self.transport.posts),1)
    def test_grok_receipt_requires_matching_callback_and_survives_restart(self):
        agent=self.grok();calls=[]
        adapter=self.bridge.commands.adapters['grok_webhook']
        class Transport:
            def open(self,method,url,headers,body):calls.append(json.loads(body));return Connection(),Response()
        adapter.transport=Transport()
        first=self.submit(agent['id']);follower=self.submit(agent['id'],'Next task')
        self.wait(first,{'queued'});self.bridge.commands.tick();self.assertEqual(len(calls),1)
        self.assertFalse(self.bridge.commands.capability(agent['id'])['same_session'])
        self.bridge.commands.close();self.bridge.commands=Commands(load_config(self.path),self.bridge.history)
        self.bridge.commands.adapters['grok_webhook'].transport=Transport()
        self.bridge.commands.tick();self.assertEqual(len(calls),1,'Never repeat a webhook after restart')
        event={'source_id':agent['source_id'],'run_id':first['request_id'],'event_id':first['request_id']+':start','type':'turn_started','text':calls[0]['prompt']['text']}
        self.bridge.accept_agent_event(agent,event)
        self.assertTrue(self.bridge.accept_agent_event(agent,event)['duplicate'])
        self.assertEqual(self.wait(first,{'running'})['run_id'],first['request_id'])
        done={**event,'type':'turn_completed','event_id':first['request_id']+':done','text':'Actual callback delivery'}
        self.bridge.accept_agent_event(agent,done);self.wait(first,{'completed'});self.wait(follower,{'queued'})
        self.assertEqual(len(calls),2)
        saved=self.bridge.history.messages({agent['id']},agent['id'])
        self.assertNotIn('[OFFICE:',json.dumps(saved));self.assertIn('Actual callback delivery',json.dumps(saved))
    def test_grok_reporter_newline_matches_canonical_prompt_without_duplicate_history(self):
        agent=self.grok();calls=[]
        class Transport:
            def open(self,method,url,headers,body):calls.append(json.loads(body));return Connection(),Response()
        self.bridge.commands.adapters['grok_webhook'].transport=Transport()
        req=self.submit(agent['id'],'Keep the\n  inner indentation')
        self.bridge.history.message(agent['id'],req['request_id'],'user','prompt',req['text'],event_id='inbox:'+req['request_id']+':prompt')
        self.wait(req,{'queued'})
        event={'source_id':agent['source_id'],'run_id':req['request_id'],'event_id':req['request_id']+':start',
            'type':'turn_started','text':calls[0]['prompt']['text']+'\n'}
        self.bridge.accept_agent_event(agent,event);self.wait(req,{'running'})
        self.bridge.accept_agent_event(agent,{**event,'type':'turn_completed','event_id':req['request_id']+':done','text':'Actual newline callback'})
        self.wait(req,{'completed'})
        saved=[r for r in self.bridge.history.messages({agent['id']},agent['id']) if r['run_id']==req['request_id']]
        prompts=[r for r in saved if r['phase']=='prompt']
        self.assertEqual(len(prompts),1);self.assertEqual(prompts[0]['text'],req['text'])
        self.assertEqual(len(calls),1)

    def test_identical_authenticated_start_reconciles_already_completed_legacy_callback(self):
        from event_ingest import ingest
        agent=self.grok();calls=[]
        class Transport:
            def open(self,method,url,headers,body):calls.append(json.loads(body));return Connection(),Response()
        self.bridge.commands.adapters['grok_webhook'].transport=Transport()
        req=self.submit(agent['id']);self.wait(req,{'queued'})
        start={'source_id':agent['source_id'],'run_id':req['request_id'],'event_id':req['request_id']+':start',
            'type':'turn_started','text':calls[0]['prompt']['text']+'\n'}
        ingest(self.bridge.history,agent,start)
        ingest(self.bridge.history,agent,{**start,'type':'turn_completed','event_id':req['request_id']+':done','text':'Previously completed'})
        self.assertEqual(self.wait(req,{'queued'})['state'],'queued')
        receipt=self.bridge.accept_agent_event(agent,start)
        self.assertTrue(receipt['duplicate']);self.wait(req,{'completed'})
        self.assertEqual(len(calls),1,'Reconcile receipts; never send another webhook')

    def test_grok_wrong_prompt_cannot_reconcile_a_command(self):
        from event_ingest import EventConflict
        agent=self.grok();adapter=self.bridge.commands.adapters['grok_webhook']
        adapter.transport=type('T',(),{'open':lambda *args:(Connection(),Response())})()
        req=self.submit(agent['id']);self.wait(req,{'queued'})
        for kind in ('turn_started','turn_completed'):
            with self.assertRaises(EventConflict):
                self.bridge.accept_agent_event(agent,{'source_id':agent['source_id'],'run_id':req['request_id'],'event_id':kind,'type':kind,'text':'Unrelated prompt'})
        self.assertEqual(self.wait(req,{'queued'})['state'],'queued')
    def test_no_grok_command_binding_until_return_route_exists(self):
        with self.assertRaises(ConnectionError):
            self.bridge.connections.connect_grok({'agent_id':'grok_bot:unknown','url':'https://api.cursor.com/fixture/webhook','key':KEY})
        self.assertEqual(load_config(self.path)['agents'],[])
    def test_write_timeout_never_auto_retries_or_releases_followers(self):
        self.connect();calls=[]
        def fail(method,url,headers,data=None):calls.append(method);raise RemoteError('platform_delivery_unknown',True)
        self.transport.json=fail
        req=self.submit();self.wait(req,{'delivery_unknown'})
        self.submit(text='Waiting follower')
        for _ in range(4):self.bridge.commands.tick()
        self.assertEqual(calls,['POST'])
    def test_owner_only_connection_api_and_no_secret_reflection(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),proxy_handler(self.bridge));self.bridge.config['port']=server.server_address[1]
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        url='http://127.0.0.1:%s/api/office/connections/cursor'%server.server_address[1]
        body=json.dumps({'name':'Cursor fixture','session_id':SID,'api_key':KEY}).encode()
        def call(headers):
            return urllib.request.urlopen(urllib.request.Request(url,data=body,headers={'Content-Type':'application/json',**headers}),timeout=3)
        try:
            for headers in ({},{'X-Office-Key':self.config['topic'],'Origin':'https://foreign.example'}):
                with self.assertRaises(urllib.error.HTTPError) as caught:call(headers)
                self.assertEqual(caught.exception.code,403)
            with call({'X-Office-Key':self.config['topic']}) as response:result=response.read().decode()
            self.assertNotIn(KEY,result);self.assertIn('native_target_verified',result)
            self.assertEqual(len(self.transport.posts),0,'Connecting must not invoke a model')
        finally:server.shutdown();server.server_close();worker.join()


class TransportTests(unittest.TestCase):
    def test_webhook_rejects_private_arbitrary_hosts_and_url_credentials(self):
        for url in ('http://api.cursor.com/webhook','https://127.0.0.1/webhook','https://cursor.com.attacker.example/webhook','https://a:b@cursor.com/webhook','https://api.cursor.com:8443/webhook','https://api.cursor.com/webhook#fragment'):
            with self.assertRaises(ValueError):webhook_url(url)
        self.assertEqual(webhook_url('https://api.cursor.com/routines/webhook'),'https://api.cursor.com/routines/webhook')
    def test_dns_is_validated_before_socket_creation(self):
        with patch('platform_adapters.socket.getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]),patch('platform_adapters.socket.create_connection') as connect:
            with self.assertRaises(RemoteError):HTTPS().open('POST','https://api.cursor.com/test',{'Authorization':KEY},b'{}')
            connect.assert_not_called()
    def test_shared_credential_file_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'secret.json';path.write_text(json.dumps({'api_key':KEY}));path.chmod(0o644)
            with self.assertRaises(ValueError):secret_file(path)


if __name__=='__main__':unittest.main()
