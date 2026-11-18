"""Deterministic failure, identity, privacy and concurrency tests; no inference."""
import json
import os
import queue
import tempfile
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import codex_background
from codex_background import (CodexBackgroundAdapter, DISABLED_FEATURES,
    _disabled_integrations, durable_state, classify_turn_error, _model_catalog)
from codex_rpc import RpcError, StdioRpc, child_environment
from command_adapters import DeliveryError


def until(condition, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError('condition timed out')


class FakeRpc:
    def __init__(self, owner, binary, cwd, overrides):
        self.owner, self.cwd, self.overrides = owner, cwd, overrides
        self.closed = threading.Event()
        self.events = queue.Queue()
        self.calls, self.replies = [], []
        self.thread_id = 'native-thread'
        owner.clients.append(self)

    def initialize(self):
        if self.owner.failure == 'initialize':
            raise RpcError('codex_process_unavailable')

    def call(self, method, params, timeout=20):
        self.calls.append((method, params))
        if self.owner.failure == method:
            raise RpcError('codex_rpc_timeout', sent=True)
        if method == 'account/read':
            return {'account': {'type': 'chatgpt'}}
        if method == 'config/read':
            features = {key: False for key in DISABLED_FEATURES}
            if self.owner.bad_policy:
                features['plugins'] = True
            return {'config': {'features': features, 'web_search': 'disabled',
                               'model': 'configured-model'}}
        if method in {'thread/start', 'thread/resume'}:
            self.owner.native_creates += method == 'thread/start'
            return {'thread': {'id': self.thread_id}, 'model': params.get('model', 'configured-model'),
                'modelProvider': 'openai', 'sandbox': {'type': 'readOnly'},
                'approvalPolicy': 'on-request', 'approvalsReviewer': 'user', 'cwd': self.cwd}
        if method == 'turn/start':
            self.owner.native_turns += 1
            return {'turn': {'id': 'turn-' + str(self.owner.native_turns)}}
        raise AssertionError(method)

    def reply(self, ident, result=None, error=None):
        self.replies.append({'id': ident, 'result': result, 'error': error})

    def close(self):
        self.closed.set()

    def notice(self, method, turn='turn-1', thread='native-thread', **params):
        self.events.put({'method': method, 'params': dict(threadId=thread, turnId=turn, **params)})

    def complete(self, turn='turn-1', items=()):
        self.events.put({'method': 'turn/completed', 'params': {'threadId': self.thread_id,
            'turn': {'id': turn, 'status': 'completed', 'items': list(items)}}})


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sid = 'bg-' + str(uuid.uuid4())
        self.path = self.root / (self.sid + '.json')
        durable_state(self.path, {'schema': 1, 'session_id': self.sid, 'cwd': str(self.root),
            'phase': 'reserved', 'native_thread_id': None})
        self.config = {'command_adapters': {'codex_background': {'binary': '/usr/bin/true',
            'sessions': {self.sid: {'cwd': str(self.root), 'lifecycle_file': str(self.path),
                'sandbox': 'read-only', 'approval_policy': 'on-request'}}}}}
        self.clients, self.events = [], []
        self.failure, self.bad_policy = None, False
        self.native_creates = self.native_turns = 0
        self.adapter = self.new_adapter()

    def new_adapter(self, emit=None):
        return CodexBackgroundAdapter(self.config, emit or (lambda agent, event: self.events.append(event)),
            rpc_factory=lambda *args: FakeRpc(self, *args),
            probe_fn=lambda _: {'available': True, 'logged_in': True})

    def tearDown(self):
        self.adapter.close()
        self.temp.cleanup()

    def submit(self, command=None):
        command = command or str(uuid.uuid4())
        return self.adapter.submit(self.sid, '[OFFICE:' + command + ']\nhello'), command

    def state(self):
        return json.loads(self.path.read_text())

    def finish(self, text='done', turn='turn-1'):
        self.clients[-1].complete(turn, [{'type': 'agentMessage', 'id': 'final-' + turn, 'text': text}])
        until(lambda: any(event['type'] == 'finished' and event['native_run_id'] == turn for event in self.events))

    def test_status_does_not_create_native_thread(self):
        for _ in range(5):
            self.assertTrue(self.adapter.available(self.sid))
            self.assertTrue(self.adapter.can_dispatch(self.sid))
        self.assertFalse(self.adapter.native_verified(self.sid))
        self.assertEqual(self.clients, [])

    def test_receipt_precedes_public_events_and_model_is_preserved(self):
        receipt, command = self.submit()
        self.assertEqual(receipt['native_message_id'], 'turn-1')
        self.finish()
        self.assertEqual([event['type'] for event in self.events][:2], ['accepted', 'started'])
        calls = dict(self.clients[0].calls)
        self.assertNotIn('model', calls['thread/start'])
        self.assertEqual(calls['turn/start']['clientUserMessageId'], command)
        self.assertEqual(calls['turn/start']['input'][0]['text'], 'hello')
        self.assertEqual(calls['turn/start']['sandboxPolicy'], {'type': 'readOnly', 'networkAccess': False})
        self.submit()
        self.finish(turn='turn-2')
        resumes = [params for name, params in self.clients[0].calls if name == 'thread/resume']
        self.assertEqual(resumes[0]['threadId'], 'native-thread')
        self.assertEqual(resumes[0]['model'], 'configured-model')
        self.assertEqual(self.native_creates, 1)
        self.assertTrue(self.adapter.native_verified(self.sid))
        journal = self.path.parent / (self.sid + '-turns') / (command + '.json')
        self.assertNotIn('hello', journal.read_text())

    def test_healthy_owned_worker_status_never_spawns_probe(self):
        self.submit()
        self.adapter.probe_fn = lambda _: (_ for _ in ()).throw(AssertionError('unnecessary probe'))
        self.assertTrue(self.adapter.available(self.sid))
        self.finish()
        self.assertTrue(self.adapter.available(self.sid))

    def test_public_segments_cumulative_and_foreign_private_events_ignored(self):
        self.submit()
        rpc = self.clients[-1]
        rpc.notice('item/agentMessage/delta', itemId='public', delta='no', thread='foreign')
        rpc.notice('item/agentMessage/delta', itemId='public', delta='no', turn='foreign')
        rpc.notice('item/reasoning/textDelta', itemId='private', delta='hidden reasoning')
        rpc.notice('item/completed', item={'type': 'mcpToolCall', 'id': 'secret', 'result': 'secret body'})
        rpc.notice('item/agentMessage/delta', itemId='public', delta='Hel')
        rpc.notice('item/agentMessage/delta', itemId='public', delta='lo')
        rpc.notice('item/completed', item={'type': 'agentMessage', 'id': 'public', 'text': 'Hello'})
        rpc.notice('item/agentMessage/delta', itemId='public', delta='late')
        self.finish('Final')
        texts = [e for e in self.events if e['type'] == 'text']
        self.assertEqual([e['text'] for e in texts], ['Hel', 'Hello', 'Hello', 'Final'])
        self.assertEqual([e['segment'] for e in texts], [0, 0, 0, 1])
        self.assertNotIn('hidden reasoning', str(self.events))
        self.assertNotIn('secret body', str(self.events))

    def test_terminal_snapshot_last_public_item_is_authoritative(self):
        self.submit()
        rpc = self.clients[-1]
        rpc.notice('item/completed', item={'type': 'agentMessage', 'id': 'answer', 'text': 'final answer'})
        rpc.notice('item/completed', item={'type': 'agentMessage', 'id': 'progress', 'text': 'old progress'})
        rpc.complete(items=[{'type': 'agentMessage', 'id': 'progress', 'text': 'old progress'},
                            {'type': 'agentMessage', 'id': 'answer', 'text': 'final answer'}])
        until(lambda: any(e['type'] == 'finished' for e in self.events))
        self.assertEqual(self.events[-1]['text'], 'final answer')

    def test_completed_commentary_is_not_delivery_before_final_answer(self):
        self.submit()
        rpc = self.clients[-1]
        commentary = {'type': 'agentMessage', 'id': 'progress', 'phase': 'commentary',
                      'text': 'I am checking the workspace.'}
        answer = {'type': 'agentMessage', 'id': 'answer', 'phase': 'final_answer',
                  'text': 'Checked. The result is 42.'}
        rpc.notice('item/started', item=dict(commentary, text=''))
        rpc.notice('item/agentMessage/delta', itemId='progress', delta=commentary['text'])
        rpc.notice('item/completed', item=commentary)
        until(lambda: len([event for event in self.events if event['type'] == 'text']) == 2)
        self.assertTrue(all(not event['final'] for event in self.events if event['type'] == 'text'))
        self.assertEqual(self.state()['phase'], 'running')
        rpc.notice('item/agentMessage/delta', itemId='progress', delta='ignored late delta')
        rpc.notice('item/started', item=dict(answer, text=''))
        rpc.notice('item/agentMessage/delta', itemId='answer', delta=answer['text'])
        rpc.notice('item/completed', item=answer)
        until(lambda: any(event['type'] == 'text' and event['final'] for event in self.events))
        before = len([event for event in self.events if event['type'] == 'text'])
        rpc.complete(items=[commentary, answer])
        until(lambda: any(event['type'] == 'finished' for event in self.events))
        texts = [event for event in self.events if event['type'] == 'text']
        self.assertEqual(len(texts), before, 'terminal snapshot must not duplicate sealed items')
        self.assertEqual([event['text'] for event in texts if event['final']], [answer['text']])
        self.assertNotIn('ignored late delta', str(self.events))

    def test_started_phase_is_kept_when_completed_phase_is_absent(self):
        self.submit()
        rpc = self.clients[-1]
        rpc.notice('item/started', item={'type': 'agentMessage', 'id': 'known',
                                        'phase': 'commentary', 'text': ''})
        rpc.notice('item/completed', item={'type': 'agentMessage', 'id': 'known', 'text': 'Still working'})
        until(lambda: any(event['type'] == 'text' for event in self.events))
        rpc.complete()
        until(lambda: any(event['type'] == 'finished' for event in self.events))
        self.assertTrue(all(not event['final'] for event in self.events if event['type'] == 'text'))

    def test_legacy_unknown_phase_only_last_item_becomes_delivery_at_turn_end(self):
        self.submit()
        rpc = self.clients[-1]
        rpc.notice('item/completed', item={'type': 'agentMessage', 'id': 'one', 'text': 'Progress'})
        rpc.notice('item/completed', item={'type': 'agentMessage', 'id': 'two', 'text': 'Answer'})
        until(lambda: len([event for event in self.events if event['type'] == 'text']) == 2)
        self.assertTrue(all(not event['final'] for event in self.events if event['type'] == 'text'))
        rpc.complete()
        until(lambda: any(event['type'] == 'finished' for event in self.events))
        finals = [event for event in self.events if event['type'] == 'text' and event['final']]
        self.assertEqual([(event['segment'], event['text']) for event in finals], [(1, 'Answer')])

    def test_model_rejection_is_failed_with_safe_delivery_and_no_retry(self):
        self.submit()
        raw = {'type': 'error', 'status': 400, 'error': {
            'code': 'model_not_found', 'type': 'invalid_request_error',
            'message': 'unsupported model; token=private-provider-material'}}
        self.clients[-1].events.put({'method': 'turn/completed', 'params': {
            'threadId': 'native-thread', 'turn': {'id': 'turn-1', 'status': 'failed',
                'items': [], 'error': {'message': json.dumps(raw), 'codexErrorInfo': 'other'}}}})
        until(lambda: any(event['type'] == 'finished' for event in self.events))
        last = self.events[-1]
        self.assertEqual(last['state'], 'failed')
        self.assertEqual(last['detail_code'], 'codex_model_unavailable')
        self.assertIn('未有自動更換模型', last['text'])
        self.assertNotIn('private-provider-material', str(self.events))
        self.assertNotIn('private-provider-material', self.path.read_text())
        self.assertEqual(self.state()['phase'], 'ready')
        self.assertEqual(self.state()['last_outcome'], 'failed')
        self.assertEqual(self.state()['model'], 'configured-model')
        self.assertEqual(self.native_turns, 1)
        self.assertEqual([event['type'] for event in self.events][:2], ['accepted', 'started'])

    def test_pre_dispatch_failure_does_not_poison_reservation(self):
        self.failure = 'initialize'
        with self.assertRaises(DeliveryError) as caught:
            self.submit()
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(self.state()['phase'], 'reserved')
        self.assertEqual(self.native_creates, 0)

    def test_uncertain_create_blocks_replay(self):
        self.failure = 'thread/start'
        with self.assertRaises(DeliveryError) as caught:
            self.submit()
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(self.state()['phase'], 'unknown')
        self.assertFalse(self.adapter.can_dispatch(self.sid))
        with self.assertRaises(DeliveryError):
            self.submit()
        self.assertEqual(len(self.clients), 1)

    def test_uncertain_turn_blocks_replay_and_retains_thread(self):
        self.failure = 'turn/start'
        with self.assertRaises(DeliveryError) as caught:
            self.submit()
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(self.state()['native_thread_id'], 'native-thread')
        self.assertEqual(self.state()['phase'], 'unknown')
        self.assertEqual(self.events, [])

    def test_process_crash_records_unknown_and_restart_does_not_retry(self):
        self.submit()
        self.clients[-1].closed.set()
        until(lambda: self.state()['phase'] == 'unknown')
        self.adapter.close()
        self.adapter = self.new_adapter()
        self.assertFalse(self.adapter.can_dispatch(self.sid))
        self.assertEqual(self.native_turns, 1)

    def test_duplicate_command_is_never_replayed(self):
        _, command = self.submit()
        self.finish()
        with self.assertRaises(DeliveryError) as caught:
            self.submit(command)
        self.assertEqual(caught.exception.code, 'codex_command_already_attempted')
        self.assertEqual(self.native_turns, 1)

    def test_ready_restart_resumes_same_thread(self):
        self.submit()
        self.finish()
        self.adapter.close()
        self.adapter = self.new_adapter()
        self.submit()
        self.finish(turn='turn-2')
        self.assertEqual(self.native_creates, 1)
        self.assertTrue(any(name == 'thread/resume' for name, _ in self.clients[-1].calls))

    def test_one_time_approval_and_stale_decision(self):
        self.submit()
        rpc = self.clients[-1]
        rpc.events.put({'id': 42, 'method': 'item/commandExecution/requestApproval', 'params': {
            'threadId': 'native-thread', 'turnId': 'turn-1', 'itemId': 'exec', 'command': 'pwd'}})
        until(lambda: bool(self.adapter.approvals(self.sid)))
        approval = self.adapter.approvals(self.sid)[0]
        self.assertFalse(self.adapter.decide(self.sid, approval['id'], 'always_allow'))
        self.assertTrue(self.adapter.decide(self.sid, approval['id'], 'allow_once'))
        self.assertEqual(rpc.replies[-1]['result'], {'decision': 'accept'})
        self.assertFalse(self.adapter.decide(self.sid, approval['id'], 'allow_once'))
        self.finish()

    def test_unknown_and_foreign_requests_fail_closed(self):
        self.submit()
        rpc = self.clients[-1]
        rpc.events.put({'id': 1, 'method': 'newPermissionMethod', 'params': {}})
        rpc.events.put({'id': 2, 'method': 'item/fileChange/requestApproval', 'params': {
            'threadId': 'foreign', 'turnId': 'turn-1', 'itemId': 'file'}})
        until(lambda: len(rpc.replies) == 2)
        self.assertEqual(rpc.replies[0]['error']['code'], -32601)
        self.assertEqual(rpc.replies[1]['result'], {'decision': 'decline'})
        self.assertEqual(self.adapter.approvals(self.sid), [])
        self.finish()

    def test_session_wide_file_grant_cannot_be_approved(self):
        self.submit()
        rpc = self.clients[-1]
        rpc.events.put({'id': 3, 'method': 'item/fileChange/requestApproval', 'params': {
            'threadId': 'native-thread', 'turnId': 'turn-1', 'itemId': 'file', 'grantRoot': '/tmp'}})
        until(lambda: bool(self.adapter.approvals(self.sid)))
        approval = self.adapter.approvals(self.sid)[0]
        self.assertFalse(approval['can_allow'])
        self.assertFalse(self.adapter.decide(self.sid, approval['id'], 'allow_once'))
        self.assertTrue(self.adapter.decide(self.sid, approval['id'], 'reject_once'))
        self.finish()

    def test_logout_fences_late_events_and_closes_only_owned_client(self):
        self.submit()
        until(lambda: len(self.events) >= 2)
        self.adapter.sync_config({'command_adapters': {}})
        count = len(self.events)
        self.clients[-1].complete(items=[{'type': 'agentMessage', 'id': 'late', 'text': 'late'}])
        time.sleep(0.3)
        self.assertEqual(len(self.events), count)
        self.assertTrue(self.clients[-1].closed.is_set())
        self.assertEqual(self.state()['phase'], 'unknown')

    def test_malformed_lifecycle_is_unavailable(self):
        self.path.write_text('[]')
        self.assertFalse(self.adapter.available(self.sid))
        self.assertEqual(self.adapter.reason, 'codex_background_lifecycle_invalid')

    def test_unenforced_tool_policy_prevents_native_thread(self):
        self.bad_policy = True
        with self.assertRaises(DeliveryError) as caught:
            self.submit()
        self.assertEqual(caught.exception.code, 'codex_background_tool_policy_unverified')
        self.assertEqual(self.native_creates, 0)

    def test_callbacks_can_query_adapter_without_lock_inversion(self):
        self.adapter.close()
        callback_lock = threading.Lock()
        entered = threading.Event()
        def emit(_, event):
            entered.set()
            with callback_lock:
                self.adapter.approvals(self.sid)
                self.events.append(event)
        self.adapter = self.new_adapter(emit)
        with callback_lock:
            worker = threading.Thread(target=self.submit, daemon=True)
            worker.start()
            self.assertTrue(entered.wait(2))
            self.assertTrue(self.adapter.available(self.sid))
            self.adapter.approvals(self.sid)
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.finish()

    def test_projection_failure_marks_durable_unknown(self):
        self.adapter.close()
        def fail(*_):
            raise OSError('simulated database failure')
        self.adapter = self.new_adapter(fail)
        self.submit()
        until(lambda: self.state()['phase'] == 'unknown')
        self.assertFalse(self.adapter.can_dispatch(self.sid))

    def test_cross_process_lease_prevents_second_office(self):
        self.submit()
        second = self.new_adapter()
        try:
            with self.assertRaises(DeliveryError):
                second.submit(self.sid, '[OFFICE:' + str(uuid.uuid4()) + ']\nhello')
            self.assertEqual(self.native_creates, 1)
        finally:
            second.close()
        self.finish()

    def test_environment_scrubs_caller_identity_and_keeps_auth_location(self):
        with patch.dict(os.environ, {'CODEX_THREAD_ID': 'parent', 'CODEX_TURN_ID': 'turn',
                'CODEX_HOME': '/private/codex', 'PATH': '/usr/bin:/bin'}):
            env = child_environment()
            self.assertNotIn('CODEX_THREAD_ID', env)
            self.assertNotIn('CODEX_TURN_ID', env)
            self.assertEqual(env['CODEX_HOME'], '/private/codex')
            if Path('/opt/homebrew/opt/node@22/bin/node').exists():
                self.assertIn('/opt/homebrew/opt/node@22/bin', env['PATH'])

    def test_disabled_mcp_plugin_keys_use_nested_maps(self):
        overrides = _disabled_integrations({'mcp_servers': {'dots.and"quotes': {}},
                                             'plugins': {'name@market': {}}})
        self.assertEqual(overrides[0], 'mcp_servers={"dots.and\\"quotes"={enabled=false}}')
        self.assertEqual(overrides[1], 'plugins={"name@market"={enabled=false}}')


class StdioTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.client = None

    def tearDown(self):
        if self.client:
            self.client.close()
        self.temp.cleanup()

    def make_client(self, body):
        script = self.root / 'fake-codex'
        script.write_text('#!' + sys.executable + '\n' + body)
        script.chmod(0o700)
        self.client = StdioRpc(str(script), str(self.root))
        return self.client

    def test_actual_stdio_framing_and_owned_cleanup(self):
        client = self.make_client('''import json,sys
assert sys.argv[1:] == ['app-server', '--listen', 'stdio://']
for line in sys.stdin:
    value=json.loads(line)
    if 'id' in value:
        print(json.dumps({'id':value['id'],'result':{'echo':value.get('params')}}),flush=True)
''')
        result = client.call('readOnlyProbe', {'text': '你好'}, timeout=5)
        self.assertEqual(result, {'echo': {'text': '你好'}})
        client.close()
        self.assertIsNotNone(client.child.poll())

    def test_malformed_protocol_is_bounded_failure(self):
        client = self.make_client('''import sys,time
sys.stdin.readline()
print('not-json',flush=True)
time.sleep(2)
''')
        with self.assertRaises(RpcError) as caught:
            client.call('readOnlyProbe', {}, timeout=5)
        self.assertTrue(caught.exception.sent)
        self.assertTrue(client.closed.is_set())

    def test_blocked_pipe_write_is_bounded_and_uncertain(self):
        client = self.make_client('import time\ntime.sleep(10)\n')
        client.WRITE_TIMEOUT = 0.05
        before = time.monotonic()
        with self.assertRaises(RpcError) as caught:
            client.call('readOnlyProbe', {'text': 'x' * 1000000}, timeout=1)
        self.assertLess(time.monotonic() - before, 1)
        self.assertTrue(caught.exception.sent)
        self.assertEqual(caught.exception.code, 'codex_write_unknown')


class ModelMetadataTests(unittest.TestCase):
    def test_structured_error_categories_and_unknown_text_fail_closed(self):
        examples = [
            ({'error': {'code': 'not_supported'}}, 'codex_model_unavailable'),
            ({'codexErrorInfo': 'unauthorized'}, 'codex_authentication_required'),
            ({'error': {'code': 'insufficient_quota'}, 'status': 429}, 'codex_quota_exceeded'),
            ({'codexErrorInfo': {'httpConnectionFailed': {'httpStatusCode': 429}}}, 'codex_rate_limited'),
            ({'codexErrorInfo': 'contextWindowExceeded'}, 'codex_context_limit'),
            ({'codexErrorInfo': 'serverOverloaded'}, 'codex_provider_unavailable'),
            ({'message': 'model_not_found is text and may contain private data'}, 'codex_turn_failed'),
            ({'message': '{invalid JSON'}, 'codex_turn_failed'),
        ]
        for error, expected in examples:
            with self.subTest(error=error):
                self.assertEqual(classify_turn_error(error), expected)

    def test_catalog_filters_metadata_and_does_not_select_default(self):
        class Catalog:
            def call(self, method, params, timeout=20):
                self.method, self.params = method, params
                return {'data': [
                    {'id': 'catalog-internal-id', 'model': 'gpt-5.6-sol', 'displayName': 'GPT 5.6 Sol',
                     'isDefault': True, 'description': 'not exported', 'availabilityNux': {'message': 'not exported'}},
                    {'id': 'duplicate', 'model': 'gpt-5.6-sol', 'displayName': 'Duplicate'},
                    {'model': 'hidden-model', 'hidden': True},
                    {'model': 'https://not-a-model', 'displayName': 'Bad'},
                ], 'nextCursor': None}
        catalog = Catalog()
        result = _model_catalog(catalog)
        self.assertEqual(result, [{'id': 'gpt-5.6-sol', 'name': 'GPT 5.6 Sol',
                                   'is_default': True, 'support': 'catalog_listed'}])
        self.assertEqual(catalog.method, 'model/list')
        self.assertFalse(catalog.params['includeHidden'])

    def test_probe_preserves_configured_model_and_never_starts_thread(self):
        calls = []
        class ProbeRpc:
            def __init__(self, *args, **kwargs):
                pass
            def initialize(self):
                pass
            def close(self):
                pass
            def call(self, method, params, timeout=20):
                calls.append(method)
                if method == 'account/read':
                    return {'account': {'type': 'chatgpt'}}
                if method == 'config/read':
                    return {'config': {'model': 'gpt-6.1-terra'}}
                if method == 'model/list':
                    return {'data': [{'id': 'gpt-5.6-sol', 'model': 'gpt-5.6-sol',
                                      'displayName': 'GPT 5.6 Sol', 'isDefault': True}]}
                raise AssertionError(method)
        with patch.dict(codex_background._PROBES, {}, clear=True), \
                patch.object(codex_background, 'resolve_binary', return_value='/private/fake-codex'), \
                patch.object(codex_background, 'StdioRpc', ProbeRpc), \
                patch.object(codex_background.subprocess, 'run', return_value=SimpleNamespace(
                    returncode=0, stdout='codex-cli 0.145.0\n')):
            result = codex_background.probe()
            self.assertTrue(result['available'])
            self.assertEqual(result['model'], 'gpt-6.1-terra')
            self.assertFalse(result['configured_model_listed'])
            self.assertEqual(result['model_catalog_status'], 'available')
            self.assertEqual(result['models'][0]['id'], 'gpt-5.6-sol')
            result['models'][0]['id'] = 'changed'
            self.assertEqual(codex_background.probe()['models'][0]['id'], 'gpt-5.6-sol')
        self.assertEqual(calls, ['account/read', 'config/read', 'model/list'])


if __name__ == '__main__':
    unittest.main()
