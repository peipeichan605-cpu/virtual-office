"""Synthetic root native-provenance tests; no live Cursor database is read."""
import copy
from pathlib import Path
import time
import unittest

from configuration import load_config
from cursor_public_sync import CursorPublicSync, digest
from dashboard import Bridge
from event_ingest import EventConflict
from history import timestamp
import test_child_events


class RootStore:
    def __init__(self):
        self.results={}
        self.calls=[]
        self.callback=None

    def root_snapshot(self,conversation_id,request_id,terminal=False):
        self.calls.append((conversation_id,request_id,terminal))
        if self.callback:self.callback()
        value=self.results.get(request_id)
        if isinstance(value,Exception):raise value
        return copy.deepcopy(value)

    def snapshot(self,*args,**kwargs):
        raise AssertionError('Root-only fixture must not select a child')


class CursorRootPublicSyncTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_child_events.ChildEventsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.b=self.fixture.b
        self.root=self.fixture.roots['cursor']
        self.credential=self.fixture.credential
        self.store=RootStore()
        self.sync=CursorPublicSync(self.b.child_events,self.store)
        self.b.child_events.public_sync=self.sync
        self.run='native-root-request-one'

    def native(self,kind,run=None,event_id=None,**fields):
        run=run or self.run
        return self.b.child_events.root_event(self.credential['observer_id'],self.credential['token'],{
            'source_id':self.credential['native_session_id'],'run_id':run,'type':kind,
            'event_id':event_id or 'root-'+run+'-'+kind,'at':timestamp(self.fixture.start+2),**fields})

    def start(self,run=None,**fields):
        return self.native('turn_started',run=run,text='Public original native prompt',**fields)

    def finish(self,text='Native final body',**fields):
        return self.native('turn_completed',text=text,**fields)

    def source(self,messages=None,final=None,run=None,boundary='human-exact-request',conversation=None):
        self.store.results[run or self.run]={'complete':True,
            'conversation_id':conversation or self.credential['native_session_id'],
            'boundary_id':boundary,'messages':messages or [],'final_id':final}

    def bubble(self,ident,text,closed=True):
        return {'id':ident,'text':text,'closed':closed,'at':timestamp(self.fixture.start+3)}

    def messages(self,run=None):
        return self.b.history.messages(self.b.history_agents(),self.root['id'],run or self.run)

    def record(self,table='runs',run=None):
        query='SELECT * FROM '+table+' WHERE agent_id=?'
        args=[self.root['id']]
        if table=='runs':query+=' AND run_id=?';args.append(run or self.run)
        return dict(self.b.history.db.execute(query,args).fetchone())

    def tracking(self):
        return [dict(r) for r in self.b.history.db.execute('SELECT * FROM cursor_public_root_runs')]

    def test_authenticated_native_start_creates_durable_exact_root_target(self):
        self.start()
        rows=self.tracking()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['native_conversation'],self.credential['native_session_id'])
        self.assertEqual(rows[0]['run_id'],self.run)
        self.assertEqual(rows[0]['observer_id'],self.credential['observer_id'])
        self.assertEqual(rows[0]['generation'],self.credential['generation'])
        self.source([self.bubble('progress','Actual public progress')])
        self.sync.poll(force=True)
        self.assertEqual(self.store.calls,[(self.credential['native_session_id'],self.run,False)])
        self.assertIn('Actual public progress',[m['text'] for m in self.messages()])
        self.assertEqual(self.record()['status'],'working')

    def test_root_public_start_and_tracking_rollback_together_on_run_conflict(self):
        self.fixture.root_start('cursor',self.run,self.fixture.start+1)
        with self.assertRaises(EventConflict):self.start()
        self.assertEqual(self.tracking(),[])
        self.assertIsNone(self.b.history.db.execute('SELECT 1 FROM event_receipts WHERE event_id=?',
            ('root-'+self.run+'-turn_started',)).fetchone())

    def test_duplicate_start_receipt_does_not_create_extra_checkpoint_or_public_prompt(self):
        self.start()
        before=self.tracking()
        self.assertTrue(self.start()['duplicate'])
        self.assertEqual(self.tracking(),before)
        self.assertEqual(len([m for m in self.messages() if m['role']=='user']),1)

    def test_original_report_credential_and_unbound_history_never_enable_root_storage_reads(self):
        self.fixture.root_start('cursor',self.run,self.fixture.start+1)
        self.source([self.bubble('unbound','MUST_NOT_IMPORT_UNBOUND')])
        self.sync.poll(force=True)
        self.assertEqual(self.store.calls,[])
        self.assertEqual(self.tracking(),[])

    def test_growing_text_waits_and_store_never_completes_native_run(self):
        self.start()
        self.source([self.bubble('growing','Not closed yet',False)],final='growing')
        self.sync.poll(force=True)
        self.assertEqual([m for m in self.messages() if m['role']=='assistant'],[])
        self.source([self.bubble('closed','Closed public progress')],final='closed')
        self.sync.poll(force=True)
        self.assertEqual(self.record()['status'],'working')
        self.assertEqual(self.record()['delivery'],'')
        self.assertEqual(next(m for m in self.messages() if m['role']=='assistant')['phase'],'commentary')

    def test_native_stop_and_late_storage_keep_finish_state_activity_and_one_final(self):
        self.start()
        self.native('message',role='assistant',phase='commentary',text='Native final body')
        self.finish()
        native_run=self.record()
        native_live=self.record('live_runs')
        native_source=copy.deepcopy(self.b.sources[self.root['name']])
        self.source([self.bubble('progress','Earlier public progress')],final='progress')
        self.sync.poll(force=True)
        self.assertEqual(self.record(),native_run)
        self.assertEqual([m['phase'] for m in self.messages() if m['text']=='Earlier public progress'],['commentary'])
        self.source([self.bubble('progress','Earlier public progress'),self.bubble('final','Native final body')],final='final')
        self.sync.poll(force=True)
        self.sync.poll(force=True)
        self.assertEqual(self.record(),native_run)
        self.assertEqual(self.record('live_runs'),native_live)
        self.assertEqual(self.b.sources[self.root['name']],native_source)
        self.assertEqual(len([m for m in self.messages() if m['phase']=='final']),1)
        self.assertEqual(self.tracking()[0]['final_fingerprint'],digest('Native final body'))

    def test_store_cannot_promote_old_progress_when_native_stop_has_no_final(self):
        self.start()
        self.finish(text='')
        self.source([self.bubble('old-progress','Earlier progress is not final')],final='old-progress')
        self.sync.poll(force=True)
        self.assertEqual(self.record()['delivery'],'')
        self.assertEqual([m['phase'] for m in self.messages() if m['role']=='assistant'],['commentary'])
        self.assertEqual(self.tracking()[0]['final_fingerprint'],'')

    def test_non_observer_final_cannot_supply_native_final_proof(self):
        self.start()
        self.b.accept_agent_event(self.root,{'source_id':self.root['source_id'],'run_id':self.run,
            'event_id':'manual-final','type':'turn_completed','text':'Unproven body'})
        self.source([self.bubble('body','Unproven body')],final='body')
        self.sync.poll(force=True)
        self.assertEqual(self.tracking()[0]['native_final_fingerprint'],'')
        self.assertEqual(self.tracking()[0]['final_fingerprint'],'')
        self.assertIn('commentary',[m['phase'] for m in self.messages() if m['event_id'].startswith('cursor-public:')])

    def test_stable_public_bubble_revision_and_final_upgrade_never_duplicates_visible_final(self):
        self.start()
        self.source([self.bubble('one','First public revision')])
        self.sync.poll(force=True)
        first=next(m for m in self.messages() if m['role']=='assistant')
        self.finish(text='Completed public revision')
        self.source([self.bubble('one','Completed public revision')],final='one')
        self.sync.poll(force=True)
        rows=self.messages()
        public=next(m for m in rows if m['event_id']==first['event_id'])
        self.assertEqual(public['text'],'Completed public revision')
        self.assertEqual(public['phase'],'final')
        self.assertGreater(public['seq'],first['seq'])
        self.assertEqual(len([m for m in rows if m['phase']=='final']),1)
        after=self.b.history.messages(self.b.history_agents(),self.root['id'],self.run,after=first['seq'])
        self.assertIn(first['event_id'],[m['event_id'] for m in after])

    def test_history_reader_without_optional_root_schema_keeps_legacy_final_dedupe(self):
        from history import HistoryStore
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            store=HistoryStore(Path(directory)/'history.sqlite3')
            try:
                for event in ('first','second'):
                    store.message('cursor:legacy','run','assistant','final','Same final',event_id=event)
                self.assertEqual([m['event_id'] for m in store.messages({'cursor:legacy'},'cursor:legacy')],['first'])
                store.db.execute('CREATE TABLE cursor_public_messages(legacy TEXT)')
                store.db.execute('CREATE TABLE cursor_public_root_runs(legacy TEXT)')
                self.assertEqual([m['event_id'] for m in store.messages({'cursor:legacy'},'cursor:legacy')],['first'])
            finally:store.close()

    def test_callback_first_progress_keeps_existing_visible_identity(self):
        self.start()
        self.native('message',event_id='native-progress-first',role='assistant',phase='commentary',text='Same public progress')
        original=next(m for m in self.messages() if m['role']=='assistant')
        self.source([self.bubble('progress','Same public progress')])
        self.sync.poll(force=True)
        rows=[m for m in self.messages() if m['role']=='assistant']
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['event_id'],original['event_id'])
        self.assertEqual(rows[0]['seq'],original['seq'])
        self.assertIsNotNone(self.b.history.db.execute('SELECT 1 FROM event_receipts WHERE event_id=?',
            ('native-progress-first',)).fetchone())

    def test_structured_first_progress_suppresses_later_callback_projection_before_api_read(self):
        self.start()
        self.source([self.bubble('progress','Same public progress')])
        self.sync.poll(force=True)
        original=next(m for m in self.messages() if m['role']=='assistant')
        self.native('message',event_id='native-progress-later',role='assistant',phase='commentary',text='Same public progress')
        rows=[m for m in self.messages() if m['role']=='assistant']
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['event_id'],original['event_id'])
        self.assertEqual(rows[0]['seq'],original['seq'])
        self.assertEqual(self.b.history.messages(self.b.history_agents(),self.root['id'],self.run,after=original['seq']),[])
        self.assertIsNotNone(self.b.history.db.execute('SELECT 1 FROM event_receipts WHERE event_id=?',
            ('native-progress-later',)).fetchone())

    def test_callback_receipt_and_projection_suppression_share_one_history_transaction(self):
        from unittest.mock import patch
        self.start()
        self.source([self.bubble('progress','Same public progress')])
        self.sync.poll(force=True)
        before=self.messages()
        run=self.record()
        with patch.object(self.sync,'suppress_root_callback_projection',side_effect=OSError('fixture transaction failure')):
            with self.assertRaises(OSError):
                self.native('message',event_id='atomic-callback',role='assistant',phase='commentary',text='Same public progress')
        self.assertEqual(self.messages(),before)
        self.assertEqual(self.record(),run)
        self.assertIsNone(self.b.history.db.execute('SELECT 1 FROM event_receipts WHERE event_id=?',('atomic-callback',)).fetchone())
        self.assertIsNone(self.b.history.db.execute('SELECT 1 FROM cursor_public_root_callbacks WHERE event_id=?',('atomic-callback',)).fetchone())

    def test_repeated_equal_native_bubbles_are_individually_associated(self):
        self.start()
        for number in (1,2):
            self.native('message',event_id='native-equal-'+str(number),role='assistant',phase='commentary',text='Repeated')
        original=[m['event_id'] for m in self.messages() if m['role']=='assistant']
        self.source([self.bubble('one','Repeated'),self.bubble('two','Repeated')])
        self.sync.poll(force=True)
        self.assertEqual([m['event_id'] for m in self.messages() if m['role']=='assistant'],original)

    def test_interleaved_equal_callback_cannot_consume_same_structured_bubble_twice(self):
        self.start()
        self.source([self.bubble('one','Repeated')])
        self.sync.poll(force=True)
        first=next(m for m in self.messages() if m['role']=='assistant')
        self.native('message',event_id='first-callback',role='assistant',phase='commentary',text='Repeated')
        self.native('message',event_id='second-callback',role='assistant',phase='commentary',text='Repeated')
        before=[m for m in self.messages() if m['role']=='assistant']
        self.assertEqual(len(before),2)
        self.assertEqual(before[0]['event_id'],first['event_id'])
        self.assertEqual(before[1]['event_id'],'second-callback')
        self.source([self.bubble('one','Repeated'),self.bubble('two','Repeated')])
        self.sync.poll(force=True)
        self.assertEqual([m['event_id'] for m in self.messages() if m['role']=='assistant'],
            [m['event_id'] for m in before])

    def test_distinct_equal_public_bubbles_keep_their_native_identity(self):
        self.start()
        self.source([self.bubble('one','Same public words'),self.bubble('two','Same public words')])
        self.sync.poll(force=True)
        self.assertEqual(len([m for m in self.messages() if m['text']=='Same public words']),2)

    def test_logout_during_native_read_cannot_commit_unselected_root_text(self):
        self.start()
        self.source([self.bubble('gone','MUST_NOT_IMPORT_AFTER_LOGOUT')])
        self.store.callback=lambda:self.b.definitions.pop(self.root['id'],None)
        self.sync.poll(force=True)
        self.assertEqual(self.b.history.db.execute("SELECT count(*) FROM messages WHERE text LIKE 'MUST_NOT_IMPORT%'").fetchone()[0],0)

    def test_rebind_during_read_cannot_commit_old_scope(self):
        self.start()
        self.source([self.bubble('old','MUST_NOT_IMPORT_OLD_SCOPE')])
        self.store.callback=lambda:self.b.child_events.create_observer({'agent_id':self.root['id'],
            'provider':'cursor','native_session_id':'replacement-native-root'})
        self.sync.poll(force=True)
        self.assertEqual(self.b.history.db.execute("SELECT count(*) FROM messages WHERE text LIKE 'MUST_NOT_IMPORT%'").fetchone()[0],0)

    def test_membership_aba_same_id_is_fenced_by_new_generation(self):
        self.start()
        self.source([self.bubble('old','MUST_NOT_IMPORT_ABA')])
        def replace():
            root=self.b.definitions.pop(self.root['id'])
            self.b.children.sync()
            self.b.definitions[root['id']]=root
            self.b.children.sync()
        self.store.callback=replace
        self.sync.poll(force=True)
        self.assertEqual(self.b.history.db.execute("SELECT count(*) FROM messages WHERE text LIKE 'MUST_NOT_IMPORT%'").fetchone()[0],0)

    def test_new_current_request_during_read_cannot_import_old_request_text(self):
        self.start()
        self.source([self.bubble('old','MUST_NOT_IMPORT_OLD_REQUEST')])
        self.store.callback=lambda:self.start(run='native-request-two')
        self.sync.poll(force=True)
        self.assertEqual(self.record('live_runs')['run_id'],'native-request-two')
        self.assertEqual(self.b.history.db.execute("SELECT count(*) FROM messages WHERE text LIKE 'MUST_NOT_IMPORT%'").fetchone()[0],0)

    def test_exact_native_conversation_and_stable_human_boundary_are_required(self):
        self.start()
        self.source([self.bubble('wrong','MUST_NOT_IMPORT_FOREIGN')],conversation='another-native-root')
        self.sync.poll(force=True)
        self.assertEqual([m for m in self.messages() if m['role']=='assistant'],[])
        self.source([self.bubble('one','Verified public body')])
        self.sync.poll(force=True)
        self.source([self.bubble('two','MUST_NOT_IMPORT_CHANGED_BOUNDARY')],boundary='different-human')
        self.sync.poll(force=True)
        self.assertEqual([m['text'] for m in self.messages() if m['role']=='assistant'],['Verified public body'])

    def test_restart_reuses_durable_checkpoint_without_replaying_or_mutating_lifecycle(self):
        self.start()
        self.finish()
        self.source([self.bubble('progress','Persisted public progress'),self.bubble('final','Native final body')],final='final')
        self.sync.poll(force=True)
        rows=self.messages()
        run=self.record()
        self.b.commands.close()
        self.b.history.close()
        self.b=self.fixture.b=Bridge(load_config(self.fixture.path),dry_run=True)
        self.sync=CursorPublicSync(self.b.child_events,self.store)
        self.b.child_events.public_sync=self.sync
        self.sync.poll(force=True)
        self.assertEqual(self.messages(),rows)
        self.assertEqual(self.record(),run)

    def test_terminal_lag_window_and_source_failures_are_bounded_and_private(self):
        self.start()
        self.finish()
        self.source([self.bubble('progress','password=private-root-secret')])
        self.sync.poll(force=True,now=self.fixture.start+10)
        self.assertNotIn('private-root-secret',str(self.messages()))
        self.assertIn('已遮蓋',str(self.messages()))
        self.store.results[self.run]=OSError('PRIVATE_NATIVE_DATABASE_PATH_AND_BODY')
        self.sync.poll(force=True,now=self.fixture.start+11)
        self.assertEqual(self.b.health['cursor_public_error'],'cursor_public_source_unavailable')
        self.assertNotIn('PRIVATE',str(self.b.health))
        count=len(self.store.calls)
        self.sync.poll(force=True,now=self.fixture.start+123)
        self.assertEqual(len(self.store.calls),count)

    def test_real_readonly_root_store_proves_request_span_and_public_only_enrichment(self):
        import test_cursor_root_public_store
        native=test_cursor_root_public_store.CursorRootPublicStoreTests()
        native.ROOT=self.credential['native_session_id']
        native.REQUEST=self.run
        native.setUp()
        self.addCleanup(native.doCleanups)
        native.bubble('private-tool',2,'PRIVATE_TOOL_BODY',capabilityType=15,
            toolFormerData={'params':'PRIVATE_TOOL_PARAMS'})
        native.flush()
        self.sync=CursorPublicSync(self.b.child_events,native.reader)
        self.b.child_events.public_sync=self.sync
        self.start()
        self.sync.poll(force=True)
        self.assertIn('Public root progress',[m['text'] for m in self.messages()])
        self.assertEqual(self.record()['status'],'working')
        native.bubble('actual-final',2,'Verified real-reader final')
        native.flush()
        self.finish(text='Verified real-reader final')
        native_run=self.record()
        self.sync.poll(force=True)
        self.assertEqual(self.record(),native_run)
        self.assertEqual([m['text'] for m in self.messages() if m['phase']=='final'],['Verified real-reader final'])
        self.assertNotIn('PRIVATE',str(self.messages()))

    def test_shared_poll_budget_visits_both_root_and_children_without_starvation(self):
        from unittest.mock import patch
        self.start()
        for number in range(1,4):
            self.fixture.send(self.fixture.event(number,parent_run_id=self.run,
                at=timestamp(self.fixture.start+3),native_alias={'kind':'tool_call','id':'task-'+str(number)}))
        self.source([self.bubble('progress','Root public progress')])
        clock=[100.0]
        visits=[]
        self.store.callback=lambda:(visits.append('root'),clock.__setitem__(0,clock[0]+.3))
        def child_snapshot(root,parent,parent_run,call,**kwargs):
            visits.append(call)
            clock[0]+=.3
            return {'conversation_id':'child-'+call,'boundary_id':'human-'+call,'messages':[
                self.bubble('progress','Child '+call)],'final_id':None,'complete':True}
        self.store.snapshot=child_snapshot
        with patch('cursor_public_sync.time.monotonic',side_effect=lambda:clock[0]):
            self.sync.poll(force=True)
            self.sync.poll(force=True)
        self.assertEqual(set(visits),{'root','task-1','task-2','task-3'})
        self.assertLessEqual(len(visits),4)


if __name__=='__main__':
    unittest.main()
