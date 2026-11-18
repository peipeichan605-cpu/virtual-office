"""Synthetic Cursor SDK contracts; never an authenticated model/App test."""
from security_keys import owner_key
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace as Obj
import unittest
from unittest.mock import patch
import uuid
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from commands import Commands
from configuration import load_config
from connections import ConnectionError
from cursor_local import LocalStartError, SdkRuntime, public_event, tool_restrictions, lifecycle_path, SessionLifecycle, durable_state
from dashboard import Bridge, proxy_handler
from hermes_acp import private_json
import test_platforms as fixtures

KEY = 'fixture-sdk-key-private-value'


def assistant(text, ident=None):
    return Obj(type='assistant', message=Obj(id=ident, content=[
        Obj(type='thinking', text='REASONING_MUST_NOT_SAVE'), Obj(type='text', text=text)]))


class FakeClient:
    def __init__(self, fail=False):
        self.closed, self.fail = False, fail

    def close(self):
        self.closed = True
        if self.fail:
            raise RuntimeError('fixture bridge already closed')


class FakeRun:
    def __init__(self, number):
        self.id = 'sdk-run-' + str(number)
        self.status = 'finished'
        self.result = 'Delivered SDK round ' + str(number)
        self.events = [Obj(type='thinking', text='REASONING_MUST_NOT_SAVE'),
            assistant('Working'), assistant(' further'),
            Obj(type='tool_call',name='read_file',call_id='not-a-child'),
            assistant('ha'), assistant('ha'),
            Obj(type='tool_call',name='read_file',call_id='not-a-child-either'), assistant('tool'),
            Obj(type='tool_call', name='task', call_id='not-a-verified-child',
                args={'text':'RAW_TOOL_MUST_NOT_SAVE'}, result='RAW_RESULT_MUST_NOT_SAVE')]
        self.interrupted = False

    def messages(self):
        yield from self.events
        if self.interrupted:
            raise OSError('fixture connection closed')

    def wait(self):
        return self


class FakeRuntime:
    reason = 'cursor_sdk_required'

    def __init__(self):
        self.enabled, self.created, self.sent = True, [], []
        self.creation_error = self.send_error = None
        self.interrupted = self.close_error = False
        self.gate = self.entered = None

    def available(self):
        return self.enabled

    def reserve(self, key, model, cwd, policy, request_id=None, name=None):
        self.created.append({'key':key,'model':model,'cwd':cwd,'policy':policy,'request_id':request_id})
        if self.entered:
            self.entered.set()
        if self.gate:
            self.gate.wait(2)
        if self.creation_error:
            raise self.creation_error
        return 'agent-sdkfixture-' + str(len(self.created))

    def send(self, key, model, cwd, agent, text, policy, command_id=None, initialize=False, on_created=None, name=None):
        self.sent.append({'key':key,'model':model,'cwd':cwd,'agent':agent,'text':text,'policy':policy,'command_id':command_id,'initialize':initialize})
        if initialize:
            on_created(agent)
        if self.send_error:
            raise self.send_error
        run = FakeRun(len(self.sent)); run.interrupted = self.interrupted
        return run, FakeClient(self.close_error)


class CursorLocalTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp
    tearDown = fixtures.PlatformTests.tearDown
    submit = fixtures.PlatformTests.submit
    wait = fixtures.PlatformTests.wait

    def runtime(self):
        if not hasattr(self, 'fake'):
            self.fake = FakeRuntime()
            self.bridge.connections.local_runtime = self.fake
            self.bridge.commands.adapters['cursor_local'].runtime = self.fake
        return self.fake

    def creation(self, **extra):
        self.runtime()
        return {'name':'SDK fixture','workspace':str(self.root),'api_key':KEY,
                'request_id':str(uuid.uuid4()), **extra}

    def connect_sdk(self, **extra):
        data = self.creation(**extra)
        return self.bridge.connections.create_cursor_local(data)

    def test_create_is_one_explicit_session_and_preserves_cloud_credentials(self):
        legacy = self.root/'platform-secrets/cursor.json'
        private_json(legacy, {'api_key':'legacy-cloud-private-key'})
        before = legacy.read_bytes()
        created = self.connect_sdk()
        self.assertEqual(legacy.read_bytes(), before)
        self.assertEqual(len(self.bridge.agents), 1)
        config = load_config(self.path)
        session = config['command_adapters']['cursor_local']['sessions'][created['agent_id'].split(':',1)[1]]
        self.assertEqual(tool_restrictions(session), {'tools':[], 'disallowed_tools':[], 'setting_sources':[]})
        self.assertEqual(session['cwd'], str(self.root))
        self.assertEqual(Path(session['credential_file']).stat().st_mode&0o777, 0o600)
        self.assertEqual((self.root/'platform-secrets/cursor-sdk.json').stat().st_mode&0o777, 0o600)
        self.assertNotIn(KEY, self.path.read_text())
        self.assertNotIn(KEY, json.dumps(created))
        self.assertEqual(created['scope'], 'office_owned_session')
        self.assertEqual(created['tool_policy'], 'chat_only')
        self.assertFalse(created['execution_verified'])
        self.assertFalse(created['native_target_verified'])
        self.assertEqual(self.fake.sent, [], 'registration never creates a queued native bootstrap')
        self.assertEqual(SessionLifecycle(session,created['agent_id'].split(':',1)[1]).read()['phase'],'reserved')
        status = self.bridge.connections.cursor_local_status()
        self.assertTrue(status['credential_configured'])
        self.assertNotIn(KEY, json.dumps(status))
        capability = self.bridge.commands.capability(created['agent_id'])
        self.assertEqual(capability['public_sync']['scope'], 'office_dispatched_runs')
        self.assertEqual(capability['conversation_scope'], 'office_owned_session')
        self.assertFalse(capability['tools_enabled'])
        self.assertFalse(capability['native_target_verified'])
        self.assertIsNone(self.bridge.agents[created['agent_id']]['native_url'])

    def test_sdk_preflight_never_saves_key_or_creates_agent_when_unavailable(self):
        data = self.creation(); self.fake.enabled = False
        with self.assertRaises(ConnectionError) as error:
            self.bridge.connections.create_cursor_local(data)
        self.assertEqual(error.exception.code, 'cursor_sdk_required')
        self.assertEqual(self.fake.created, [])
        self.assertFalse((self.root/'platform-secrets/cursor-sdk.json').exists())
        self.assertEqual(len(self.bridge.agents), 0)

    def test_explicit_folder_creation_requires_existing_parent_and_passed_preflight(self):
        folder = self.root/'SDK workspace with spaces'
        data = self.creation(workspace=str(folder), create_folder=True)
        self.fake.enabled = False
        with self.assertRaises(ConnectionError): self.bridge.connections.create_cursor_local(data)
        self.assertFalse(folder.exists())
        self.fake.enabled = True
        self.bridge.connections.create_cursor_local(data)
        self.assertTrue(folder.is_dir())
        with self.assertRaises(ConnectionError):
            self.bridge.connections.create_cursor_local(self.creation(workspace=str(folder/'missing'/'nested'),create_folder=True))

    def test_nonempty_tool_policy_or_approval_bypass_is_rejected(self):
        for extra in [{'tools':['bash']}, {'setting_sources':['user']}, {'permission_mode':'bypassPermissions'}, {'mcp_servers':{'x':{}}}]:
            with self.assertRaises(ConnectionError):
                self.bridge.connections.create_cursor_local(self.creation(**extra))
        self.assertEqual(self.fake.created, [])

    def test_creation_idempotency_and_get_recovery_do_not_create_twice(self):
        data = self.creation()
        first = self.bridge.connections.create_cursor_local(data)
        second = self.bridge.connections.create_cursor_local(data)
        self.assertEqual(first, second); self.assertEqual(len(self.fake.created), 1)
        proof = self.bridge.connections.cursor_local_status(data['request_id'])['creation']
        self.assertEqual(proof['state'], 'completed'); self.assertEqual(proof['agent_id'], first['agent_id'])
        with self.assertRaises(ConnectionError) as error:
            self.bridge.connections.create_cursor_local({**data, 'name':'different request'})
        self.assertEqual(error.exception.code, 'request_id_conflict')

    def test_uncertain_creation_is_not_replayed_or_reported_completed(self):
        data = self.creation(); self.fake.creation_error = LocalStartError('cursor_local_delivery_unknown', True)
        for _ in range(2):
            with self.assertRaises(ConnectionError):
                self.bridge.connections.create_cursor_local(data)
        self.assertEqual(len(self.fake.created), 1)
        proof = self.bridge.connections.cursor_local_status(data['request_id'])['creation']
        self.assertEqual(proof['state'], 'unknown')
        self.assertNotIn('agent_id', proof)
        self.assertFalse((self.root/'platform-secrets/cursor-sdk.json').exists())

    def test_logged_out_creation_receipt_never_restores_or_claims_current_membership(self):
        data = self.creation(); created = self.bridge.connections.create_cursor_local(data)
        self.bridge.members.logout({'agent_id':created['agent_id']})
        proof = self.bridge.connections.cursor_local_status(data['request_id'])['creation']
        self.assertEqual(proof['state'], 'revoked')
        self.assertEqual(proof['reason'], 'agent_membership_changed')
        with self.assertRaises(ConnectionError) as error:
            self.bridge.connections.create_cursor_local(data)
        self.assertEqual(error.exception.code, 'agent_membership_changed')
        self.assertNotIn(created['agent_id'], self.bridge.agents)
        self.assertEqual(len(self.fake.created), 1)

    def test_cached_sdk_key_remains_redacted_after_last_member_logout_and_restart(self):
        created = self.connect_sdk()
        self.bridge.members.logout({'agent_id':created['agent_id']})
        self.bridge.commands.close(); self.bridge.history.close()
        config = load_config(self.path); config['run_id']='restarted-sdk-fixture'; config['run_dir']=str(self.root)
        self.bridge = Bridge(config)
        self.assertIn(KEY, self.bridge.history.secrets)
        self.bridge.history.message('fixture:other','redaction-fixture','assistant','commentary','Bare known key '+KEY)
        public = self.bridge.history.messages({'fixture:other'},'fixture:other')
        self.assertNotIn(KEY, json.dumps(public))
        self.assertTrue((self.root/'platform-secrets/cursor-sdk.json').is_file())

    def test_inflight_creation_status_is_pending_then_completed(self):
        data = self.creation(); self.fake.entered = threading.Event(); self.fake.gate = threading.Event()
        outcome = []
        thread = threading.Thread(target=lambda:outcome.append(self.bridge.connections.create_cursor_local(data)))
        thread.start()
        try:
            self.assertTrue(self.fake.entered.wait(1))
            proof = self.bridge.connections.cursor_local_status(data['request_id'])['creation']
            self.assertEqual(proof['state'], 'pending')
            with self.assertRaises(ConnectionError) as error:
                self.bridge.connections.create_cursor_local(data)
            self.assertEqual(error.exception.code, 'cursor_creation_pending')
        finally:
            self.fake.gate.set(); thread.join(3)
        self.assertEqual(len(outcome), 1); self.assertEqual(len(self.fake.created), 1)

    def test_two_rounds_resume_same_target_with_public_stream_and_final_history(self):
        created = self.connect_sdk(); ident = created['agent_id']
        first = self.submit(ident, 'Remember item 917 password=hidden-sdk-value')
        second = self.submit(ident, 'Continue item 917')
        self.wait(first, {'completed'}); self.wait(second, {'completed'})
        self.assertEqual(len(self.fake.sent), 2)
        self.assertEqual([turn['initialize'] for turn in self.fake.sent],[True,False])
        self.assertTrue(self.bridge.commands.capability(ident)['native_target_verified'])
        for sent in self.fake.sent:
            self.assertEqual(sent['agent'], ident.split(':',1)[1])
            self.assertEqual(sent['cwd'], str(self.root))
            self.assertEqual(sent['policy'], {'tools':[], 'disallowed_tools':[], 'setting_sources':[]})
            self.assertNotIn('[OFFICE:', sent['text'])
        messages = self.bridge.history.messages({ident}, ident)
        self.assertEqual(len([x for x in messages if x['phase']=='prompt']), 2)
        self.assertEqual(len([x for x in messages if x['phase']=='final']), 2)
        first_texts = [x['text'] for x in messages if x['run_id']==first['request_id']]
        self.assertIn('haha', first_texts, 'repeated ordered SDK chunks are appended without substring deduplication')
        self.assertIn('tool', first_texts, 'literal public assistant tool text is not a sentinel')
        self.assertIn('Working further', first_texts)
        self.assertNotIn('Working', first_texts, 'partial chunks update one stable public segment')
        public = json.dumps(messages)
        for secret in ['REASONING_MUST_NOT_SAVE','RAW_TOOL_MUST_NOT_SAVE','RAW_RESULT_MUST_NOT_SAVE',KEY,'hidden-sdk-value','[OFFICE:']:
            self.assertNotIn(secret, public)
        self.assertEqual(self.bridge.commands.db.execute('SELECT count(*) FROM commands WHERE payload IS NOT NULL').fetchone()[0], 0)
        self.assertTrue(self.bridge.commands.create(first)['duplicate'])
        self.bridge.commands.tick(); self.assertEqual(len(self.fake.sent), 2)
        self.assertEqual(self.bridge.work_snapshot()['subagent_count'], 0, 'tool call ID is not proof of a child session')

    def test_uncertain_send_blocks_followup_instead_of_cancelling_or_retrying(self):
        created = self.connect_sdk(); ident = created['agent_id']
        self.fake.interrupted = True
        first = self.submit(ident); self.wait(first, {'delivery_unknown'})
        self.submit(ident, 'Queued followup must wait')
        for _ in range(3): self.bridge.commands.tick()
        self.assertEqual(len(self.fake.sent), 1)

    def test_fragmented_sdk_reply_is_one_cumulative_final_message_without_chunk_rows(self):
        created=self.connect_sdk();ident=created['agent_id'];original=self.fake.send
        marker='OFFICE_CURSOR_SDK_fixture_V2_A_OK'
        def fragmented(*args,**kwargs):
            run,client=original(*args,**kwargs)
            run.events=[Obj(type='thinking',text='PRIVATE_REASONING_FRAGMENT')]+[
                assistant(chunk) for chunk in ['OFF','ICE_CURSOR_SDK_','fixture_V','2_A_OK']]
            run.result=marker
            return run,client
        self.fake.send=fragmented
        request=self.submit(ident);self.wait(request,{'completed'})
        messages=[message for message in self.bridge.history.messages({ident},ident)
                  if message['role']=='assistant']
        self.assertEqual([(message['phase'],message['text']) for message in messages],[('final',marker)])
        self.assertEqual(self.bridge.sources['SDK fixture']['status'],'done')
        self.assertNotIn('PRIVATE_REASONING_FRAGMENT',json.dumps(messages))

    def sdk_lifecycle(self, ident):
        sid=ident.split(':',1)[1]
        return SessionLifecycle(self.bridge.commands.adapters['cursor_local'].sessions[sid],sid)

    def test_native_create_then_presend_failure_stays_unknown_and_visible_without_fake_start(self):
        created=self.connect_sdk();ident=created['agent_id'];lifecycle=self.sdk_lifecycle(ident)
        self.fake.send_error=LocalStartError('platform_auth_required',False,True)
        first=self.submit(ident,'Only one explicit attempt password=must-hide')
        row=self.wait(first,{'delivery_unknown'})
        native=self.bridge.commands.db.execute('SELECT native_message_id FROM commands WHERE id=?',(first['request_id'],)).fetchone()[0]
        self.assertIsNone(native)
        self.assertEqual(lifecycle.read()['phase'],'unknown')
        self.assertEqual(self.bridge.sources['SDK fixture']['status'],'waiting_user')
        records=self.bridge.history.list({ident},ident)
        self.assertEqual(records[0]['status'],'unknown')
        self.assertNotIn('must-hide',json.dumps(records))
        self.assertNotIn('working',[item['status'] for item in records])
        self.submit(ident,'Never retry the uncertain bootstrap')
        self.bridge.commands.tick()
        self.assertEqual(len(self.fake.sent),1)

    def test_durable_initialization_fence_precedes_runtime_and_blocks_all_cold_crash_phases(self):
        created=self.connect_sdk();ident=created['agent_id'];lifecycle=self.sdk_lifecycle(ident)
        original=self.fake.send
        def guarded(*args,**kwargs):
            state=lifecycle.read()
            self.assertEqual(state['phase'],'initializing')
            self.assertEqual(state['command_id'],kwargs['command_id'])
            return original(*args,**kwargs)
        self.fake.send=guarded
        first=self.submit(ident);self.wait(first,{'completed'})
        self.assertEqual(lifecycle.read()['phase'],'ready')
        self.fake.send=original
        adapter=self.bridge.commands.adapters['cursor_local']
        for phase in ('initializing','native_created','sending','accepted','unknown'):
            state=lifecycle.read();state['phase']=phase;durable_state(lifecycle.path,state)
            self.assertFalse(adapter.available(ident.split(':',1)[1]),phase)
        lifecycle.path.unlink()
        self.assertFalse(adapter.available(ident.split(':',1)[1]),'legacy native bootstrap cannot be silently adopted')
        self.assertEqual(adapter.reason,'cursor_legacy_session_review_required')

    def test_second_turn_after_cold_restart_resumes_only_verified_terminal_native_target(self):
        created=self.connect_sdk();ident=created['agent_id']
        first=self.submit(ident);self.wait(first,{'completed'})
        self.bridge.commands.close();self.bridge.history.close()
        config=load_config(self.path);config['run_id']='sdk-cold-restart';config['run_dir']=str(self.root)
        self.bridge=Bridge(config)
        self.bridge.commands.adapters['cursor_local'].runtime=self.fake
        second=self.submit(ident);self.wait(second,{'completed'})
        self.assertEqual([turn['initialize'] for turn in self.fake.sent],[True,False])
        self.assertEqual(len(self.fake.created),1)

    def test_restart_does_not_repeat_inflight_local_send(self):
        created = self.connect_sdk(); ident = created['agent_id']
        request = self.submit(ident)
        self.bridge.commands.db.execute("UPDATE commands SET state='running',native_message_id='sdk-recorded',run_id=? WHERE id=?", (request['request_id'],request['request_id']))
        self.bridge.commands.db.commit(); self.bridge.commands.close()
        config = load_config(self.path)
        self.bridge.commands = Commands(config, self.bridge.history)
        self.bridge.commands.adapters['cursor_local'].runtime = self.fake
        state = self.bridge.commands.list(ident)['commands'][0]
        self.assertEqual(state['state'], 'delivery_unknown')
        self.submit(ident, 'Must wait after restart'); self.bridge.commands.tick()
        self.assertEqual(self.fake.sent, [])

    def test_accepted_receipt_survives_observer_startup_failure(self):
        created = self.connect_sdk(); request = self.submit(created['agent_id'])
        with patch('cursor_local.threading.Thread.start', side_effect=RuntimeError('fixture thread unavailable')):
            self.bridge.commands.tick()
        row = self.bridge.commands.db.execute('SELECT * FROM commands WHERE id=?',(request['request_id'],)).fetchone()
        self.assertEqual(row['state'], 'delivery_unknown')
        self.assertEqual(row['native_message_id'], 'sdk-run-1')
        self.bridge.commands.tick(); self.assertEqual(len(self.fake.sent), 1)

    def test_cleanup_failure_cannot_strand_worker_or_erase_delivery(self):
        created = self.connect_sdk(); ident = created['agent_id']; self.fake.close_error = True
        first = self.submit(ident); self.wait(first, {'completed'})
        end = time.monotonic()+1
        adapter = self.bridge.commands.adapters['cursor_local']
        while not adapter.can_dispatch(ident.split(':',1)[1]) and time.monotonic()<end:
            time.sleep(.005)
        second = self.submit(ident, 'Another valid turn'); self.wait(second, {'completed'})
        self.assertEqual(len(self.fake.sent), 2)

    def test_owner_auth_required_for_status_and_creation(self):
        data = self.creation()
        server = ThreadingHTTPServer(('127.0.0.1',0), proxy_handler(self.bridge))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.bridge.config['port'] = server.server_address[1]
        base = 'http://127.0.0.1:'+str(server.server_address[1])
        try:
            for route, payload in [('/api/office/cursor/local/status',None),('/api/office/cursor/local/create',data)]:
                req = urllib.request.Request(base+route, data=json.dumps(payload).encode() if payload else None,
                    headers={'Content-Type':'application/json'})
                with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(req,timeout=2)
                self.assertEqual(error.exception.code,403)
            req = urllib.request.Request(base+'/api/office/cursor/local/status', headers={'X-Office-Key':owner_key(self.bridge.config)})
            with urllib.request.urlopen(req,timeout=2) as response: self.assertTrue(json.load(response)['sdk_available'])
            self.assertEqual(self.fake.created, [])
            req = urllib.request.Request(base+'/api/office/cursor/local/create', data=json.dumps(data).encode(),
                headers={'Content-Type':'application/json','X-Office-Key':owner_key(self.bridge.config)})
            with urllib.request.urlopen(req,timeout=2) as response:
                created = json.load(response)
            self.assertEqual(created['scope'], 'office_owned_session')
            self.assertNotIn(KEY, json.dumps(created))
            req = urllib.request.Request(base+'/api/office/cursor/local/status?request_id='+data['request_id'],
                headers={'X-Office-Key':owner_key(self.bridge.config)})
            with urllib.request.urlopen(req,timeout=2) as response:
                proof = json.load(response)['creation']
            self.assertEqual(proof['state'], 'completed')
            self.assertEqual(proof['agent_id'], created['agent_id'])
            self.assertEqual(len(self.fake.created), 1)
        finally:
            server.shutdown(); server.server_close(); thread.join(2)


