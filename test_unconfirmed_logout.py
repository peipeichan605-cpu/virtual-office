"""Owner detachment is membership retirement, never fabricated native success."""
import copy
import json
from pathlib import Path
import tempfile
import sqlite3
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from commands import Commands, CommandError
from configuration import load_config, save_config
from dashboard import Bridge
from history import HistoryStore
from office_members import MemberError
import test_office_members as member_fixtures


class UnconfirmedLogoutTests(unittest.TestCase):
    setUp = member_fixtures.MemberTests.setUp
    tearDown = member_fixtures.MemberTests.tearDown
    request = member_fixtures.MemberTests.request
    invite = member_fixtures.MemberTests.invite
    claim_headers = member_fixtures.MemberTests.claim_headers
    member = member_fixtures.MemberTests.member
    command = member_fixtures.MemberTests.command

    def unknown(self, agent):
        ident = self.command(agent)
        with self.bridge.commands.lock, self.bridge.commands.db:
            self.bridge.commands.db.execute("UPDATE commands SET state='delivery_unknown',payload=NULL,run_id=?,native_message_id=? WHERE id=?",
                (ident, 'native-' + ident, ident))
        definition = self.bridge.definitions[agent]
        self.bridge.history.record(agent, definition['provider'], definition['name'], ident,
            'unknown', prompt='Existing prompt', delivery='Existing public result; native completion unknown', source_kind='fixture')
        return ident

    def challenge(self, agent):
        with self.assertRaises(MemberError) as raised:
            self.bridge.members.logout({'agent_id': agent})
        error = raised.exception
        self.assertEqual((error.status, error.code), (409, 'agent_has_unconfirmed_commands'))
        self.assertRegex(error.details['logout_token'], r'^[0-9a-f]{64}$')
        return {'agent_id': agent, 'acknowledge_unconfirmed': True, 'logout_token': error.details['logout_token']}

    def test_explicit_detach_preserves_unknown_outcome_and_history(self):
        _, agent, _ = self.member(); ident = self.unknown(agent)
        before = dict(self.bridge.commands.db.execute('SELECT * FROM commands WHERE id=?', (ident,)).fetchone())
        history = [dict(x) for x in self.bridge.history.db.execute('SELECT * FROM runs WHERE agent_id=?', (agent,))]
        payload = self.challenge(agent)
        self.assertIn(agent, self.bridge.definitions)
        result = self.bridge.members.logout(payload)
        self.assertTrue(result['logged_out']); self.assertTrue(result['history_preserved'])
        self.assertFalse(result['native_session_stopped'])
        self.assertEqual(result['unconfirmed_commands_preserved'], 1)
        self.assertEqual(before, dict(self.bridge.commands.db.execute('SELECT * FROM commands WHERE id=?', (ident,)).fetchone()))
        self.assertEqual(history, [dict(x) for x in self.bridge.history.db.execute('SELECT * FROM runs WHERE agent_id=?', (agent,))])
        self.assertTrue(self.bridge.commands.detached(ident))
        self.assertNotIn(agent, self.bridge.definitions)
        self.assertTrue(self.bridge.members.logout(payload)['duplicate'])
        self.assertEqual(self.bridge.commands.db.execute('SELECT count(*) FROM command_detachments WHERE command_id=?', (ident,)).fetchone()[0], 1)

    def test_ack_does_not_bypass_known_active_work(self):
        _, agent, _ = self.member(); ident = self.unknown(agent); payload = self.challenge(agent)
        for state in ('sending', 'queued', 'running', 'waiting_user'):
            with self.bridge.commands.db:
                self.bridge.commands.db.execute('UPDATE commands SET state=? WHERE id=?', (state, ident))
            with self.assertRaises(MemberError) as raised:self.bridge.members.logout(payload)
            self.assertEqual(raised.exception.code, 'agent_has_active_commands')
            self.assertIn(agent, self.bridge.definitions)
            self.assertFalse(self.bridge.commands.detached(ident))

    def test_new_unknown_work_requires_a_fresh_owner_confirmation(self):
        _, agent, _ = self.member(); self.unknown(agent); payload = self.challenge(agent)
        self.unknown(agent)
        with self.assertRaises(MemberError) as raised:self.bridge.members.logout(payload)
        self.assertEqual(raised.exception.code, 'agent_has_unconfirmed_commands')
        self.assertNotEqual(raised.exception.details['logout_token'], payload['logout_token'])
        self.assertEqual(raised.exception.details['unconfirmed_commands'], 2)
        self.assertEqual(self.bridge.commands.db.execute('SELECT count(*) FROM command_detachments').fetchone()[0], 0)

    def test_token_is_bound_to_membership_and_cannot_logout_new_idle_membership(self):
        _, agent, _ = self.member(); ident = self.unknown(agent); payload = self.challenge(agent)
        with self.bridge.history.db:
            self.bridge.history.db.execute('INSERT INTO office_membership_events VALUES (?,?,?,?)',
                (str(uuid.uuid4()), agent, 'fixture_rejoined', '2026-01-01T00:00:00Z'))
        with self.assertRaises(MemberError) as raised:self.bridge.members.logout(payload)
        self.assertEqual(raised.exception.code, 'agent_has_unconfirmed_commands')
        self.assertNotEqual(raised.exception.details['logout_token'], payload['logout_token'])
        with self.bridge.commands.db:self.bridge.commands.db.execute("UPDATE commands SET state='completed' WHERE id=?", (ident,))
        with self.assertRaises(MemberError) as raised:self.bridge.members.logout(payload)
        self.assertEqual(raised.exception.code, 'logout_scope_changed')
        self.assertIn(agent, self.bridge.definitions)

    def test_malformed_acknowledgement_is_rejected_without_retirement(self):
        _, agent, _ = self.member(); self.unknown(agent)
        for extra in ({'acknowledge_unconfirmed': 1}, {'acknowledge_unconfirmed': True},
                      {'acknowledge_unconfirmed': True, 'logout_token': 'wrong'},
                      {'acknowledge_unconfirmed': False, 'logout_token': 'a' * 64},
                      {'acknowledge_unconfirmed': True, 'logout_token': ['a' * 64]}):
            with self.assertRaises(MemberError) as raised:self.bridge.members.logout(dict(agent_id=agent, **extra))
            self.assertEqual(raised.exception.code, 'invalid_logout')
        self.assertIn(agent, self.bridge.definitions)

    def test_failed_config_save_rolls_back_detachment_fence(self):
        _, agent, _ = self.member(); ident = self.unknown(agent); payload = self.challenge(agent)
        with patch('office_members.save_config', side_effect=OSError('fixture save failure')):
            with self.assertRaises(OSError):self.bridge.members.logout(payload)
        self.assertIn(agent, self.bridge.definitions)
        self.assertFalse(self.bridge.commands.detached(ident))
        self.assertIn(agent, {a['id'] for a in load_config(self.path)['agents']})

    def assert_retired_authority(self, agent, token):
        self.assertNotIn(agent, {a['id'] for a in load_config(self.path)['agents']})
        self.assertNotIn(agent, self.bridge.definitions)
        self.assertFalse(self.bridge.commands.allowed(agent))
        self.assertIsNone(self.bridge.commands.binding(agent))
        self.assertFalse(self.bridge.inbox.authenticated(agent, token))
        with self.assertRaises(CommandError) as caught:self.command(agent)
        self.assertEqual(caught.exception.code, 'not_connected')

    def test_post_save_commit_failure_immediately_revokes_all_authority(self):
        _, agent, joined = self.member(); ident = self.unknown(agent); payload = self.challenge(agent)
        credential = Path(joined['conversation']['credential_path'])
        token = json.loads(credential.read_text())['token']; raw = self.bridge.commands.db
        class CommitFailure:
            def __getattr__(self, name):return getattr(raw, name)
            def __enter__(self):return raw.__enter__()
            def __exit__(self, *args):return raw.__exit__(*args)
            def commit(self):raise sqlite3.OperationalError('fixture commit failure')
        self.bridge.commands.db = CommitFailure()
        try:
            with self.assertRaises(sqlite3.OperationalError):self.bridge.members.logout(payload)
            # Credentials can still exist until cleanup, but carry no authority.
            self.assertTrue(credential.exists())
            self.assert_retired_authority(agent, token)
        finally:self.bridge.commands.db = raw
        self.assertTrue(self.bridge.members.logout(payload)['duplicate'])
        self.assertFalse(credential.exists()); self.assertTrue(self.bridge.commands.detached(ident))
        self.assertEqual(raw.execute('SELECT state FROM commands WHERE id=?', (ident,)).fetchone()[0], 'delivery_unknown')

    def test_post_save_refresh_failure_keeps_membership_fail_closed(self):
        _, agent, joined = self.member(); ident = self.unknown(agent); payload = self.challenge(agent)
        token = json.loads(Path(joined['conversation']['credential_path']).read_text())['token']
        with patch.object(self.bridge, 'refresh_connections', side_effect=OSError('fixture refresh failure')):
            with self.assertRaises(OSError):self.bridge.members.logout(payload)
        self.assert_retired_authority(agent, token)
        self.assertTrue(self.bridge.members.logout(payload)['duplicate'])
        self.assertTrue(self.bridge.commands.detached(ident))

    def test_startup_repairs_retirement_before_reconciling_old_work(self):
        _, agent, _ = self.member(); ident = self.unknown(agent)
        config = load_config(self.path); config['agents'] = []; config['command_bindings'] = {}
        config['logged_out_agent_ids'] = [agent]; save_config(self.path, config)
        # Simulate history evidence arriving before a crash but not belonging to
        # a new membership. Startup must not silently rewrite the retired row.
        with self.bridge.history.db:self.bridge.history.db.execute("UPDATE runs SET status='done' WHERE agent_id=?", (agent,))
        restored = Bridge(load_config(self.path), dry_run=True)
        try:
            self.assertTrue(restored.commands.detached(ident))
            self.assertEqual(restored.commands.db.execute('SELECT state FROM commands WHERE id=?', (ident,)).fetchone()[0], 'delivery_unknown')
        finally:restored.commands.close(); restored.history.close()

    def test_pending_unsent_work_is_cancelled_but_unknown_is_preserved(self):
        _, agent, _ = self.member(); unknown = self.unknown(agent); pending = self.command(agent)
        result = self.bridge.members.logout(self.challenge(agent))
        self.assertEqual(result['pending_commands_cancelled'], 1)
        self.assertEqual(self.bridge.commands.db.execute('SELECT state FROM commands WHERE id=?', (pending,)).fetchone()[0], 'cancelled')
        self.assertEqual(self.bridge.commands.db.execute('SELECT state FROM commands WHERE id=?', (unknown,)).fetchone()[0], 'delivery_unknown')

    def test_owner_http_challenge_and_ack_preserve_auth_boundary(self):
        _, agent, _ = self.member(); self.unknown(agent)
        code, challenge = self.request('/api/office/agents/logout', {'agent_id': agent})
        self.assertEqual(code, 409); self.assertIn('logout_token', challenge)
        payload = dict(agent_id=agent, acknowledge_unconfirmed=True, logout_token=challenge['logout_token'])
        self.assertEqual(self.request('/api/office/agents/logout', payload, key=False)[0], 403)
        self.assertEqual(self.request('/api/office/agents/logout', payload, headers={'Origin':'https://untrusted.example'})[0], 403)
        code, result = self.request('/api/office/agents/logout', payload)
        self.assertEqual(code, 200); self.assertTrue(result['logged_out'])


