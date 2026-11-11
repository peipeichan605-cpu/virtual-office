import copy
import json
import time
import unittest
from unittest.mock import patch

from configuration import load_config
from cursor_public_sync import CursorPublicSync
from dashboard import Bridge
from history import timestamp
import test_child_events
import test_cursor_public_store


class FixtureStore:
    def __init__(self):
        self.results = {}
        self.calls = []
        self.callback = None

    def snapshot(self, root_id, parent_id, parent_run_id, tool_call_id, child_hint=None, terminal=False):
        self.calls.append((root_id, parent_id, parent_run_id, tool_call_id, child_hint, terminal))
        if self.callback:
            self.callback()
        value = self.results.get(tool_call_id)
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


class CursorPublicSyncTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_child_events.ChildEventsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.b = self.fixture.b
        self.store = FixtureStore()
        self.sync = CursorPublicSync(self.b.child_events, self.store)
        self.b.child_events.public_sync = self.sync
        self.children = {}

    def start(self, number=1, call=None, **changes):
        call = call or 'task-' + str(number)
        event = self.fixture.event(number, text='Delegated public task',
            native_alias={'kind': 'tool_call', 'id': call}, **changes)
        result = self.fixture.send(event)
        self.children[number] = result['agent_id']
        return result['agent_id']

    def finish(self, number=1, **changes):
        return self.fixture.send(self.fixture.event(number, kind='completed', text='',
            at=timestamp(self.fixture.start + 5), **changes))

    def source(self, number=1, messages=None, final=None, call=None):
        self.store.results[call or 'task-' + str(number)] = {
            'conversation_id': 'conversation-' + str(number), 'boundary_id': 'boundary-' + str(number),
            'messages': messages or [], 'final_id': final, 'complete': True}

    def bubble(self, ident, text, closed=True):
        return {'id': ident, 'text': text, 'at': timestamp(self.fixture.start + 3), 'closed': closed}

    def messages(self, number=1):
        return self.b.history.messages(self.b.history_agents(), self.children[number])

    def row(self, table='observed_agents', number=1):
        key = 'id' if table == 'observed_agents' else 'agent_id'
        return dict(self.b.history.db.execute('SELECT * FROM ' + table + ' WHERE ' + key + '=?', (self.children[number],)).fetchone())

    def test_closed_public_progress_appears_while_child_and_parent_still_work(self):
        self.start()
        self.source(messages=[self.bubble('progress', 'Public progress')])
        self.sync.poll(force=True)
        self.assertTrue(any(m['role'] == 'assistant' and m['text'] == 'Public progress' for m in self.messages()))
        self.assertEqual(self.row()['status'], 'working')
        parent = self.b.history.db.execute('SELECT status FROM live_runs WHERE agent_id=?', (self.fixture.roots['cursor']['id'],)).fetchone()
        self.assertEqual(parent[0], 'working')
        self.assertEqual(self.store.calls[0][:4], ('cursor-native-root', 'cursor-native-root', 'parent-run', 'task-1'))

    def test_unclosed_body_waits_and_native_storage_never_completes_work(self):
        self.start()
        self.source(messages=[self.bubble('growing', 'Not yet closed', False)], final='growing')
        self.sync.poll(force=True)
        self.assertFalse(any(m['role'] == 'assistant' for m in self.messages()))
        self.assertEqual(self.row()['status'], 'working')
        self.source(messages=[self.bubble('growing', 'Closed public message')], final='growing')
        self.sync.poll(force=True)
        public = [m for m in self.messages() if m['role'] == 'assistant']
        self.assertEqual(public[0]['phase'], 'commentary')
        self.assertEqual(self.row()['status'], 'working')
        self.assertEqual(self.row('runs')['delivery'], '')

    def test_stop_source_lag_then_true_final_retains_finish_status_and_no_duplicates(self):
        self.start()
        self.finish()
        native = self.row()
        finish = self.row('runs')['finished_at']
        live = self.row('live_runs')
        self.source(messages=[self.bubble('progress', 'Earlier public progress')], final=None)
        self.sync.poll(force=True)
        self.assertEqual(self.row('runs')['delivery'], '')
        self.source(messages=[self.bubble('progress', 'Earlier public progress'), self.bubble('final', 'Actual final body')], final='final')
        self.sync.poll(force=True)
        self.sync.poll(force=True)
        self.assertEqual(self.row('runs')['delivery'], 'Actual final body')
        self.assertEqual(self.row('runs')['finished_at'], finish)
        self.assertEqual(self.row('live_runs'), live)
        self.assertEqual(self.row()['terminal_at'], native['terminal_at'])
        self.assertEqual(self.row()['updated_at'], native['updated_at'])
        self.assertEqual([(m['phase'], m['text']) for m in self.messages() if m['role'] == 'assistant'],
                         [('commentary', 'Earlier public progress'), ('final', 'Actual final body')])

    def test_public_revision_uses_one_stable_bubble_and_upgrades_final_phase(self):
        self.start()
        self.source(messages=[self.bubble('one', 'Public first revision')])
        self.sync.poll(force=True)
        first = next(m for m in self.messages() if m['role'] == 'assistant')
        self.finish()
        self.source(messages=[self.bubble('one', 'Public completed revision')], final='one')
        self.sync.poll(force=True)
        public = [m for m in self.messages() if m['role'] == 'assistant']
        self.assertEqual(len(public), 1)
        self.assertEqual(public[0]['event_id'], first['event_id'])
        self.assertGreater(public[0]['seq'], first['seq'])
        self.assertEqual(public[0]['phase'], 'final')
        self.assertEqual(self.row('runs')['delivery'], 'Public completed revision')

    def test_late_native_public_hook_also_preserves_finish_and_retirement(self):
        self.start()
        self.finish()
        native = self.row()
        finish = self.row('runs')['finished_at']
        live = self.row('live_runs')
        self.fixture.send(self.fixture.event(kind='message', text='Late native public text',
            at=timestamp(self.fixture.start + 12)))
        self.assertEqual(self.row()['terminal_at'], native['terminal_at'])
        self.assertEqual(self.row('runs')['finished_at'], finish)
        self.assertEqual(self.row('live_runs'), live)

    def test_retired_child_final_enriches_history_without_scene_reappearance(self):
        self.start()
        self.finish()
        terminal = self.row()['terminal_at']
        self.source(messages=[self.bubble('final', 'Retained late final')], final='final')
        with patch('subagents.time.time', return_value=terminal + 31):
            self.assertEqual(self.fixture.children(), [])
            self.sync.poll(force=True, now=terminal + 31)
            self.assertEqual(self.fixture.children(), [])
        self.assertEqual(self.row('runs')['delivery'], 'Retained late final')
        self.assertEqual(self.row()['terminal_at'], terminal)

    def test_restart_preserves_public_checkpoint_and_does_not_duplicate_final(self):
        self.start()
        self.finish()
        self.source(messages=[self.bubble('final', 'Persistent final')], final='final')
        self.sync.poll(force=True)
        messages = self.messages()
        self.b.commands.close()
        self.b.history.close()
        self.b = self.fixture.b = Bridge(load_config(self.fixture.path), dry_run=True)
        self.sync = CursorPublicSync(self.b.child_events, self.store)
        self.b.child_events.public_sync = self.sync
        self.sync.poll(force=True)
        self.assertEqual(self.messages(), messages)
        self.assertEqual(self.row('runs')['delivery'], 'Persistent final')

    def test_logout_during_source_snapshot_cannot_commit_public_text(self):
        self.start()
        self.source(messages=[self.bubble('private', 'MUST_NOT_IMPORT_AFTER_LOGOUT')])
        self.store.callback = lambda: self.b.definitions.pop(self.fixture.roots['cursor']['id'], None)
        self.sync.poll(force=True)
        count = self.b.history.db.execute("SELECT count(*) FROM messages WHERE text LIKE '%MUST_NOT_IMPORT%'").fetchone()[0]
        self.assertEqual(count, 0)

    def test_generation_rebind_during_snapshot_cannot_commit_old_text(self):
        self.start()
        self.source(messages=[self.bubble('old', 'MUST_NOT_IMPORT_OLD_GENERATION')])
        self.store.callback = lambda: self.b.child_events.create_observer({'agent_id': self.fixture.roots['cursor']['id'],
            'provider': 'cursor', 'native_session_id': 'replacement-native-root'})
        self.sync.poll(force=True)
        count = self.b.history.db.execute("SELECT count(*) FROM messages WHERE text LIKE '%MUST_NOT_IMPORT%'").fetchone()[0]
        self.assertEqual(count, 0)

    def test_reused_child_new_run_does_not_import_or_mutate_old_run(self):
        self.start()
        self.finish()
        old_run = self.row('runs')
        self.fixture.root_start('cursor', 'parent-two', self.fixture.start + 6)
        self.start(call='task-two', event_id='second-start', parent_run_id='parent-two',
                   child_run_id='second-child-run', at=timestamp(self.fixture.start + 7))
        self.source(call='task-1', messages=[self.bubble('old', 'OLD_MUST_NOT_IMPORT')], final='old')
        self.source(call='task-two', messages=[self.bubble('new', 'Fresh new-run public')])
        self.sync.poll(force=True)
        self.assertEqual([c[3] for c in self.store.calls], ['task-two'])
        row = self.row()
        self.assertEqual(row['run_id'], 'second-child-run')
        self.assertEqual(row['status'], 'working')
        old = self.b.history.db.execute('SELECT * FROM runs WHERE id=?', (old_run['id'],)).fetchone()
        self.assertEqual(dict(old), old_run)

    def test_two_parallel_children_cannot_mix_public_messages(self):
        one = self.start(1)
        two = self.start(2)
        self.source(1, [self.bubble('same-bubble-name', 'First public child')])
        self.source(2, [self.bubble('same-bubble-name', 'Second public child')])
        self.sync.poll(force=True)
        self.assertEqual([m['text'] for m in self.messages(1) if m['role'] == 'assistant'], ['First public child'])
        self.assertEqual([m['text'] for m in self.messages(2) if m['role'] == 'assistant'], ['Second public child'])
        self.assertNotEqual(one, two)

    def test_budget_exhaustion_advances_by_processed_targets_without_starvation(self):
        for number in range(1, 4):
            self.start(number)
            self.source(number, [self.bubble('progress', 'Public child ' + str(number))])
        clock = [100.0]
        self.store.callback = lambda: clock.__setitem__(0, clock[0] + .3)
        with patch('cursor_public_sync.time.monotonic', side_effect=lambda: clock[0]):
            self.sync.poll(force=True)
            self.sync.poll(force=True)
        self.assertEqual(set(call[3] for call in self.store.calls), {'task-1', 'task-2', 'task-3'})
        self.assertLessEqual(len(self.store.calls), 4)

    def test_existing_exact_native_final_does_not_get_another_structured_final_row(self):
        self.start()
        self.fixture.send(self.fixture.event(kind='completed', text='Native final body',
            at=timestamp(self.fixture.start + 5)))
        self.source(messages=[self.bubble('final', 'Native final body')], final='final')
        self.sync.poll(force=True)
        self.sync.poll(force=True)
        final = [m for m in self.messages() if m['phase'] == 'final']
        self.assertEqual(len(final), 1)
        self.assertEqual(final[0]['text'], 'Native final body')

    def test_distinct_public_bubbles_with_identical_words_are_not_text_deduped(self):
        self.start()
        self.source(messages=[self.bubble('one', 'Repeated public words'), self.bubble('two', 'Repeated public words')])
        self.sync.poll(force=True)
        self.assertEqual(len([m for m in self.messages() if m['role'] == 'assistant']), 2)

    def test_native_opaque_bubble_identity_is_hashed_without_trimming_or_exposure(self):
        self.start()
        self.source(messages=[self.bubble('public\nopaque-id', 'Opaque identity public text')])
        self.store.results['task-1']['boundary_id'] = 'human\nopaque-boundary'
        self.sync.poll(force=True)
        public = [m for m in self.messages() if m['role'] == 'assistant']
        self.assertEqual(len(public), 1)
        self.assertTrue(public[0]['event_id'].startswith('cursor-public:'))
        self.assertNotIn('opaque-id', public[0]['event_id'])

    def test_real_structured_wal_reader_through_backend_preserves_public_only_delivery(self):
        native = test_cursor_public_store.CursorPublicStoreTests()
        native.ROOT = 'cursor-native-root'
        native.RUN = 'parent-run'
        native.setUp()
        self.addCleanup(native.tearDown)
        from cursor_public_store import hook_id
        self.start(call=hook_id(native.CALL))
        self.sync = CursorPublicSync(self.b.child_events, native.reader)
        self.b.child_events.public_sync = self.sync
        native.bubble('tool', 2, 'PRIVATE_TOOL_BODY', capabilityType=15, toolFormerData={'params': 'PRIVATE_TOOL_ARGUMENTS'})
        native.flush()
        self.sync.poll(force=True)
        self.assertEqual([m['text'] for m in self.messages() if m['role'] == 'assistant'], ['Public progress'])
        self.assertEqual(self.row()['status'], 'working')
        self.finish()
        native.bubble('thinking', 2, 'PRIVATE_REASONING', thinking={'text': 'PRIVATE_THOUGHT'}, capabilityType=30)
        native.bubble('final', 2, 'Actual structured delivery 437')
        native.flush()
        self.sync.poll(force=True)
        self.assertEqual(self.row('runs')['delivery'], 'Actual structured delivery 437')
        self.assertNotIn('PRIVATE', json.dumps(self.messages()))

    def test_quiet_child_alias_is_automatic_and_independent_enrollment_suppresses_projection(self):
        child = self.start()
        self.source(messages=[self.bubble('public', 'Do not merge independent root')])
        self.b.definitions['cursor:independent'] = {'id': 'cursor:independent', 'source_id': 'conversation-1', 'provider': 'cursor'}
        self.sync.poll(force=True)
        self.assertEqual(self.row()['suppressed'], 1)
        self.assertFalse(any(m['role'] == 'assistant' for m in self.messages()))
        self.assertFalse(self.b.history.db.execute("SELECT 1 FROM child_native_aliases WHERE child_id=? AND kind='conversation'", (child,)).fetchone())

    def test_expired_terminal_source_is_not_polled_and_unavailable_health_has_no_private_text(self):
        self.start()
        self.finish()
        terminal = self.row()['terminal_at']
        self.source(messages=[self.bubble('final', 'Too late')], final='final')
        self.sync.poll(force=True, now=terminal + 121)
        self.assertEqual(self.store.calls, [])
        self.store.results['task-1'] = OSError('PRIVATE_DATABASE_PATH_OR_DATA')
        self.sync.poll(force=True)
        self.assertEqual(self.b.health['cursor_public_error'], 'cursor_public_source_unavailable')
        self.assertNotIn('PRIVATE', json.dumps(self.b.health))

    def test_public_body_is_redacted_before_checkpoint_or_history_commit(self):
        self.start()
        self.finish()
        self.source(messages=[self.bubble('final', 'Public api_key=THIS_PRIVATE_VALUE_123456')], final='final')
        self.sync.poll(force=True)
        public = str(self.messages()) + self.row('runs')['delivery']
        self.assertNotIn('THIS_PRIVATE_VALUE', public)
        self.assertNotIn('THIS_PRIVATE_VALUE', str(list(self.b.history.db.execute('SELECT * FROM cursor_public_messages'))))


if __name__ == '__main__':
    unittest.main()
