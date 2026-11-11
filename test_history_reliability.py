"""History fixtures: display dedupe never replaces authenticated source evidence."""
import json
from pathlib import Path
import tempfile
import unittest

from event_ingest import EventConflict, ingest
from history import HistoryStore


class HistoryReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'history.sqlite3'
        self.store = HistoryStore(self.path)
        self.agent = {'id': 'grok_bot:fixture', 'source_id': 'fixture',
                      'provider': 'grok_bot', 'name': 'Selected fixture'}
        self.allowed = {self.agent['id']}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def event(self, kind, ident, second=0, **fields):
        return {'source_id': self.agent['source_id'], 'run_id': 'run-one',
                'event_id': ident, 'type': kind, 'at': '2026-01-02T03:04:%02dZ' % second,
                **fields}

    def accept(self, kind, ident, second=0, **fields):
        return ingest(self.store, self.agent, self.event(kind, ident, second, **fields))

    def row(self):
        return self.store.db.execute('SELECT * FROM runs WHERE agent_id=? AND run_id=?',
                                     (self.agent['id'], 'run-one')).fetchone()

    def messages(self, **options):
        return self.store.messages(self.allowed, self.agent['id'], **options)

    def test_public_final_and_terminal_keep_both_proofs_but_display_one_final(self):
        self.accept('turn_started', 'start', text='Do this task')
        self.accept('message', 'reply', 1, role='assistant', phase='final', text='Delivered')
        first = self.messages()[-1]
        self.accept('turn_completed', 'finish', 2, text='Delivered')

        self.assertEqual([row['text'] for row in self.messages()], ['Do this task', 'Delivered'])
        # Raw evidence stays addressable by its exact source event ID, as used
        # by authenticated terminal reconciliation and connection verification.
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM event_receipts').fetchone()[0], 3)
        stored = self.store.db.execute('SELECT event_id,text FROM messages WHERE phase=? ORDER BY seq',
                                      ('final',)).fetchall()
        self.assertEqual([tuple(row) for row in stored], [('reply', 'Delivered'), ('finish', 'Delivered')])
        self.assertEqual(self.messages(after=first['seq']), [])
        self.assertEqual(self.row()['delivery'], 'Delivered')
        # Identical replay remains a receipt replay, not another visible reply.
        self.assertTrue(self.accept('turn_completed', 'finish', 2, text='Delivered')['duplicate'])

    def test_existing_duplicates_are_filtered_before_both_pagination_directions(self):
        first = self.store.message(self.agent['id'], 'run-one', 'assistant', 'final',
                                   'Same public delivery', event_id='first')
        for number in range(120):
            self.store.message(self.agent['id'], 'run-one', 'assistant', 'final',
                               'Same public delivery', event_id='duplicate-%s' % number)
        later = self.store.message(self.agent['id'], 'run-two', 'assistant', 'commentary',
                                   'Later task progress', event_id='later')
        last = self.store.message(self.agent['id'], 'run-two', 'assistant', 'final',
                                  'New delivery', event_id='last')
        self.store.close()
        self.store = HistoryStore(self.path)
        self.assertEqual([row['seq'] for row in self.messages(limit=2)], [later, last])
        self.assertEqual([row['seq'] for row in self.messages(before=later, limit=2)], [first])
        self.assertEqual([row['seq'] for row in self.messages(after=first, limit=1)], [later])
        self.assertEqual([row['seq'] for row in self.messages(after=later, limit=1)], [last])
        self.assertEqual(self.messages(after=last, limit=1), [])
        self.assertEqual(self.messages(run='run-one', after=first, limit=1), [])

    def test_dedupe_is_scoped_to_agent_run_phase_and_redacted_public_text(self):
        agent = self.agent['id']
        self.store.message(agent, 'run-one', 'assistant', 'final', 'password=first-private', event_id='one')
        self.store.message(agent, 'run-one', 'assistant', 'final', 'password=second-private', event_id='two')
        self.store.message(agent, 'run-two', 'assistant', 'final', 'password=first-private', event_id='three')
        self.store.message(agent, 'run-one', 'assistant', 'commentary', 'password=first-private', event_id='four')
        self.store.message('grok_bot:other', 'run-one', 'assistant', 'final', 'password=first-private', event_id='five')
        self.assertEqual(len(self.messages()), 3)
        self.assertEqual(self.store.messages(self.allowed, 'grok_bot:other'), [])
        for secret in (b'first-private', b'second-private'):
            self.assertNotIn(secret, self.path.read_bytes())

    def test_late_final_enriches_delivery_without_refinishing_or_changing_status(self):
        self.accept('turn_started', 'start', text='Original public task')
        self.accept('turn_completed', 'finish', 1)
        finish = self.row()['finished_at']
        self.accept('message', 'late-public', 5, role='assistant', phase='final', text='Late public delivery')
        self.assertEqual(self.row()['status'], 'done')
        self.assertEqual(self.row()['finished_at'], finish)
        self.assertEqual(self.row()['prompt'], 'Original public task')
        self.assertEqual(self.row()['delivery'], 'Late public delivery')
        self.assertEqual([row['text'] for row in self.messages()], ['Original public task', 'Late public delivery'])
        live = self.store.db.execute('SELECT status FROM live_runs WHERE agent_id=?', (self.agent['id'],)).fetchone()
        self.assertEqual(live[0], 'done')
        with self.assertRaises(EventConflict):
            self.accept('heartbeat', 'late-heartbeat', 6)
        with self.assertRaises(EventConflict):
            self.accept('turn_failed', 'late-terminal', 6)

    def test_delayed_old_run_cannot_enrich_a_new_current_run(self):
        self.accept('turn_started', 'start', text='Original public task')
        self.accept('turn_completed', 'finish', 1)
        new = self.event('turn_started', 'new-start', 2, text='New task')
        new['run_id'] = 'run-two'
        ingest(self.store, self.agent, new)
        with self.assertRaises(EventConflict):
            self.accept('message', 'late-old-run', 5, role='assistant', phase='final', text='Not current')
        self.assertEqual(self.row()['delivery'], '')
        self.assertNotIn('Not current', json.dumps(self.messages()))

    def test_direct_source_replays_preserve_original_terminal_state_and_time(self):
        ident = self.store.record(self.agent['id'], 'grok_bot', 'Fixture', 'run-one', 'working', at='2026-01-02T03:04:00Z')
        self.store.record(self.agent['id'], 'grok_bot', 'Fixture', 'run-one', 'error', at='2026-01-02T03:04:01Z')
        finish = self.row()['finished_at']
        for state in ('working', 'unknown', 'done', 'cancelled', 'error'):
            self.store.record(self.agent['id'], 'grok_bot', 'Fixture', 'run-one', state,
                              at='2026-01-02T03:04:05Z', delivery='Enriched public detail')
            row = self.store.detail(ident, self.allowed)
            self.assertEqual(row['status'], 'error')
            self.assertEqual(row['finished_at'], finish)
        self.assertEqual(self.row()['delivery'], 'Enriched public detail')


if __name__ == '__main__':
    unittest.main()