class PublicSdkContractTests(unittest.TestCase):
    def test_unsupported_windows_abi_reports_platform_before_importing_sdk(self):
        runtime = SdkRuntime()
        runtime.version = '1.3.1'
        with patch('cursor_local.sys.version_info', (3, 12)), \
             patch('cursor_sdk_setup.sdk_platform_supported', return_value=False), \
             patch('cursor_local.importlib.util.find_spec') as lookup:
            self.assertFalse(runtime.available())
        self.assertEqual(runtime.reason, 'cursor_sdk_platform_unsupported')
        self.assertIsNone(runtime.version)
        lookup.assert_not_called()

    def test_preflight_rejects_untested_sdk_versions_without_starting_a_bridge(self):
        with patch('cursor_local.sys.version_info',(3,12)), patch('cursor_local.importlib.util.find_spec',return_value=Obj()):
            runtime = SdkRuntime()
            with patch('cursor_local.importlib.metadata.version',return_value='2.0.0'):
                self.assertFalse(runtime.available())
                self.assertEqual(runtime.reason,'cursor_sdk_version_unsupported')
            with patch('cursor_local.importlib.metadata.version',return_value='1.3.1'):
                self.assertTrue(runtime.available())
        with patch('cursor_local.sys.version_info',(3,9)):
            self.assertFalse(runtime.available())
            self.assertEqual(runtime.reason,'cursor_python_required')

    def test_only_typed_assistant_text_is_public(self):
        self.assertIsNone(public_event(Obj(type='thinking', text='not public')))
        self.assertIsNone(public_event(Obj(interaction_update=Obj(type='text-delta', text='unverified delta'))))
        self.assertEqual(public_event(assistant('tool'))['text'], 'tool')

    def test_runtime_uses_public_options_no_private_events_or_bridge_patch(self):
        calls = []
        class Options:
            def __init__(self, **kwargs): self.__dict__.update(kwargs)
        class LocalOptions:
            def __init__(self, *, cwd, setting_sources):
                self.cwd, self.setting_sources = cwd, setting_sources
        client = FakeClient()
        run = FakeRun(1)
        class AgentHandle:
            agent_id = 'agent-public-options'
            def send(self, text): calls.append(('send',text)); return run
        class Agent:
            @staticmethod
            def create(options, *, client): calls.append(('create', options)); return AgentHandle()
            @staticmethod
            def resume(ident, options, *, client): calls.append(('resume', ident, options)); return AgentHandle()
        class Client:
            @staticmethod
            def launch_bridge(**kwargs): calls.append(('bridge',kwargs)); return client
        module = Obj(Agent=Agent, AgentOptions=Options, CursorClient=Client, LocalAgentOptions=LocalOptions)
        with patch.dict('sys.modules', {'cursor_sdk':module}):
            runtime = SdkRuntime()
            self.assertTrue(runtime.reserve(KEY,'model-fixture','/fixture',request_id='create-id').startswith('agent-'))
            self.assertEqual(calls, [], 'registration cannot create a native queued bootstrap')
            runtime.send(KEY,'model-fixture','/fixture','agent-public-options','first',command_id='command-first',
                initialize=True,on_created=lambda ident:calls.append(('journal',ident)),name='Fixture name')
            runtime.send(KEY,'model-fixture','/fixture','agent-public-options','hello',command_id='command-id')
        for call in calls:
            if call[0]=='bridge':
                self.assertEqual(call[1], {'workspace':'/fixture','host':'127.0.0.1','max_retries':0})
            elif call[0] in {'create','resume'}:
                options = call[1] if call[0]=='create' else call[2]
                self.assertEqual(options.tools, []); self.assertEqual(options.disallowed_tools, [])
                self.assertEqual(options.local.setting_sources, [])
                self.assertEqual(options.local.cwd, '/fixture')
        self.assertIn(('send','hello'), calls)
        kinds=[item[0] for item in calls]
        self.assertLess(kinds.index('create'),kinds.index('journal'))
        self.assertLess(kinds.index('journal'),kinds.index('send'))
        created=next(item[1] for item in calls if item[0]=='create')
        self.assertEqual(created.agent_id,'agent-public-options')
        self.assertEqual(created.name,'Fixture name')


if __name__ == '__main__':
    unittest.main()
