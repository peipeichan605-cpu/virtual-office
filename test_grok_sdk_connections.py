"""Owner-selected registration contracts. Native calls are synthetic reads."""
import copy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from configuration import initialize, load_config, save_config
from connections import ConnectionError, Connections
from grok_sdk_connections import GrokSdkConnections
from history import HistoryStore


class FakeClient:
    def __init__(self):
        self.auth = {'state': 'authenticated', 'available': True, 'account_fingerprint': 'a' * 64,
                     'accessToken': 'PRIVATE_TOKEN_MARKER'}
        self.roster = {'bots': [{'id': 'native-bot-1', 'name': 'Native Bot', 'sessionId': ''},
                               {'id': 'native-bot-2', 'name': 'Other Bot', 'sessionId': 'other-session'}],
                       'truncated': False}
        self.session_rows = {'sessions': [{'agentId': 'native-bot-1', 'sessionId': 'other-chat'}]}
        self.tail = {'generation': 2, 'entries': [{'seq': '10', 'updatedSeq': '15', 'body': {'secret': 'OLD_NATIVE_TEXT'}}]}
        self.calls = []
        self.logins = {}

    def auth_status(self):
        return copy.deepcopy(self.auth)

    def list_bots(self):
        self.calls.append(('list_bots',))
        return copy.deepcopy(self.roster)

    def list_sessions(self, bot):
        self.calls.append(('list_sessions', bot))
        return copy.deepcopy(self.session_rows)

    def list_transcript(self, bot, session, limit):
        self.calls.append(('list_transcript', bot, session, limit))
        return copy.deepcopy(self.tail)

    def start_login(self, ident):
        self.calls.append(('start_login', ident))
        return self.logins.setdefault(ident, {'login_id': ident, 'state': 'pending',
            'login_url': 'https://cursor.com/loginDeepControl?uuid=' + ident, 'verifier': 'PRIVATE_LOGIN_VERIFIER'})

    def login_status(self, ident):
        self.calls.append(('login_status', ident))
        return self.logins.get(ident, {'state': 'not_found'})

    def poll_login(self, ident):
        self.calls.append(('poll_login', ident))
        return self.logins[ident]

    def cancel_login(self, ident):
        self.calls.append(('cancel_login', ident))
        if self.logins[ident]['state'] != 'authenticated':
            self.logins[ident] = {'state': 'cancelled'}
        return self.logins[ident]

    def redaction_secrets(self):
        return ['PRIVATE_TOKEN_MARKER']


class FixtureBridge:
    def __init__(self, path):
        self.config = initialize(path, 48418)
        self.dry_run = False
        self.history = HistoryStore(path.parent / 'history.sqlite3')
        self.commands = SimpleNamespace(lock=threading.RLock())
        self.connections = Connections.__new__(Connections)
        self.connections.bridge = self
        self.refreshes = 0
        with self.history.db:
            self.history.db.execute('CREATE TABLE office_membership_events (event_id TEXT PRIMARY KEY,agent_id TEXT,kind TEXT,at TEXT)')

    def refresh_connections(self, config):
        self.config = config
        self.refreshes += 1


class GrokSdkConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / 'config.json'
        self.bridge = FixtureBridge(self.path)
        self.client = FakeClient()
        self.connections = GrokSdkConnections(self.bridge, self.client)

    def tearDown(self):
        self.bridge.history.close()
        self.temp.cleanup()

    def request(self, **extra):
        return {'request_id': str(uuid.uuid4()), 'name': 'Office Bot', 'bot_id': 'native-bot-1', 'session_id': '', **extra}

    def assertError(self, code, callback):
        with self.assertRaises(ConnectionError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def test_only_explicit_selected_native_bot_is_enrolled_without_send_or_create(self):
        data = self.request()
        response = self.connections.connect(data)
        config = load_config(self.path)
        self.assertEqual(len(config['agents']), 1)
        self.assertEqual(config['agents'][0]['id'], response['agent_id'])
        self.assertEqual(config['agents'][0]['driver'], 'grok_sdk')
        settings = config['command_adapters']['grok_sdk']['sessions']['sdk-' + data['request_id']]
        self.assertEqual(settings, {'bot_id': 'native-bot-1', 'native_session_id': '', 'account_fingerprint': 'a' * 64})
        self.assertTrue(response['native_target_verified'])
        self.assertFalse(response['execution_verified'])
        self.assertEqual(response['state'], 'configured')
        self.assertTrue(response['same_session'])
        self.assertEqual(self.client.calls, [('list_bots',), ('list_sessions', 'native-bot-1'),
                                            ('list_transcript', 'native-bot-1', '', 1)])
        self.assertNotIn('PRIVATE', self.path.read_text())

    def test_main_chat_empty_id_requires_explicit_key_and_verified_roster(self):
        data = self.request()
        del data['session_id']
        self.assertError('invalid_connection', lambda: self.connections.connect(data))
        self.assertError('grok_sdk_invalid_session', lambda: self.connections.connect(self.request(session_id=None)))
        self.client.roster['bots'][0]['sessionId'] = 'only-chat'
        self.assertError('grok_sdk_session_not_found', lambda: self.connections.connect(self.request()))
        self.assertEqual(load_config(self.path)['agents'], [])
        response = self.connections.connect(self.request(session_id='only-chat'))
        self.assertEqual(response['session_id'], 'only-chat')

    def test_roster_and_session_outputs_exclude_account_private_fields(self):
        self.client.roster['bots'][0].update(accessToken='PRIVATE', description='PRIVATE', memory='PRIVATE')
        self.client.session_rows['sessions'][0].update(messages='PRIVATE', account='PRIVATE')
        self.assertEqual(self.connections.sessions('native-bot-1'), {'bot_id': 'native-bot-1', 'sessions': [
            {'session_id': '', 'kind': 'main', 'label': '主要聊天'},
            {'session_id': 'other-chat', 'kind': 'session', 'label': 'other-chat'}]})
        self.assertNotIn('PRIVATE', json.dumps(self.connections.bots()))
        self.assertNotIn('PRIVATE', json.dumps(self.connections.status()))

    def test_unverified_and_cross_bot_sessions_are_refused(self):
        self.assertError('grok_sdk_bot_not_found', lambda: self.connections.connect(self.request(bot_id='missing-bot')))
        self.assertError('grok_sdk_session_not_found', lambda: self.connections.connect(self.request(session_id='missing-chat')))
        self.client.session_rows['sessions'][0]['agentId'] = 'native-bot-2'
        self.assertError('grok_sdk_sessions_invalid', lambda: self.connections.connect(self.request()))
        self.assertEqual(load_config(self.path)['agents'], [])

    def test_authentication_account_is_verified_and_fenced_during_reads(self):
        self.client.auth.pop('account_fingerprint')
        self.assertError('grok_sdk_account_unverified', lambda: self.connections.connect(self.request()))
        self.client.auth['account_fingerprint'] = 'a' * 64
        original = self.client.list_sessions
        def change_account(bot):
            value = original(bot)
            self.client.auth['account_fingerprint'] = 'b' * 64
            return value
        self.client.list_sessions = change_account
        self.assertError('grok_sdk_account_changed', lambda: self.connections.connect(self.request()))
        self.assertEqual(load_config(self.path)['agents'], [])

    def test_registration_idempotency_and_lost_response_recovery_never_repeat_native_reads(self):
        data = self.request()
        response = self.connections.connect(data)
        self.client.calls.clear()
        self.client.auth['state'] = 'expired'
        self.assertEqual(self.connections.connect(data), response)
        self.assertEqual(self.connections.status(data['request_id'])['request']['response'], response)
        self.assertEqual(self.client.calls, [])
        self.assertError('request_id_conflict', lambda: self.connections.connect({**data, 'name': 'Different'}))

    def test_enrollment_captures_durable_baseline_before_agent_visible_and_omits_old_content(self):
        data = self.request()
        original_save = self.bridge.connections.save
        at_visibility = []
        def save_after_new_app_input(config):
            # Native input arrives after capture but before config activation.
            # The observer must later consume it, not recapture this new tail.
            self.client.tail['entries'] = [{'seq': '11', 'updatedSeq': '16'}]
            row = self.bridge.history.db.execute('SELECT baseline FROM grok_sdk_connections WHERE request_id=?',
                                                 (data['request_id'],)).fetchone()
            at_visibility.append(json.loads(row['baseline']))
            return original_save(config)
        with patch.object(self.bridge.connections, 'save', side_effect=save_after_new_app_input):
            self.connections.connect(data)
        self.assertEqual(at_visibility[0]['native_generation'], 2)
        self.assertEqual(at_visibility[0]['floor_seq'], '10')
        from grok_sdk_sync import token_parts
        self.assertEqual(token_parts(at_visibility[0]['cursor'], 'native-bot-1', ''), (2, 15))
        stored = self.bridge.history.db.execute('SELECT baseline FROM grok_sdk_connections WHERE request_id=?',
                                                (data['request_id'],)).fetchone()[0]
        self.assertNotIn('OLD_NATIVE_TEXT', stored)
        self.client.calls.clear()
        self.connections.status(data['request_id'])
        self.assertEqual(self.client.calls, [], 'Lost response recovery must retain the original boundary')

    def test_invalid_native_baseline_cannot_make_agent_visible(self):
        for tail in [None, {'generation': -1, 'entries': []}, {'generation': 1, 'entries': [{}]},
                     {'generation': 1, 'entries': [{'seq': '1', 'updatedSeq': 'x'}]},
                     {'generation': 1, 'entries': [{'seq': '1'}, {'seq': '2'}]}]:
            self.client.tail = tail
            self.assertError('grok_sdk_invalid_history', lambda: self.connections.connect(self.request()))
        self.assertEqual(load_config(self.path)['agents'], [])

    def test_account_switch_during_baseline_prevents_enrollment(self):
        original = self.client.list_transcript
        def switch(bot, session, limit):
            page = original(bot, session, limit)
            self.client.auth['account_fingerprint'] = 'b' * 64
            return page
        self.client.list_transcript = switch
        self.assertError('grok_sdk_account_changed', lambda: self.connections.connect(self.request()))
        self.assertEqual(load_config(self.path)['agents'], [])

    def test_saved_config_recovers_after_receipt_write_failure(self):
        data = self.request()
        with patch.object(self.connections, '_finish', side_effect=OSError('PRIVATE_ERROR')):
            self.assertError('grok_sdk_connection_unknown', lambda: self.connections.connect(data))
        self.assertEqual(len(load_config(self.path)['agents']), 1)
        restarted = GrokSdkConnections(self.bridge, self.client)
        receipt = restarted.status(data['request_id'])['request']
        self.assertEqual(receipt['state'], 'completed')
        self.assertEqual(restarted.connect(data), receipt['response'])
        self.assertNotIn('PRIVATE_ERROR', json.dumps(receipt))

    def test_failed_config_write_remains_unknown_and_is_not_auto_repeated(self):
        data = self.request()
        with patch.object(self.bridge.connections, 'save', side_effect=OSError('PRIVATE_ERROR')):
            self.assertError('grok_sdk_connection_unknown', lambda: self.connections.connect(data))
        self.assertEqual(load_config(self.path)['agents'], [])
        restarted = GrokSdkConnections(self.bridge, self.client)
        self.assertEqual(restarted.status(data['request_id'])['request']['state'], 'unknown')
        self.assertError('grok_sdk_connection_unknown', lambda: restarted.connect(data))

    def test_logout_epoch_fences_old_success_even_if_exact_config_is_restored(self):
        data = self.request()
        response = self.connections.connect(data)
        with self.bridge.history.db:
            self.bridge.history.db.execute('INSERT INTO office_membership_events VALUES (?,?,?,?)',
                (uuid.uuid4().hex, response['agent_id'], 'logged_out', 'fixture'))
        receipt = self.connections.status(data['request_id'])['request']
        self.assertEqual(receipt['state'], 'revoked')
        self.assertError('agent_membership_changed', lambda: self.connections.connect(data))

    def test_duplicate_name_and_native_session_do_not_add_agents(self):
        self.connections.connect(self.request())
        self.assertError('name_in_use', lambda: self.connections.connect(self.request(session_id='other-chat')))
        self.assertError('grok_sdk_session_already_selected', lambda: self.connections.connect(self.request(name='Another')))
        self.assertEqual(len(load_config(self.path)['agents']), 1)

    def test_webhook_and_unrelated_settings_are_preserved(self):
        config = load_config(self.path)
        old = {'provider': 'grok_bot', 'source_id': 'inv-legacy', 'name': 'Webhook Bot', 'driver': 'push', 'token_sha256': 'b' * 64}
        config['agents'].append(old)
        config['command_bindings'] = {'grok_bot:inv-legacy': {'adapter': 'grok_webhook', 'session_id': 'inv-legacy'}}
        config['command_adapters'] = {'grok_webhook': {'sessions': {'inv-legacy': {'credential_file': str(self.root / 'webhook.json')}}}}
        config['disabled_command_agents'] = ['grok_bot:inv-legacy']
        config['graphics_fixture'] = {'fps': 30, 'idle_saving': True}
        save_config(self.path, config)
        self.bridge.refresh_connections(load_config(self.path))
        before = copy.deepcopy(self.bridge.config)
        self.connections.connect(self.request())
        after = load_config(self.path)
        self.assertEqual(after['command_adapters']['grok_webhook'], before['command_adapters']['grok_webhook'])
        self.assertEqual(after['disabled_command_agents'], before['disabled_command_agents'])
        self.assertEqual(after['graphics_fixture'], before['graphics_fixture'])
        self.assertEqual(after['agents'][0], before['agents'][0])

    def test_config_rejects_unselected_routes_arbitrary_paths_and_cross_provider(self):
        response = self.connections.connect(self.request())
        valid = load_config(self.path)
        sid = response['agent_id'].split(':', 1)[1]
        mutations = [lambda c: c['agents'][0].update(provider='cursor'),
            lambda c: c['agents'][0].update(driver='push'),
            lambda c: c['agents'][0].update(source_id='other'),
            lambda c: c['command_adapters']['grok_sdk'].update(binary='/bin/sh'),
            lambda c: c['command_adapters']['grok_sdk']['sessions'][sid].update(credential_file='/tmp/other.json'),
            lambda c: c['command_adapters']['grok_sdk']['sessions'][sid].update(account_fingerprint='invalid'),
            lambda c: c['command_adapters']['grok_sdk']['sessions'][sid].update(native_session_id='../escape'),
            lambda c: c['command_adapters']['grok_sdk']['sessions'][sid].update(bot_id='https://other.test'),
            lambda c: c['command_adapters']['grok_sdk']['sessions'].clear(),
            lambda c: c['command_bindings'].clear()]
        for mutation in mutations:
            config = copy.deepcopy(valid)
            mutation(config)
            save_config(self.path, config)
            with self.assertRaises(ValueError):
                load_config(self.path)
        save_config(self.path, valid)

    def test_login_flow_leaves_human_url_and_redacts_secret_fields(self):
        ident = str(uuid.uuid4())
        result = self.connections.start_login({'request_id': ident})
        self.assertEqual(result['state'], 'pending')
        self.assertNotIn('PRIVATE', json.dumps(result))
        recovered = self.connections.status(login_request_id=ident)
        self.assertEqual(recovered['login']['login_url'], result['login_url'])
        self.assertFalse(any(call[0] == 'poll_login' for call in self.client.calls), 'Status recovery must not poll authentication')
        self.client.logins[ident] = {'state': 'authenticated', 'accessToken': 'PRIVATE'}
        polled = self.connections.login_status(ident)
        self.assertEqual(polled['state'], 'authenticated')
        self.assertNotIn('PRIVATE', json.dumps(polled))
        self.assertIn('PRIVATE_TOKEN_MARKER', self.bridge.history.secrets)
        self.assertEqual(self.connections.cancel_login({'login_id': ident})['state'], 'authenticated')

    def test_registration_reuses_adapter_client_and_registers_known_auth_for_redaction(self):
        self.bridge.commands.adapters = {'grok_sdk': SimpleNamespace(client=self.client)}
        service = GrokSdkConnections(self.bridge)
        self.assertIs(service.client, self.client)
        self.assertIn('PRIVATE_TOKEN_MARKER', self.bridge.history.secrets)
        self.assertNotIn('PRIVATE_TOKEN_MARKER', json.dumps(service.status()))

    def test_unknown_login_and_connect_status_are_safe_and_distinct(self):
        ident = str(uuid.uuid4())
        result = self.connections.status(request_id=ident, login_request_id=ident)
        self.assertEqual(result['request'], {'request_id': ident, 'state': 'not_found'})
        self.assertEqual(result['login_request'], {'request_id': ident, 'state': 'not_found'})

    def test_login_rejects_untrusted_url_and_never_forwards_client_exception_text(self):
        ident = str(uuid.uuid4())
        self.client.logins[ident] = {'state': 'pending', 'login_url': 'https://evil.test/loginDeepControl'}
        self.assertError('grok_sdk_login_url_invalid', lambda: self.connections.start_login({'request_id': ident}))
        self.client.list_bots = lambda: (_ for _ in ()).throw(OSError('PRIVATE_EXCEPTION'))
        self.assertError('grok_sdk_unavailable', self.connections.bots)

    def test_dry_run_refuses_login_mutations_and_registration(self):
        self.bridge.dry_run = True
        dry = GrokSdkConnections(self.bridge, self.client)
        ident = str(uuid.uuid4())
        before = self.path.read_bytes()
        for operation in [lambda: dry.connect(self.request()), lambda: dry.start_login({'request_id': ident}),
                          lambda: dry.login_status(ident), lambda: dry.cancel_login({'login_id': ident})]:
            self.assertError('grok_sdk_dry_run', operation)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.client.calls, [])


if __name__ == '__main__':
    unittest.main()