class Adapter:
    mode = 'cursor_cloud'
    serial = True
    same_session = True
    def __init__(self):self.sent = []; self.restored = []
    def available(self, sid):return True
    def can_dispatch(self, sid):return True
    def submit(self, sid, text):self.sent.append((sid,text)); return {'native_message_id':'new-native'}
    def resume(self, *args, **kwargs):self.restored.append((args,kwargs))


class DetachedCommandFenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.path = Path(self.temp.name)
        self.sid = 'bc-00000000-0000-4000-8000-000000000009'; self.agent = 'cursor:' + self.sid
        self.config = {'data_dir':str(self.path),'topic':'fixture-owner-key',
            'agents':[{'id':self.agent,'source_id':self.sid,'provider':'cursor','driver':'cursor','name':'Fixture'}],
            'command_bindings':{self.agent:{'adapter':'cursor_cloud','session_id':self.sid}}}
        self.history = HistoryStore(self.path/'history.sqlite3'); self.adapter = Adapter(); self.status = []
        self.open()
        self.old = str(uuid.uuid4()); self.text = 'Original exact task'
        self.commands.create({'agent_id':self.agent,'request_id':self.old,'text':self.text})
        with self.commands.db:self.commands.db.execute("UPDATE commands SET state='delivery_unknown',payload=NULL,run_id=?,native_message_id='old-native' WHERE id=?", (self.old,self.old))
        self.history.record(self.agent,'cursor','Fixture',self.old,'unknown',delivery='Unknown native completion')
        self.commands.detach_unconfirmed(self.agent)

    def open(self):
        self.commands = Commands(self.config,self.history,adapters={'cursor_cloud':self.adapter},
            on_status=lambda *args:self.status.append(args))

    def tearDown(self):
        self.commands.close(); self.history.close(); self.temp.cleanup()

    def event(self, kind, **fields):
        self.commands.observe_native(self.agent,dict(type=kind,adapter='cursor_cloud',session_id=self.sid,
            run_id=self.old,native_run_id='old-native',**fields))

    def test_old_callbacks_cannot_reactivate_a_same_id_rejoin(self):
        old_definition = copy.deepcopy(self.config['agents'])
        self.config['agents'] = []; self.commands.bindings = {}
        self.config['agents'] = old_definition; self.commands.bindings = self.config['command_bindings']
        before = dict(self.commands.db.execute('SELECT * FROM commands WHERE id=?',(self.old,)).fetchone())
        self.event('accepted'); self.event('started',text='[OFFICE:'+self.old+']\n'+self.text)
        self.event('text',text='Late text must not project',segment=1,final=True)
        self.event('status',waiting=True,step='Late status')
        self.event('finished',state='completed',text='Late delivery must not overwrite unknown')
        self.commands.observe_prompt(self.agent,self.old,'[OFFICE:'+self.old+']\n'+self.text)
        self.commands.observe_run(self.agent,self.old,'done')
        self.assertEqual(before,dict(self.commands.db.execute('SELECT * FROM commands WHERE id=?',(self.old,)).fetchone()))
        self.assertEqual(self.history.db.execute('SELECT count(*) FROM messages WHERE agent_id=?',(self.agent,)).fetchone()[0],0)
        self.assertEqual(self.status,[])

    def test_restart_does_not_resume_retired_observation_or_replay_prompt(self):
        self.commands.close(); self.open(); self.commands.restore_observations(); self.commands.tick()
        self.assertEqual(self.adapter.restored,[]); self.assertEqual(self.adapter.sent,[])
        self.assertEqual(self.commands.db.execute('SELECT state FROM commands WHERE id=?',(self.old,)).fetchone()[0],'delivery_unknown')

    def test_retired_unknown_does_not_block_a_new_explicit_command(self):
        new = str(uuid.uuid4()); self.commands.create({'agent_id':self.agent,'request_id':new,'text':'New explicitly requested task'})
        self.commands.tick()
        self.assertEqual(len(self.adapter.sent),1); self.assertIn(new,self.adapter.sent[0][1])
        self.assertEqual(self.commands.db.execute('SELECT state FROM commands WHERE id=?',(self.old,)).fetchone()[0],'delivery_unknown')

    def test_membership_is_checked_after_callback_acquires_lock(self):
        # A non-detached callback must also respect a logout that wins the lock.
        with self.commands.db:self.commands.db.execute('DELETE FROM command_detachments')
        started = threading.Event()
        def callback():started.set(); self.event('finished',state='completed',text='Late delivery')
        with self.commands.lock:
            worker = threading.Thread(target=callback); worker.start(); self.assertTrue(started.wait(1))
            time.sleep(.02)
            self.config['agents'] = []; self.commands.bindings = {}
        worker.join(1); self.assertFalse(worker.is_alive())
        self.assertEqual(self.commands.db.execute('SELECT state FROM commands WHERE id=?',(self.old,)).fetchone()[0],'delivery_unknown')
        self.assertEqual(self.status,[])


if __name__ == '__main__':unittest.main()
