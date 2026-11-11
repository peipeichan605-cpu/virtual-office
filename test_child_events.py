import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from child_events import ChildEventError
from configuration import initialize,load_config,save_config
from dashboard import Bridge
from history import timestamp


class ChildEventsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.directory=Path(self.temp.name).resolve()
        self.path=self.directory/'config.json';initialize(self.path)
        config=load_config(self.path);config.update(run_id='fixture',run_dir=str(self.directory/'run'))
        self.token='fixture-observation-root-token';self.start=time.time()-20
        config['agents']=[{'id':p+':logical','source_id':p+'-source','provider':p,'driver':'push',
            'name':p+' root','token_sha256':hashlib.sha256(self.token.encode()).hexdigest()} for p in ('cursor','grok_bot')]
        save_config(self.path,config);self.b=Bridge(load_config(self.path),dry_run=True)
        self.roots={r['provider']:r for r in self.b.definitions.values()}
        self.root_start('cursor','parent-run',self.start);self.root_start('grok_bot','parent-run',self.start)
        binding=self.b.child_events.create_observer({'agent_id':self.roots['cursor']['id'],'provider':'cursor','native_session_id':'cursor-native-root'})
        self.credential=json.loads(Path(binding['credential_path']).read_text())

    def tearDown(self):
        self.b.commands.close();self.b.history.close();self.temp.cleanup()

    def root_start(self,provider,run,at=None):
        root=self.roots[provider]
        return self.b.accept_agent_event(root,{'source_id':root['source_id'],'event_id':'start-'+run,
            'run_id':run,'type':'turn_started','text':'Fixture root task','at':timestamp(at or time.time())})

    def event(self,number=1,kind='started',**changes):
        result={'event_id':'event-'+str(number)+'-'+kind,'native_child_id':'native-child-'+str(number),
                'native_parent_id':'cursor-native-root','parent_run_id':'parent-run',
                'child_run_id':'child-run-'+str(number),'type':kind,'at':timestamp(self.start+1),
                'name':'Worker','text':'Public child text api_key=exampleSecretValue123456789'}
        if kind=='message':result.update(role='assistant',phase='commentary')
        result.update(changes);return result

    def send(self,data):
        return self.b.child_events.accept('observer',self.credential['observer_id'],self.credential['token'],data)

    def children(self):return [a for a in self.b.work_snapshot()['agents'] if a.get('is_subagent')]

    def denied(self,data,code):
        with self.assertRaises(ChildEventError) as error:self.send(data)
        self.assertEqual(error.exception.code,code)

    def test_native_hook_lifecycle_public_history_without_parent_completion(self):
        receipt=self.send(self.event());child=self.children()[0]
        self.assertEqual(child['id'],receipt['agent_id']);self.assertEqual(child['provider'],'cursor')
        self.assertEqual(child['evidence'],'native_hook');self.assertEqual(child['source_kind'],'cursor_hook')
        self.assertFalse(child['command_capability']['can_send']);self.assertIsNone(child['ground_desk_id'])
        self.send(self.event(kind='message'));self.send(self.event(kind='completed'))
        live=self.b.history.db.execute('SELECT status FROM live_runs WHERE agent_id=?',(self.roots['cursor']['id'],)).fetchone()
        self.assertEqual(live[0],'working');self.assertEqual(self.children()[0]['state'],'done')
        messages=self.b.history.messages(self.b.history_agents(),child['id'])
        self.assertEqual(len(messages),3);self.assertNotIn('exampleSecretValue',str(messages))

    def test_cooperative_grok_reports_cannot_claim_native_evidence(self):
        root=self.roots['grok_bot'];event=self.event(native_parent_id=root['source_id'])
        result=self.b.child_events.accept('report',root['id'],self.token,event)
        child=next(a for a in self.children() if a['id']==result['agent_id'])
        self.assertEqual(child['evidence'],'parent_reported');self.assertEqual(child['provider'],'grok_bot')
        with self.assertRaises(ChildEventError):self.b.child_events.accept('report',root['id'],self.token,dict(event,event_id='wrong',evidence='native_hook'))

    def test_duplicate_and_conflicting_event_have_durable_receipts(self):
        first=self.send(self.event());self.assertFalse(first['duplicate'])
        self.assertTrue(self.send(self.event())['duplicate'])
        self.denied(self.event(text='changed'),'child_event_id_conflict')
        self.assertEqual(len(self.b.history.messages(self.b.history_agents(),first['agent_id'])),1)

    def test_long_native_run_identity_is_normalized_without_losing_receipt_stability(self):
        event=self.event(child_run_id='a'*180)
        first=self.send(event)
        self.assertTrue(first['run_id'].startswith('native-'));self.assertLessEqual(len(first['run_id']),160)
        self.assertTrue(self.send(event)['duplicate'])
        self.send(self.event(kind='completed',child_run_id='a'*180))
        self.assertEqual(self.children()[0]['state'],'done')

    def test_terminal_requires_child_start_and_cannot_create_ghost_row(self):
        self.denied(self.event(kind='completed'),'child_start_required')
        self.assertEqual(self.children(),[])

    def test_public_field_whitelist_ignores_no_private_payloads(self):
        self.denied(self.event(tool_input='private'),'invalid_child_event')
        self.denied(self.event(kind='message',phase='analysis'),'invalid_public_child_message')
        self.denied(self.event(provider='grok_bot'),'invalid_child_event')
        self.assertEqual(self.children(),[])

    def test_alias_requires_verified_start_tool_and_is_immutable(self):
        self.send(self.event(native_alias={'kind':'tool_call','id':'task-call-1'}))
        self.denied(self.event(kind='message',native_alias={'kind':'conversation','id':'child-convo','parent_tool_call_id':'wrong'}),'child_alias_unverified')
        self.send(self.event(kind='message',native_alias={'kind':'conversation','id':'child-convo','parent_tool_call_id':'task-call-1'}))
        self.send(self.event(2,native_alias={'kind':'tool_call','id':'task-call-2'}))
        self.denied(self.event(2,kind='message',native_alias={'kind':'conversation','id':'child-convo','parent_tool_call_id':'task-call-2'}),'child_alias_conflict')
        self.denied(self.event(kind='completed',native_alias={'kind':'conversation','id':'changed'}),'child_alias_conflict')
        self.send(self.event(kind='completed',native_alias={'kind':'conversation','id':'child-convo'}))

    def test_failed_alias_admission_rolls_back_all_partial_child_rows(self):
        self.denied(self.event(native_alias={'kind':'conversation','id':'unverified'}),'child_alias_unverified')
        self.assertEqual(self.children(),[])
        self.assertEqual(self.b.history.db.execute('SELECT count(*) FROM observed_agents').fetchone()[0],0)

    def test_callback_failure_after_history_staging_rolls_back_receipt_alias_and_identity(self):
        event=self.event(native_alias={'kind':'tool_call','id':'staged-task'})
        with patch.object(self.b.child_events,'_step',side_effect=RuntimeError('fixture after-message crash')):
            with self.assertRaises(RuntimeError):self.send(event)
        for table in ('observed_agents','child_native_aliases','child_event_receipts','child_callback_runs'):
            self.assertEqual(self.b.history.db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
        self.assertEqual(self.b.history.db.execute("SELECT count(*) FROM runs WHERE agent_id LIKE 'child:%'").fetchone()[0],0)
        self.assertEqual(self.b.history.db.execute("SELECT count(*) FROM messages WHERE agent_id LIKE 'child:%'").fetchone()[0],0)
        self.assertEqual(self.b.history.db.execute("SELECT count(*) FROM event_receipts WHERE agent_id LIKE 'child:%'").fetchone()[0],0)
        self.assertFalse(self.send(event)['duplicate'])

    def test_nested_parent_chain_resolves_verified_conversation_alias(self):
        one=self.send(self.event(native_alias={'kind':'tool_call','id':'task-call-1'}))
        self.send(self.event(kind='message',native_alias={'kind':'conversation','id':'child-convo','parent_tool_call_id':'task-call-1'}))
        two=self.send(self.event(2,native_parent_id='child-convo'))
        child=next(a for a in self.children() if a['id']==two['agent_id'])
        self.assertEqual(child['depth'],2);self.assertEqual(child['parent_agent_id'],one['agent_id'])
        self.denied(self.event(3,native_parent_id='unknown-parent'),'child_parent_not_observed')
        self.denied(self.event(4,native_parent_id='native-child-4'),'child_parent_cycle')

    def test_old_parent_run_cannot_admit_but_existing_child_can_finish(self):
        one=self.send(self.event());self.root_start('cursor','new-parent',self.start+3)
        self.denied(self.event(2),'parent_run_not_current')
        self.send(self.event(kind='completed'))
        self.assertEqual(next(a for a in self.children() if a['id']==one['agent_id'])['state'],'done')

    def test_parent_completion_never_completes_child(self):
        self.send(self.event());root=self.roots['cursor']
        self.b.accept_agent_event(root,{'source_id':root['source_id'],'event_id':'root-end','run_id':'parent-run','type':'turn_completed','at':timestamp(self.start+3)})
        self.assertEqual(self.children()[0]['state'],'working')
        self.denied(self.event(2,at=timestamp(self.start+5)),'child_birth_outside_parent_run')
        self.send(self.event(kind='completed'))

    def test_active_child_can_spawn_after_root_done_or_root_new_turn(self):
        parent=self.send(self.event());root=self.roots['cursor']
        self.b.accept_agent_event(root,{'source_id':root['source_id'],'event_id':'root-end','run_id':'parent-run','type':'turn_completed','at':timestamp(self.start+3)})
        nested=self.send(self.event(2,native_parent_id='native-child-1',at=timestamp(self.start+4)))
        self.assertEqual(next(a for a in self.children() if a['id']==nested['agent_id'])['parent_agent_id'],parent['agent_id'])
        self.root_start('cursor','new-main-turn',self.start+5)
        self.send(self.event(3,native_parent_id='native-child-1',at=timestamp(self.start+6)))
        self.denied(self.event(4,at=timestamp(self.start+6)),'parent_run_not_current')
        self.send(self.event(kind='completed',at=timestamp(self.start+7)))
        self.denied(self.event(5,native_parent_id='native-child-1',at=timestamp(self.start+8)),'direct_parent_not_active')

    def test_rebind_revokes_old_observer_and_child_history_without_membership_change(self):
        receipt=self.send(self.event());old=self.credential;root=self.roots['cursor']
        binding=self.b.child_events.create_observer({'agent_id':root['id'],'provider':'cursor','native_session_id':'new-native-root'})
        self.assertNotEqual(binding['generation'],old['generation'])
        with self.assertRaises(ChildEventError):self.b.child_events.authorize('observer',old['observer_id'],old['token'])
        self.assertNotIn(receipt['agent_id'],self.b.history_agents())
        self.assertEqual(len(self.b.definitions),2)
        self.assertEqual(self.b.history.db.execute('SELECT count(*) FROM runs WHERE agent_id=?',(receipt['agent_id'],)).fetchone()[0],1)

    def test_idempotent_owner_setup_keeps_current_generation_and_private_file(self):
        root=self.roots['cursor'];new=self.b.child_events.create_observer({'agent_id':root['id'],'provider':'cursor','native_session_id':'cursor-native-root'})
        self.assertEqual(new['observer_id'],self.credential['observer_id']);self.assertNotIn('token',new)
        self.assertEqual(Path(new['credential_path']).stat().st_mode&0o777,0o600)

    def test_callback_polling_is_separate_from_codex_readers(self):
        self.send(self.event());self.b.children.poll(force=True);self.b.child_events.poll()
        self.assertEqual(self.children()[0]['state'],'working');self.assertTrue(self.b.work_snapshot()['subagents_ok'])
        self.assertEqual(self.b.children.tails,{})

    def test_restart_retains_identity_history_and_requires_fresh_activity(self):
        receipt=self.send(self.event());self.b.commands.close();self.b.history.close()
        self.b=Bridge(load_config(self.path),dry_run=True);self.b.child_events.poll()
        self.assertEqual(self.children()[0]['id'],receipt['agent_id']);self.assertEqual(self.children()[0]['state'],'waiting_user')
        self.send(self.event(kind='message',at=timestamp()));self.assertEqual(self.children()[0]['state'],'working')
        self.send(self.event(kind='completed'));self.assertEqual(self.children()[0]['state'],'done')

    def test_stale_callback_is_unknown_never_completed(self):
        receipt=self.send(self.event())
        with self.b.history.db:self.b.history.db.execute('UPDATE observed_agents SET updated_at=? WHERE id=?',(time.time()-1900,receipt['agent_id']))
        self.b.child_events.poll();self.assertEqual(self.children()[0]['state'],'waiting_user')

    def test_old_outbox_source_timestamp_is_not_restamped_by_receipt_poll(self):
        before=time.time()-4000;source=before+10
        self.root_start('cursor','old-source-root',before)
        receipt=self.send(self.event(parent_run_id='old-source-root',at=timestamp(source)))
        self.assertEqual(self.children()[0]['state'],'waiting_user')
        self.b.child_events.poll()
        child=self.children()[0];self.assertEqual(child['state'],'waiting_user')
        row=self.b.history.db.execute('SELECT updated_at FROM observed_agents WHERE id=?',(receipt['agent_id'],)).fetchone()
        self.assertAlmostEqual(row[0],source,places=3)

    def test_observer_root_endpoint_maps_only_bound_native_root(self):
        c=self.credential
        result=self.b.child_events.root_event(c['observer_id'],c['token'],{'source_id':'cursor-native-root','event_id':'native-parent','run_id':'native-parent','type':'turn_started','text':'Native public root task'})
        self.assertTrue(result['accepted']);self.assertEqual(self.b.sources[self.roots['cursor']['name']]['status'],'working')
        with self.assertRaises(ChildEventError):self.b.child_events.root_event(c['observer_id'],c['token'],{'source_id':'some-child-conversation','event_id':'spoof','run_id':'spoof','type':'turn_completed'})

    def pending(self):
        with self.b.history.db:self.b.history.db.execute('UPDATE child_observers SET active=0')
        metadata=self.b.child_events.register_pending(self.roots['cursor']['id'],self.token)
        return metadata,json.loads(Path(metadata['credential_path']).read_text())

    def test_reused_native_child_accepts_new_tool_call_but_keeps_identity(self):
        first=self.send(self.event(native_alias={'kind':'tool_call','id':'task-one'}))
        self.send(self.event(kind='message',native_alias={'kind':'conversation','id':'convo-one','parent_tool_call_id':'task-one'}))
        self.send(self.event(kind='completed'))
        self.root_start('cursor','parent-two',self.start+5)
        event=self.event(event_id='reuse-start',child_run_id='reuse-turn',parent_run_id='parent-two',at=timestamp(self.start+6),native_alias={'kind':'tool_call','id':'task-two'})
        self.assertEqual(self.send(event)['agent_id'],first['agent_id'])
        self.send(dict(event,event_id='reuse-message',type='message',role='assistant',phase='commentary',native_alias={'kind':'conversation','id':'convo-one','parent_tool_call_id':'task-two'}))
        self.assertEqual(len(self.children()),1);self.assertEqual(self.children()[0]['state'],'working')

    def test_pending_observer_requires_binding_before_any_native_activity(self):
        metadata,credential=self.pending()
        self.assertTrue(metadata['pending']);self.assertNotIn('token',metadata)
        again=self.b.child_events.register_pending(self.roots['cursor']['id'],self.token)
        self.assertEqual(again['observer_id'],metadata['observer_id']);self.assertEqual(again['activation_marker'],metadata['activation_marker'])
        with self.assertRaises(ChildEventError) as error:self.b.child_events.accept('observer',credential['observer_id'],credential['token'],self.event())
        self.assertEqual(error.exception.code,'observer_binding_pending')
        with self.assertRaises(ChildEventError):self.b.child_events.root_event(credential['observer_id'],credential['token'],{'source_id':credential['native_session_id'],'event_id':'pending','run_id':'pending','type':'turn_started'})
        self.assertEqual(self.children(),[])

    def test_pending_activation_rotates_private_token_without_starting_root(self):
        metadata,credential=self.pending();root=self.roots['cursor']
        before=self.b.history.db.execute('SELECT run_id FROM live_runs WHERE agent_id=?',(root['id'],)).fetchone()[0]
        bound=self.b.child_events.bind(credential['observer_id'],credential['token'],{'native_session_id':'actual-native-root','activation_marker':metadata['activation_marker']})
        current=json.loads(Path(bound['credential_path']).read_text())
        self.assertFalse(bound['pending']);self.assertNotIn('token',bound)
        self.assertNotEqual(current['token'],credential['token']);self.assertNotEqual(current['generation'],credential['generation'])
        self.assertEqual(self.b.definitions[root['id']]['source_id'],root['source_id'])
        self.assertEqual(self.b.history.db.execute('SELECT run_id FROM live_runs WHERE agent_id=?',(root['id'],)).fetchone()[0],before)
        with self.assertRaises(ChildEventError):self.b.child_events.authorize('observer',credential['observer_id'],credential['token'])
        same=self.b.child_events.bind(current['observer_id'],current['token'],{'native_session_id':'actual-native-root','activation_marker':metadata['activation_marker']})
        self.assertEqual(same['generation'],bound['generation'])
        with self.assertRaises(ChildEventError):self.b.child_events.bind(current['observer_id'],current['token'],{'native_session_id':'different-native-root','activation_marker':metadata['activation_marker']})

    def test_pending_activation_checks_marker_and_provider(self):
        metadata,credential=self.pending()
        with self.assertRaises(ChildEventError):self.b.child_events.bind(credential['observer_id'],credential['token'],{'native_session_id':'actual-native-root','activation_marker':'wrong-marker'})
        self.assertEqual(json.loads(Path(metadata['credential_path']).read_text())['token'],credential['token'])
        with self.assertRaises(ChildEventError):self.b.child_events.register_pending(self.roots['grok_bot']['id'],self.token)

    def test_pending_rotation_recovers_file_after_commit_before_rename_crash(self):
        metadata,credential=self.pending()
        with patch.object(self.b.child_events,'_recover_rotations',side_effect=[None,RuntimeError('fixture crash')]):
            with self.assertRaises(RuntimeError):self.b.child_events.bind(credential['observer_id'],credential['token'],{'native_session_id':'actual-native-root','activation_marker':metadata['activation_marker']})
        self.assertEqual(json.loads(Path(metadata['credential_path']).read_text())['token'],credential['token'])
        self.b.commands.close();self.b.history.close();self.b=Bridge(load_config(self.path),dry_run=True)
        current=json.loads(Path(metadata['credential_path']).read_text())
        self.assertEqual(current['native_session_id'],'actual-native-root');self.assertNotEqual(current['token'],credential['token'])
        self.assertEqual(self.b.history.db.execute('SELECT count(*) FROM child_observer_rotations').fetchone()[0],0)
        self.b.child_events.authorize('observer',current['observer_id'],current['token'])
        with self.assertRaises(ChildEventError):self.b.child_events.authorize('observer',credential['observer_id'],credential['token'])

    def test_pending_setup_recovers_crash_before_marker_file(self):
        with self.b.history.db:self.b.history.db.execute('UPDATE child_observers SET active=0')
        with patch.object(self.b.child_events,'_write_private',side_effect=RuntimeError('fixture before-marker crash')):
            with self.assertRaises(RuntimeError):self.b.child_events.register_pending(self.roots['cursor']['id'],self.token)
        pending=self.b.child_events.register_pending(self.roots['cursor']['id'],self.token)
        self.assertTrue(pending['activation_marker'].startswith('OFFICE_CURSOR_BIND_'))
        credential=json.loads(Path(pending['credential_path']).read_text())
        self.assertFalse(self.b.child_events.bind(credential['observer_id'],credential['token'],{'native_session_id':'repaired-native','activation_marker':pending['activation_marker']})['pending'])

    def test_pending_setup_recovers_marker_rename_before_hash_commit(self):
        with self.b.history.db:self.b.history.db.execute('UPDATE child_observers SET active=0')
        with patch.object(self.b.child_events,'_set_activation_hash',side_effect=RuntimeError('fixture marker-hash crash')):
            with self.assertRaises(RuntimeError):self.b.child_events.register_pending(self.roots['cursor']['id'],self.token)
        row=self.b.history.db.execute('SELECT * FROM child_observers WHERE active=1').fetchone()
        first=json.loads(Path(row['credential_path']).read_text())['activation_marker']
        repaired=self.b.child_events.register_pending(self.roots['cursor']['id'],self.token)
        self.assertEqual(repaired['activation_marker'],first)
        credential=json.loads(Path(repaired['credential_path']).read_text())
        self.b.child_events.bind(credential['observer_id'],credential['token'],{'native_session_id':'repaired-native','activation_marker':first})

    def test_active_cursor_index_excludes_retired_generations_and_non_cursor(self):
        root=self.roots['cursor']
        first=json.loads((self.b.child_events.directory/'.cursor-active.json').read_text())
        self.assertEqual(first['credentials'],[self.credential['observer_id']+'.json'])
        binding=self.b.child_events.create_observer({'agent_id':root['id'],'provider':'cursor','native_session_id':'new-index-native'})
        self.b.child_events.create_observer({'agent_id':self.roots['grok_bot']['id'],'provider':'grok_bot','native_session_id':'grok-native'})
        current=json.loads((self.b.child_events.directory/'.cursor-active.json').read_text())
        self.assertEqual(current['credentials'],[binding['observer_id']+'.json'])
        self.assertEqual((self.b.child_events.directory/'.cursor-active.json').stat().st_mode&0o777,0o600)
        self.b.members.logout({'agent_id':root['id']})
        self.assertEqual(json.loads((self.b.child_events.directory/'.cursor-active.json').read_text())['credentials'],[])

    def test_independent_selected_native_session_cannot_be_rebound_or_false_child(self):
        config=load_config(self.path)
        other={'id':'cursor:other','source_id':'other-logical-source','provider':'cursor','driver':'push','name':'Other root',
               'token_sha256':hashlib.sha256(self.token.encode()).hexdigest()}
        config['agents'].append(other);save_config(self.path,config);self.b.refresh_connections(config)
        self.b.child_events.create_observer({'agent_id':other['id'],'provider':'cursor','native_session_id':'other-native-root'})
        with self.assertRaises(ChildEventError):self.b.child_events.create_observer({'agent_id':self.roots['cursor']['id'],'provider':'cursor','native_session_id':'other-native-root'})
        provisional=self.send(self.event(native_alias={'kind':'tool_call','id':'provisional-call'}))
        for proof in ({'kind':'conversation','id':'other-native-root'},
                      {'kind':'conversation','id':'other-native-root','parent_tool_call_id':'wrong-call'}):
            self.denied(self.event(kind='message',native_alias=proof),'child_alias_unverified')
            self.assertEqual([child['id'] for child in self.children()],[provisional['agent_id']])
        alias={'kind':'conversation','id':'other-native-root','parent_tool_call_id':'provisional-call'}
        self.denied(self.event(kind='message',child_run_id='unstarted-run',native_alias=alias),'child_start_required')
        self.assertEqual([child['id'] for child in self.children()],[provisional['agent_id']])
        result=self.send(self.event(kind='message',native_alias={'kind':'conversation','id':'other-native-root','parent_tool_call_id':'provisional-call'}))
        self.assertTrue(result['ignored']);self.assertEqual(result['reason'],'child_independently_enrolled')
        self.assertNotIn(provisional['agent_id'],self.b.history_agents());self.assertEqual(self.children(),[])
        self.assertIsNone(self.b.history.db.execute("SELECT 1 FROM child_native_aliases WHERE kind='conversation' AND native_id='other-native-root'").fetchone())
        self.assertIn(other['id'],self.b.definitions)


if __name__=='__main__':unittest.main()
