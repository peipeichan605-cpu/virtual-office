"""Native Cursor final projection must not duplicate its public response."""
import copy
import unittest

from event_ingest import EventConflict
from history import timestamp
import test_child_events


class CursorRootDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_child_events.ChildEventsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.b=self.fixture.b
        self.root=self.fixture.roots['cursor']
        self.credential=self.fixture.credential

    def event(self,ident,kind='message',text='Public final',**changes):
        data={'source_id':'cursor-native-root','event_id':ident,'run_id':'parent-run',
              'type':kind,'text':text,'at':timestamp(self.fixture.start+3)}
        if kind=='message':data.update(role='assistant',phase='commentary')
        data.update(changes)
        return data

    def native(self,data):
        return self.b.child_events.root_event(self.credential['observer_id'],
            self.credential['token'],data)

    def messages(self):
        return self.b.history.messages(self.b.history_agents(),self.root['id'],run='parent-run')

    def row(self,table='runs'):
        return dict(self.b.history.db.execute('SELECT * FROM '+table+' WHERE agent_id=? AND run_id=?',
            (self.root['id'],'parent-run')).fetchone())

    def test_native_final_collapses_only_trailing_projection_and_keeps_receipts(self):
        self.native(self.event('progress',text='Working on the public task'))
        self.native(self.event('response'))
        completed=self.event('stop','turn_completed',at=timestamp(self.fixture.start+5))
        receipt=self.native(completed)
        self.assertEqual(receipt['status'],'done')
        self.assertEqual([(m['phase'],m['text']) for m in self.messages()],
            [('prompt','Fixture root task'),('commentary','Working on the public task'),('final','Public final')])
        stored={r[0] for r in self.b.history.db.execute('SELECT event_id FROM event_receipts WHERE agent_id=?',
            (self.root['id'],))}
        self.assertTrue({'progress','response','stop'}.issubset(stored))
        raw=self.b.history.db.execute('SELECT event_id,phase,text FROM messages '
            'WHERE agent_id=? AND event_id IN (?,?) ORDER BY seq',
            (self.root['id'],'response','stop')).fetchall()
        self.assertEqual([tuple(r) for r in raw],
            [('response','final','Public final'),('stop','final','Public final')])
        row=self.row()
        self.assertEqual(row['finished_at'],completed['at'])
        self.assertEqual(row['delivery'],'Public final')
        self.assertEqual(row['status'],'done')

    def test_promotion_reaches_open_conversation_after_cursor_with_stable_event_id(self):
        self.native(self.event('progress',text='Earlier genuine progress'))
        self.native(self.event('response'))
        original=self.messages()[-1]
        self.assertEqual(original['phase'],'commentary')
        self.native(self.event('stop','turn_completed'))
        after=self.b.history.messages(self.b.history_agents(),self.root['id'],run='parent-run',
            after=original['seq'])
        self.assertEqual(len(after),1)
        self.assertEqual(after[0]['event_id'],original['event_id'])
        self.assertEqual(after[0]['phase'],'final')
        self.assertGreater(after[0]['seq'],original['seq'])
        self.assertEqual(after[0]['at'],original['at'])
        self.assertEqual([(m['phase'],m['text']) for m in self.messages() if m['role']=='assistant'],
            [('commentary','Earlier genuine progress'),('final','Public final')])

    def test_earlier_same_words_and_other_public_progress_are_not_removed(self):
        self.native(self.event('earlier',text='Public final'))
        self.native(self.event('progress',text='Distinct public progress'))
        self.native(self.event('response',text='Public final'))
        self.native(self.event('stop','turn_completed'))
        self.assertEqual([(m['phase'],m['text']) for m in self.messages() if m['role']=='assistant'],
            [('commentary','Public final'),('commentary','Distinct public progress'),('final','Public final')])

    def test_equality_uses_redacted_public_text(self):
        self.native(self.event('response',text='Result api_key=first-private-value'))
        self.native(self.event('stop','turn_completed',text='Result api_key=second-private-value'))
        public=[m for m in self.messages() if m['role']=='assistant']
        self.assertEqual(len(public),1)
        self.assertEqual(public[0]['phase'],'final')
        self.assertEqual(public[0]['text'],'Result api_key=[已遮蓋]')

    def test_duplicate_stop_keeps_later_commentary_and_all_native_times(self):
        self.native(self.event('response'))
        stop=self.event('stop','turn_completed')
        self.native(stop)
        self.native(self.event('later',text='Public final'))
        before=self.messages();run=self.row();live=self.row('live_runs')
        self.assertTrue(self.native(copy.deepcopy(stop))['duplicate'])
        self.assertEqual(self.messages(),before)
        self.assertEqual(self.row(),run)
        self.assertEqual(self.row('live_runs'),live)

    def test_late_same_final_response_after_stop_keeps_single_final_and_times(self):
        self.native(self.event('response'))
        self.native(self.event('stop','turn_completed'))
        finished=self.row()['finished_at']
        late=self.event('late',phase='final',at=timestamp(self.fixture.start+10))
        self.native(late)
        before=self.messages()
        self.assertEqual(len([m for m in before if m['phase']=='final']),1)
        self.assertEqual(len([m for m in before if m['phase']=='commentary']),0)
        self.assertEqual(self.row()['finished_at'],finished)
        self.assertTrue(self.native(late)['duplicate'])
        self.assertEqual(self.messages(),before)

    def test_replay_of_collapsed_response_keeps_its_receipt_without_reappearing(self):
        response=self.event('response')
        self.native(response)
        self.native(self.event('stop','turn_completed'))
        before=self.messages();run=self.row();live=self.row('live_runs')
        self.assertTrue(self.native(response)['duplicate'])
        self.assertEqual(self.messages(),before)
        self.assertEqual(self.row(),run)
        self.assertEqual(self.row('live_runs'),live)

    def test_late_final_can_collapse_trailing_response_after_empty_stop(self):
        self.native(self.event('stop','turn_completed',text=''))
        finished=self.row()['finished_at']
        self.native(self.event('response'))
        self.native(self.event('late',phase='final'))
        self.assertEqual([(m['phase'],m['text']) for m in self.messages() if m['role']=='assistant'],
            [('final','Public final')])
        self.assertEqual(self.row()['finished_at'],finished)

    def test_stale_run_failure_rolls_back_staged_projection_removal(self):
        self.native(self.event('response'))
        before=self.messages()
        self.fixture.root_start('cursor','new-run',self.fixture.start+4)
        with self.assertRaises(EventConflict):
            self.native(self.event('stale-stop','turn_completed'))
        self.assertEqual(self.messages(),before)
        self.assertIsNone(self.b.history.db.execute('SELECT 1 FROM event_receipts WHERE agent_id=? AND event_id=?',
            (self.root['id'],'stale-stop')).fetchone())

    def test_conflicting_duplicate_does_not_remove_later_projection(self):
        stop=self.event('stop','turn_completed')
        self.native(stop)
        self.native(self.event('later',text='Changed final'))
        before=self.messages();run=self.row();live=self.row('live_runs')
        with self.assertRaises(EventConflict):
            self.native(dict(stop,text='Changed final'))
        self.assertEqual(self.messages(),before)
        self.assertEqual(self.row(),run)
        self.assertEqual(self.row('live_runs'),live)

    def test_manual_cursor_reporting_does_not_collapse_public_messages(self):
        for data in [self.event('manual-response'),self.event('manual-stop','turn_completed')]:
            self.b.accept_agent_event(self.root,dict(data,source_id=self.root['source_id']))
        self.assertEqual([(m['phase'],m['text']) for m in self.messages() if m['role']=='assistant'],
            [('commentary','Public final'),('final','Public final')])

    def test_unrelated_provider_observer_does_not_collapse_public_messages(self):
        root=self.fixture.roots['grok_bot']
        metadata=self.b.child_events.create_observer({'agent_id':root['id'],'provider':'grok_bot',
            'native_session_id':'grok-native-root'})
        import json
        from pathlib import Path
        credential=json.loads(Path(metadata['credential_path']).read_text())
        for data in [self.event('grok-response'),self.event('grok-stop','turn_completed')]:
            self.b.child_events.root_event(credential['observer_id'],credential['token'],
                dict(data,source_id='grok-native-root'))
        public=self.b.history.messages(self.b.history_agents(),root['id'],run='parent-run')
        self.assertEqual([(m['phase'],m['text']) for m in public if m['role']=='assistant'],
            [('commentary','Public final'),('final','Public final')])

    def test_different_trailing_response_and_failed_terminal_are_not_collapsed(self):
        self.native(self.event('earlier',text='Public final'))
        self.native(self.event('latest',text='Other response'))
        self.native(self.event('stop','turn_failed'))
        self.assertEqual([(m['phase'],m['text']) for m in self.messages() if m['role']=='assistant'],
            [('commentary','Public final'),('commentary','Other response')])

    def test_final_does_not_remove_matching_earlier_nontrailing_progress(self):
        self.native(self.event('earlier',text='Public final'))
        self.native(self.event('latest',text='Other response'))
        self.native(self.event('stop','turn_completed'))
        self.assertEqual([(m['phase'],m['text']) for m in self.messages() if m['role']=='assistant'],
            [('commentary','Public final'),('commentary','Other response'),('final','Public final')])


if __name__=='__main__':
    unittest.main()
