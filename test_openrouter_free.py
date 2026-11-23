"""Portable OFFICE conversations exercise the real command queue with a fake remote."""
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid

from configuration import initialize, load_config, save_config
from connections import ConnectionError
from dashboard import Bridge, proxy_handler
from openrouter_free import API, LIGHTNING, SUPER_VL, key_path, read_key
from platform_adapters import RemoteError
from security_keys import owner_key


def entry(model, **changes):
    data = {'id': model, 'name': model, 'pricing': {'prompt': '0', 'completion': '0'},
            'architecture': {'input_modalities': ['text'], 'output_modalities': ['text']}}
    return dict(data, **changes)


class FakeRouter:
    def __init__(self):
        self.models = [entry(SUPER_VL), entry(LIGHTNING), entry('example/other:free')]
        self.calls = []
        self.posts = []
        self.error = self.reply = self.gate = None

    def json(self, method, url, headers, data=None):
        self.calls.append((method, url))
        if url == API + '/models':
            assert not headers and method == 'GET'
            return {'data': self.models}
        assert headers['Authorization'].startswith('Bearer ')
        if url == API + '/key':
            assert method == 'GET'
            return {'data': {'is_management_key': False, 'usage': 0}}
        assert method == 'POST' and url == API + '/chat/completions'
        self.posts.append(copy.deepcopy(data))
        if self.gate:
            self.gate.wait(timeout=4)
        if self.error:
            raise self.error
        if self.reply is not None:
            return self.reply
        return {'id': 'gen-fixture', 'model': data['model'].removesuffix(':free'), 'usage': {'cost': 0},
                'choices': [{'finish_reason': 'stop', 'message': {'role':'assistant', 'content': '答 ' +
                    data['messages'][-1]['content'], 'reasoning':'HIDDEN_REASONING'}}]}


class OpenRouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / '.office'
        self.config = self.root / 'config.json'
        initialize(self.config)
        self.remote = FakeRouter()
        self.start()

    def start(self):
        cfg = load_config(self.config)
        cfg.update(run_id='test', run_dir=str(self.root / 'run'))
        self.bridge = Bridge(cfg)
        self.service = self.bridge.openrouter_connections
        self.service.transport = self.remote
        self.adapter = self.bridge.commands.adapters['openrouter']
        self.adapter.transport = self.remote

    def tearDown(self):
        self.bridge.commands.close()
        self.bridge.mobile.close()
        self.bridge.history.close()
        self.temp.cleanup()

    def saved(self):
        key = 'sk-or-v1-fixture-private-01234567890'
        self.assertTrue(self.service.set_key({'api_key': key})['key_saved'])
        return key

    def register(self, name='Test A', model=LIGHTNING):
        data = {'request_id': str(uuid.uuid4()), 'name': name, 'model': model}
        return self.service.create(data)['agent_id'], data

    def send(self, ident, text):
        data = {'request_id': str(uuid.uuid4()), 'agent_id': ident, 'text': text}
        self.bridge.commands.create(data)
        self.bridge.commands.tick()
        return data

    def terminal(self, data):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            with self.bridge.commands.lock:
                row = self.bridge.commands.db.execute('SELECT state,detail_code FROM commands WHERE id=?',
                                                      (data['request_id'],)).fetchone()
            if row and row['state'] in {'completed','delivery_unknown','failed','cancelled'} and self.adapter.can_dispatch(''):
                return tuple(row)
            time.sleep(.01)
        self.fail('worker did not reach a terminal state')

    def test_catalog_enforces_zero_and_text_for_generic_models(self):
        self.remote.models += [entry('vendor/paid'), entry('vendor/priced:free', pricing={'prompt':0,'completion':1}),
            entry('vendor/extra:free', pricing={'prompt':0,'completion':0,'request':'0.01'}),
            entry('vendor/missing:free', pricing={'prompt':0}), entry('vendor/invalid:free', pricing={'prompt':'NaN','completion':0}),
            entry('vendor/embed:free', architecture={'input_modalities':['text'],'output_modalities':['embeddings']})]
        self.assertFalse(self.remote.calls)
        self.assertEqual([m['id'] for m in self.service.status()['models']], [LIGHTNING,SUPER_VL,'example/other:free'])
        self.assertFalse(self.service.status()['key_saved'])
        self.assertEqual(set(self.remote.calls), {('GET', API + '/models')})

    def test_enrollment_key_is_private_replay_is_idempotent_and_retirement_is_final(self):
        with self.assertRaisesRegex(ConnectionError,'openrouter_key_required'):
            self.register()
        key = self.saved()
        self.assertEqual(read_key(self.bridge.config), key)
        self.assertNotIn(key, self.config.read_text())
        self.assertNotIn(key, json.dumps(self.service.status()))
        a, data = self.register()
        self.assertEqual(self.service.create(data)['agent_id'], a)
        with self.assertRaisesRegex(ConnectionError,'request_id_conflict'):
            self.service.create(dict(data, model=SUPER_VL))
        with self.assertRaisesRegex(ConnectionError,'name_in_use'):
            self.register()
        self.assertFalse(self.remote.posts)
        self.assertEqual(self.bridge.work_snapshot()['agents'][0]['state'], 'idle')
        self.assertTrue(self.bridge.members.logout({'agent_id':a})['logged_out'])
        with self.assertRaisesRegex(ConnectionError,'agent_logged_out'):
            self.service.create(data)
        self.assertFalse(load_config(self.config)['agents'])
        self.assertTrue(key_path(self.bridge.config).is_file())

    def test_two_turns_history_restart_isolation_and_working_status(self):
        key = self.saved()
        a, _ = self.register()
        b, _ = self.register('Test B', SUPER_VL)
        self.remote.gate = threading.Event()
        first = self.send(a, 'first-turn')
        for _ in range(300):
            if self.remote.posts: break
            time.sleep(.01)
        self.assertTrue(self.remote.posts)
        self.assertEqual(self.bridge.sources['Test A']['status'], 'working')
        self.remote.gate.set()
        self.assertEqual(self.terminal(first)[0], 'completed')
        self.assertTrue(self.bridge.commands.create(first)['duplicate'])
        self.bridge.commands.close()
        self.bridge.mobile.close()
        self.bridge.history.close()
        self.start()
        second = self.send(a, 'second '+key)
        self.assertEqual(self.terminal(second)[0], 'completed')
        other = self.send(b, 'another-agent')
        self.assertEqual(self.terminal(other)[0], 'completed')
        one, two, three = self.remote.posts
        self.assertEqual([m['role'] for m in two['messages']], ['user','assistant','user'])
        self.assertEqual(two['messages'][0]['content'], 'first-turn')
        self.assertEqual(two['messages'][1]['content'], '答 first-turn')
        self.assertEqual(three['messages'], [{'role':'user','content':'another-agent'}])
        for call in self.remote.posts:
            self.assertEqual(call['provider'], {'allow_fallbacks':False, 'max_price':{'prompt':0,'completion':0,'request':0,'image':0}})
            self.assertEqual(call['plugins'], [{'id':'web','enabled':False}])
            self.assertNotIn('tools', call)
            self.assertNotIn('models', call)
        self.assertNotIn(key, str(two))
        messages=self.bridge.history.messages({a}, a)
        self.assertEqual([m['role'] for m in messages], ['user','assistant','user','assistant'])
        self.assertNotIn('HIDDEN_REASONING', str(messages))
        self.assertNotIn('another-agent', str(messages))
        self.assertEqual(len(self.bridge.history.list({a},agent=a,terminal=True)), 2)
        self.assertEqual(self.bridge.sources['Test A']['status'], 'done')

    def test_removed_free_variant_never_substitutes_paid(self):
        self.saved()
        a, _ = self.register()
        self.remote.models = [entry(LIGHTNING.removesuffix(':free'))]
        self.assertEqual(self.terminal(self.send(a,'unavailable')), ('failed','openrouter_model_unavailable'))
        self.assertFalse(self.remote.posts)
        self.assertEqual(self.bridge.sources['Test A']['status'], 'error')

    def test_429_is_a_failed_delivery_and_never_resent(self):
        self.saved()
        a, _ = self.register()
        self.remote.error = RemoteError('platform_rate_limited')
        data = self.send(a, 'quota')
        self.assertEqual(self.terminal(data), ('failed', 'platform_rate_limited'))
        self.bridge.commands.tick()
        self.assertTrue(self.bridge.commands.create(data)['duplicate'])
        self.assertEqual(len(self.remote.posts), 1)
        self.assertIn('流量', self.bridge.history.list({a}, agent=a, terminal=True)[0]['delivery_preview'])

    def test_api_error_envelopes_and_credit_limits_keep_safe_failure_reasons(self):
        self.saved()
        a, _ = self.register()
        self.remote.reply={'error':{'code':429,'message':'private provider detail'}}
        self.assertEqual(self.terminal(self.send(a,'quota')), ('failed','platform_rate_limited'))
        self.remote.error=RemoteError('platform_request_failed', http_status=402)
        self.assertEqual(self.terminal(self.send(a,'limit')), ('failed','openrouter_account_limit'))
        self.assertNotIn('private provider detail', str(self.bridge.history.list({a}, agent=a)))

    def test_unknown_model_or_partial_output_is_not_a_delivery(self):
        self.saved()
        a, _ = self.register()
        base={'model':LIGHTNING, 'usage':{'cost':0}, 'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'text'}}]}
        self.remote.reply=dict(base, choices=[{'finish_reason':'length','message':{'role':'assistant','content':'partial'}}])
        self.assertEqual(self.terminal(self.send(a,'first')), ('failed','openrouter_incomplete'))
        self.remote.reply=dict(base, model='vendor/unselected:free')
        self.assertEqual(self.terminal(self.send(a,'second')), ('delivery_unknown','openrouter_unconfirmed'))
        self.bridge.commands.tick()
        self.assertEqual(len(self.remote.posts), 2)
        self.assertFalse(any(m['role']=='assistant' for m in self.bridge.history.messages({a},a)))

    def test_only_desktop_owner_can_enroll_or_change_the_key(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), proxy_handler(self.bridge))
        self.bridge.config['port'] = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(method, suffix, key=None, body=None):
            connection = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            headers = {'Content-Type': 'application/json'}
            if key is not None: headers['X-Office-Key'] = key
            try:
                connection.request(method, '/api/office/openrouter/' + suffix,
                                   body=json.dumps(body) if body is not None else None, headers=headers)
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()
        try:
            for method, suffix in [('GET','status'), ('POST','key'), ('POST','create')]:
                self.assertEqual(request(method, suffix, 'fixture-unprivileged')[0], 403)
            self.assertFalse(self.remote.calls)
            owner = owner_key(self.bridge.config)
            self.assertEqual(request('POST','key',owner,{'api_key':'example-fixture-secret'}), (200,{'key_saved':True}))
            self.assertTrue(request('GET','status',owner)[1]['key_saved'])
            code, result=request('POST','create',owner,{'request_id':str(uuid.uuid4()),'model':SUPER_VL,'name':'HTTP Agent'})
            self.assertEqual(code,200)
            self.assertIn(result['agent_id'],self.bridge.definitions)
        finally:
            server.shutdown();server.server_close();thread.join(3)

    def test_permission_changed_during_catalog_lookup_sends_no_chat(self):
        self.saved()
        a, _ = self.register()
        self.adapter.authorize=lambda agent, sid, run: False
        self.assertEqual(self.terminal(self.send(a,'revoked')), ('cancelled','openrouter_permission_changed'))
        self.assertFalse(self.remote.posts)

    def test_config_disallows_paid_model_or_unbound_settings(self):
        self.saved()
        a,_=self.register()
        config=load_config(self.config)
        config['command_adapters']['openrouter']['sessions'][a.split(':')[1]]['model']=LIGHTNING.removesuffix(':free')
        save_config(self.config,config)
        with self.assertRaisesRegex(ValueError,'free model'):
            load_config(self.config)
