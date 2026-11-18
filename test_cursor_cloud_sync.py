"""Synthetic public Cloud API contracts; these do not replace a live App trial."""
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.parse
import uuid

from commands import Commands
from cursor_cloud_sync import CursorCloudSync, public_prompt, source_time
from history import HistoryStore
from platform_adapters import RemoteError


SID = 'bc-00000000-0000-4000-8000-000000000019'
AGENT = 'cursor:' + SID
CUTOFF = '2026-11-13T01:00:00.000Z'


def run(ident, created='2026-11-13T01:01:00.000Z', status='FINISHED'):
    return {'id': 'run-' + ident, 'agentId': SID, 'status': status, 'createdAt': created}


def frame(kind, value):
    return ('event: ' + kind + '\ndata: ' + json.dumps(value) + '\n\n').encode()


class ReadTransport:
    def __init__(self):
        self.pages = {None: {'items': []}}
        self.details, self.streams, self.calls = {}, {}, []
        self.closed = 0

    def json(self, method, url, headers, data=None):
        assert method == 'GET', 'Observer must never write to the vendor'
        self.calls.append((method, url))
        parsed = urllib.parse.urlsplit(url)
        if parsed.path.endswith('/runs'):
            cursor = urllib.parse.parse_qs(parsed.query).get('cursor', [None])[0]
            value = self.pages[cursor]
        else:
            value = self.details[parsed.path.rsplit('/', 1)[-1]]
        if isinstance(value, Exception):
            raise value
        return value

    def open(self, method, url, headers):
        assert method == 'GET'
        self.calls.append((method, url))
        value = self.streams[url.split('/')[-2]]
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(close=self.close), io.BytesIO(value)

    def close(self):
        self.closed += 1


class Adapter:
    base = 'https://api.cursor.com/v1/agents/'
    mode, serial, same_session = 'cursor_cloud', True, True
    reason = None

    def __init__(self, transport):
        self.transport = transport
        self.posts = []

    def available(self, sid):
        return sid == SID

    def headers(self, sid):
        assert sid == SID
        return {}

    def submit(self, sid, text):
        self.posts.append((sid, text))
        return {'native_message_id': 'run-office-next'}


class CloudObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.history = HistoryStore(Path(self.tmp.name) / 'history.sqlite3', secrets=('fixture-secret-value',))
        self.definition = {'id': AGENT, 'source_id': SID, 'name': 'Selected fixture',
                           'provider': 'cursor', 'driver': 'cursor'}
        self.config = {'data_dir': self.tmp.name, 'topic': 'fixture-owner-topic',
                       'agents': [self.definition], 'command_bindings': {
                           AGENT: {'adapter': 'cursor_cloud', 'session_id': SID}}}
        self.transport = ReadTransport()
        self.adapter = Adapter(self.transport)
        self.commands = Commands(self.config, self.history, adapters={'cursor_cloud': self.adapter})
        self.statuses = []
        self.bridge = SimpleNamespace(config=self.config, history=self.history, commands=self.commands,
                                      definitions={AGENT: self.definition},
                                      native_status=lambda agent, event: self.statuses.append((agent, event)))
        self.sync = CursorCloudSync(self.bridge)
        self.commands.cloud_sync = self.sync
        with self.history.db:
            self.history.db.execute('UPDATE cursor_cloud_members SET joined_at=?,watermark=?', (CUTOFF, CUTOFF))

    def tearDown(self):
        self.commands.close()
        self.history.close()
        self.tmp.cleanup()

    def member(self):
        return dict(self.history.db.execute('SELECT * FROM cursor_cloud_members WHERE agent_id=?', (AGENT,)).fetchone())

    def rows(self):
        return [dict(row) for row in self.history.db.execute('SELECT * FROM cursor_cloud_runs ORDER BY created_at,native_run')]

    def discover(self):
        self.sync.discover(self.member(), self.adapter)

    def selected_row(self, ident='run-new'):
        row = next(row for row in self.rows() if row['native_run'] == ident)
        return dict(row, session_id=SID)

    def request(self):
        ident = str(uuid.uuid4())
        self.commands.create({'request_id': ident, 'agent_id': AGENT, 'text': 'Office synthetic prompt'})
        return ident

    def test_bootstrap_excludes_old_terminal_but_includes_active_and_fast_new_completion(self):
        self.transport.pages[None] = {'items': [run('new'), run('old-active', '2026-11-12T23:00:00Z', 'RUNNING'),
                                                run('old-done', '2026-11-12T22:00:00Z')]}
        self.discover()
        self.assertEqual({row['native_run'] for row in self.rows()}, {'run-new', 'run-old-active'})
        self.assertEqual(self.member()['watermark'], '2026-11-13T01:01:00.000Z')
        self.assertTrue(self.sync.busy(AGENT))

    def test_timestamp_ties_do_not_skip_later_discovered_run(self):
        self.transport.pages[None] = {'items': [run('first')]}
        self.discover()
        self.transport.pages[None] = {'items': [run('second'), run('first')]}
        self.discover()
        self.assertEqual({row['native_run'] for row in self.rows()}, {'run-first', 'run-second'})

    def test_page_budget_persists_cursor_without_advancing_watermark(self):
        self.sync.pages_per_tick = 1
        self.transport.pages = {
            None: {'items': [run('latest', '2026-11-13T01:03:00Z')], 'nextCursor': 'second'},
            'second': {'items': [run('middle', '2026-11-13T01:02:00Z')], 'nextCursor': 'third'},
            'third': {'items': [run('earliest'), run('old', '2026-11-12T23:00:00Z')]}}
        self.discover()
        self.assertEqual(self.member()['scan_cursor'], 'second')
        self.assertEqual(self.member()['watermark'], CUTOFF)
        self.discover()
        self.assertEqual(self.member()['scan_cursor'], 'third')
        self.assertEqual(self.member()['watermark'], CUTOFF)
        self.discover()
        self.assertIsNone(self.member()['scan_cursor'])
        self.assertEqual(self.member()['watermark'], '2026-11-13T01:03:00.000Z')
        self.assertEqual(len(self.rows()), 3)
        self.assertTrue(all('/' + SID + '/runs?' in url for _, url in self.transport.calls))

    def test_failed_second_page_retains_progress_and_does_not_duplicate(self):
        self.transport.pages = {None: {'items': [run('new')], 'nextCursor': 'later'},
                                'later': RemoteError('platform_read_failed')}
        with self.assertRaises(RemoteError):
            self.discover()
        self.assertEqual(self.member()['scan_cursor'], 'later')
        self.assertEqual(self.member()['watermark'], CUTOFF)
        self.transport.pages['later'] = {'items': [run('other', '2026-11-13T01:00:30Z')]}
        self.discover()
        self.assertEqual(len(self.rows()), 2)

    def test_wrong_agent_page_is_rejected_without_advancing_checkpoint(self):
        wrong = run('wrong')
        wrong['agentId'] = 'bc-00000000-0000-4000-8000-000000000099'
        self.transport.pages[None] = {'items': [wrong]}
        with self.assertRaises(RemoteError):
            self.discover()
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.member()['watermark'], CUTOFF)

    def test_office_native_id_dedup_and_uncertain_dispatch_deferral(self):
        ident = self.request()
        with self.commands.db:
            self.commands.db.execute("UPDATE commands SET state='sending' WHERE id=?", (ident,))
        self.assertEqual(self.sync.classify(AGENT, 'run-arrived-first'), 'deferred')
        with self.commands.db:
            self.commands.db.execute("UPDATE commands SET state='delivery_unknown' WHERE id=?", (ident,))
        self.assertEqual(self.sync.classify(AGENT, 'run-arrived-first'), 'native')
        self.assertEqual(self.commands.db.execute('SELECT state FROM commands WHERE id=?',(ident,)).fetchone()[0], 'delivery_unknown')
        with self.commands.db:
            self.commands.db.execute("UPDATE commands SET native_message_id=?,state='completed' WHERE id=?", ('run-arrived-first', ident))
        self.assertEqual(self.sync.classify(AGENT, 'run-arrived-first'), 'office')
        self.assertEqual(self.sync.classify(AGENT, 'run-app'), 'native')
        self.transport.pages[None] = {'items': [run('arrived-first')]}
        self.discover()
        row = self.selected_row('run-arrived-first')
        self.assertFalse(self.sync.project(row, 'prompt', text='Do not duplicate'))
        self.assertEqual(self.history.messages({AGENT}, AGENT), [])

    def test_logout_rejoin_fences_old_generation_even_with_same_native_id(self):
        self.transport.pages[None] = {'items': [run('new')]}
        self.discover()
        old = self.selected_row()
        self.config['agents'] = []
        self.sync.sync_members()
        self.assertFalse(self.sync.project(old, 'prompt', text='late old message'))
        self.config['agents'] = [self.definition]
        self.sync.sync_members()
        self.assertNotEqual(self.member()['generation'], old['generation'])
        self.assertFalse(self.sync.project(old, 'finished', state='completed', text='late old result'))
        self.assertEqual(self.history.messages({AGENT}, AGENT), [])

    def test_membership_generation_and_checkpoint_survive_observer_restart(self):
        self.transport.pages[None] = {'items': [run('new')]}
        self.discover()
        old = self.member()
        self.sync.close()
        self.sync = CursorCloudSync(self.bridge)
        self.commands.cloud_sync = self.sync
        self.assertEqual(self.member()['generation'], old['generation'])
        self.assertEqual(self.member()['watermark'], old['watermark'])

    def test_public_prompt_requires_exact_type_and_native_run(self):
        value = {'type': 'user-message-appended', 'userMessage': {
            'type': 'user_message', 'session_id': 'run-new', 'text': 'public question'}}
        self.assertEqual(public_prompt(value, 'run-new'), 'public question')
        self.assertIsNone(public_prompt(value, 'run-other'))
        self.assertIsNone(public_prompt({'text': 'not a public envelope'}, 'run-new'))
        self.assertIsNone(public_prompt(dict(value, type='thinking'), 'run-new'))
        value['userMessage']['type'] = 'tool_result'
        self.assertIsNone(public_prompt(value, 'run-new'))

    def test_stream_public_only_redacted_and_replay_deduplicated(self):
        self.transport.pages[None] = {'items': [run('new')]}
        self.discover()
        row = self.selected_row()
        self.transport.streams['run-new'] = (
            frame('interaction_update', {'type': 'user-message-appended', 'userMessage': {
                'type': 'user_message', 'session_id': 'run-new', 'text': 'hello fixture-secret-value'}})
            + frame('status', {'runId': 'run-new', 'status': 'RUNNING'})
            + frame('thinking', {'text': 'private-thinking-do-not-project'})
            + frame('assistant', {'text': 'checking '})
            + frame('tool_call', {'status': 'running', 'args': 'private-tool-args', 'result': 'private-tool-result'})
            + frame('assistant', {'text': 'answer'})
            + frame('result', {'runId': 'run-new', 'status': 'FINISHED', 'text': 'answer'})
            + frame('done', {}))
        self.sync.observe(row, self.adapter, threading.Event())
        self.sync.observe(row, self.adapter, threading.Event())
        messages = self.history.messages({AGENT}, AGENT)
        self.assertEqual([message['phase'] for message in messages], ['prompt', 'commentary', 'final'])
        public = json.dumps(messages)
        for value in ['private-thinking', 'private-tool-args', 'private-tool-result', 'fixture-secret-value']:
            self.assertNotIn(value, public)
        runs = self.history.list({AGENT}, AGENT, terminal=True)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]['status'], 'done')
        self.assertEqual(self.rows()[0]['complete'], 1)
        self.assertEqual(self.adapter.posts, [])

    def test_failed_and_cancelled_runs_never_project_success(self):
        for ident, state, expected in [('error', 'ERROR', 'error'), ('cancelled', 'CANCELLED', 'cancelled'),
                                       ('expired', 'EXPIRED', 'error')]:
            self.transport.pages[None] = {'items': [run(ident)]}
            self.discover()
            self.transport.streams['run-' + ident] = frame('result', {
                'runId': 'run-' + ident, 'status': state, 'text': 'public failure detail'})
            row = self.selected_row('run-' + ident)
            self.sync.observe(row, self.adapter, threading.Event())
            record = self.history.db.execute('SELECT status FROM runs WHERE run_id=?', (row['local_run'],)).fetchone()
            self.assertEqual(record['status'], expected)

    def test_expired_stream_get_recovers_terminal_without_invented_prompt(self):
        self.transport.pages[None] = {'items': [run('new')]}
        self.discover()
        self.transport.streams['run-new'] = RemoteError('platform_stream_expired')
        self.transport.details['run-new'] = dict(run('new'), result='authoritative public final')
        self.sync.observe(self.selected_row(), self.adapter, threading.Event())
        messages = self.history.messages({AGENT}, AGENT)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]['phase'], 'final')
        self.assertEqual(messages[0]['text'], 'authoritative public final')

    def test_older_recovery_cannot_override_newer_visible_work(self):
        self.transport.pages[None] = {'items': [run('new', '2026-11-13T01:02:00Z', 'RUNNING'), run('old')]}
        self.discover()
        new, old = self.selected_row('run-new'), self.selected_row('run-old')
        self.sync.project(new, 'started')
        self.sync.project(old, 'finished', state='completed', text='older final')
        self.assertEqual(len(self.statuses), 1)
        self.assertEqual(self.statuses[-1][1]['run_id'], new['local_run'])
        self.assertEqual(self.statuses[-1][1]['type'], 'started')

    def test_external_work_holds_office_queue_until_terminal(self):
        self.transport.pages[None] = {'items': [run('new', status='RUNNING')]}
        self.discover()
        ident = self.request()
        self.commands.tick()
        self.assertEqual(self.adapter.posts, [])
        state = self.commands.db.execute('SELECT state FROM commands WHERE id=?', (ident,)).fetchone()[0]
        self.assertEqual(state, 'pending')
        self.sync.project(self.selected_row(), 'finished', state='completed', text='app work done')
        self.assertFalse(self.sync.busy(AGENT))
        self.commands.tick()
        self.assertEqual(len(self.adapter.posts), 1)

    def test_invalid_timezone_timestamp_is_not_replaced_with_local_now(self):
        for value in ['bad-date', '2026-11-13T01:00:00', None]:
            with self.assertRaises(RemoteError):
                source_time(value)

    def new_row(self):
        self.transport.pages[None] = {'items': [run('new')]}
        self.discover()
        return self.selected_row()

    def saved(self, row):
        return dict(self.history.db.execute('SELECT * FROM runs WHERE run_id=?', (row['local_run'],)).fetchone())

    def test_foreign_assistant_frame_never_projects_body_or_falls_back_to_success(self):
        row = self.new_row()
        self.transport.streams['run-new'] = (
            frame('status', {'runId': 'run-new', 'status': 'RUNNING'})
            + frame('assistant', {'runId': 'run-foreign', 'text': 'FOREIGN_BODY_MUST_NOT_PROJECT'}))
        self.transport.details['run-new'] = dict(run('new'), result='Must not recover a mismatched stream')
        self.sync.observe(row, self.adapter, threading.Event())
        self.assertEqual(self.history.messages({AGENT}, AGENT), [])
        self.assertEqual(self.saved(row)['status'], 'unknown')
        self.assertFalse(any(url.endswith('/run-new') for _, url in self.transport.calls))

    def test_second_public_user_append_preserves_both_stable_ordinals(self):
        row = self.new_row()
        data = b''
        for text in ['first public question', 'second public clarification']:
            data += frame('interaction_update', {'type': 'user-message-appended', 'userMessage': {
                'type': 'user_message', 'session_id': 'run-new', 'text': text}})
        self.transport.streams['run-new'] = data + frame('result', {
            'runId': 'run-new', 'status': 'FINISHED', 'text': 'public final'})
        self.sync.observe(row, self.adapter, threading.Event())
        self.sync.observe(row, self.adapter, threading.Event())
        prompts = [m for m in self.history.messages({AGENT}, AGENT) if m['role'] == 'user']
        self.assertEqual([m['text'] for m in prompts], ['first public question', 'second public clarification'])
        self.assertEqual([m['event_id'].rsplit(':', 1)[-1] for m in prompts], ['0', '1'])

    def test_expired_stream_keeps_earlier_commentary_and_replaces_latest_segment(self):
        row = self.new_row()
        self.sync.project(row, 'prompt', text='persisted public question')
        self.sync.project(row, 'text', text='earlier commentary', segment=0)
        self.sync.project(row, 'text', text='partial latest segment', segment=1)
        self.transport.streams['run-new'] = RemoteError('platform_stream_expired')
        self.transport.details['run-new'] = dict(run('new'), result='authoritative final')
        self.sync.observe(row, self.adapter, threading.Event())
        messages = self.history.messages({AGENT}, AGENT)
        self.assertEqual([(m['phase'], m['text']) for m in messages], [
            ('prompt', 'persisted public question'), ('commentary', 'earlier commentary'), ('final', 'authoritative final')])
        self.assertEqual(self.sync.decorate_run(self.saved(row))['sync_warning'], 'cursor_cloud_transcript_gap')

    def test_clean_stream_eof_before_result_exposes_transcript_gap(self):
        row = self.new_row()
        self.transport.streams['run-new'] = frame('interaction_update', {
            'type': 'user-message-appended', 'userMessage': {
                'type': 'user_message', 'session_id': 'run-new', 'text': 'question before truncated stream'}})
        self.transport.details['run-new'] = dict(run('new'), result='recovered public final')
        self.sync.observe(row, self.adapter, threading.Event())
        self.assertEqual(self.saved(row)['status'], 'done')
        self.assertEqual(self.sync.decorate_run(self.saved(row))['sync_warning'], 'cursor_cloud_transcript_gap')

    def test_uncertain_command_does_not_block_neutral_native_history_or_get_reconciled(self):
        ident = self.request()
        with self.commands.db:
            self.commands.db.execute("UPDATE commands SET state='delivery_unknown' WHERE id=?", (ident,))
        row = self.new_row()
        self.assertEqual(row['origin'], 'native')
        self.sync.project(row, 'prompt', text='native public prompt')
        self.sync.project(row, 'finished', state='completed', text='native public result')
        command = self.commands.db.execute('SELECT * FROM commands WHERE id=?', (ident,)).fetchone()
        self.assertEqual(command['state'], 'delivery_unknown')
        self.assertIsNone(command['native_message_id'])
        self.assertEqual(self.saved(row)['source_kind'], 'cursor_cloud_native')
        self.assertEqual(self.saved(row)['status'], 'done')

    def test_per_run_read_retries_back_off_and_completed_worker_records_are_pruned(self):
        row = self.new_row()
        key = (AGENT, row['generation'], 'run-new')
        self.transport.streams['run-new'] = RemoteError('platform_read_failed')
        self.transport.details['run-new'] = RemoteError('platform_read_failed')
        with patch('cursor_cloud_sync.time.monotonic', return_value=100):
            self.sync.observe(row, self.adapter, threading.Event())
        self.assertEqual(self.sync.retry_after[key], 106)
        with patch('cursor_cloud_sync.time.monotonic', return_value=106):
            self.sync.observe(row, self.adapter, threading.Event())
        self.assertEqual(self.sync.retry_after[key], 118)
        dead = SimpleNamespace(is_alive=lambda: False)
        self.sync.workers[key] = dead
        self.sync.worker_stops[key] = threading.Event()
        with patch('cursor_cloud_sync.time.monotonic', return_value=107), patch('cursor_cloud_sync.threading.Thread') as launch:
            self.sync.launch_pending(self.adapter)
            launch.assert_not_called()
        self.assertNotIn(key, self.sync.workers)
        self.assertNotIn(key, self.sync.worker_stops)
        self.assertEqual(self.sync.retry_after[key], 118)
        self.sync.workers[key] = dead
        self.sync.worker_stops[key] = threading.Event()
        self.sync.project(row, 'finished', state='completed', text='finally complete')
        self.sync.launch_pending(self.adapter)
        self.assertNotIn(key, self.sync.workers)
        self.assertNotIn(key, self.sync.retry_after)
        self.assertNotIn(key, self.sync.run_failures)

    def test_active_run_starts_before_backlog_and_newer_discovery_fences_old_visible_state(self):
        self.transport.pages[None] = {'items': [run('newer-terminal', '2026-11-13T01:03:00Z'),
                                                run('active', '2026-11-13T01:02:00Z', 'RUNNING'), run('old')]}
        self.discover()
        self.sync.project(self.selected_row('run-old'), 'finished', state='completed', text='old recovery')
        self.assertEqual(self.statuses, [], 'Newer discovered run must fence old visible state before its stream opens')
        self.sync.max_workers = 1
        with patch('cursor_cloud_sync.threading.Thread') as launch:
            launch.return_value.is_alive.return_value = True
            self.sync.launch_pending(self.adapter)
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(launch.call_args.kwargs['args'][0]['native_run'], 'run-active')
        self.sync.workers.clear()
        self.sync.worker_stops.clear()

    def test_replay_prefix_never_shrinks_public_bubble_or_advances_cursor(self):
        row = self.new_row()
        self.sync.project(row, 'text', segment=0, text='Previously streamed public sentence')
        initial = self.history.messages({AGENT}, AGENT)[0]
        for prefix in ['P', 'Previously', 'Previously streamed', 'Previously streamed public sentence']:
            self.sync.project(row, 'text', segment=0, text=prefix)
            current = self.history.messages({AGENT}, AGENT)[0]
            self.assertEqual((current['text'],current['seq']), (initial['text'],initial['seq']))
        self.sync.project(row, 'text', segment=0, text='Previously streamed public sentence, now extended')
        extended = self.history.messages({AGENT}, AGENT)[0]
        self.assertGreater(extended['seq'],initial['seq'])
        self.assertTrue(extended['text'].endswith('extended'))
        self.sync.project(row, 'text', segment=0, text='Short authoritative final', final=True)
        final = self.history.messages({AGENT}, AGENT)[0]
        self.assertEqual((final['phase'],final['text']), ('final','Short authoritative final'))
        self.sync.project(row, 'text', segment=0, text='Previously streamed public sentence, now extended again')
        self.assertEqual(self.history.messages({AGENT}, AGENT)[0], final)

    def test_close_tolerates_socket_close_error_and_description_after_database_close(self):
        closed = []
        def bad_close():
            raise OSError('already closed')
        self.sync.connections[('fixture','one','run')] = SimpleNamespace(close=bad_close)
        self.sync.connections[('fixture','two','run')] = SimpleNamespace(close=lambda: closed.append(True))
        self.sync.close()
        self.assertEqual(closed, [True])
        with patch.object(self.history, 'db', None):
            self.assertIsNone(self.sync.describe(AGENT))
        self.sync.connections.clear()

    def held_stream(self, row):
        key = (AGENT,row['generation'],row['native_run'])
        event = threading.Event()
        self.sync.workers[key] = SimpleNamespace(is_alive=lambda: True,join=lambda timeout: None)
        self.sync.worker_stops[key] = event
        self.sync.connections[key] = SimpleNamespace(close=self.transport.close)
        return key,event

    def test_terminal_discovery_reconciles_open_stream_with_exact_get_and_fences_late_events(self):
        row = self.new_row()
        self.sync.project(row,'prompt',text='original native question')
        self.sync.project(row,'started')
        self.sync.project(row,'text',segment=0,text='early commentary')
        self.sync.project(row,'text',segment=1,text='partial final')
        key,event = self.held_stream(row)
        self.transport.details['run-new'] = dict(run('new'),result='authoritative completed reply')
        with patch('cursor_cloud_sync.time.monotonic',return_value=100):
            self.sync.reconcile_terminal_streams(self.member(),self.adapter)
        self.assertEqual(self.saved(row)['status'],'working')
        self.assertFalse(event.is_set())
        with patch('cursor_cloud_sync.time.monotonic',return_value=103):
            self.sync.reconcile_terminal_streams(self.member(),self.adapter)
        self.assertTrue(event.is_set())
        self.assertEqual(self.transport.closed,1)
        self.assertEqual(self.saved(row)['status'],'done')
        self.assertEqual(self.saved(row)['delivery'],'authoritative completed reply')
        before = self.history.messages({AGENT},AGENT)
        self.assertEqual([(m['phase'],m['text']) for m in before],[
            ('prompt','original native question'),('commentary','early commentary'),('final','authoritative completed reply')])
        last = self.statuses[-1]
        for kind,extra in [('started',{}),('status',{'step':'late work'}),
                           ('text',{'text':'late replay','segment':1}),
                           ('interrupted',{'state':'delivery_unknown'})]:
            self.assertFalse(self.sync.project(row,kind,**extra))
        self.assertEqual(self.statuses[-1],last)
        self.assertEqual(self.history.messages({AGENT},AGENT),before)
        self.assertEqual(self.selected_row()['complete'],1)
        self.sync.reconcile_terminal_streams(self.member(),self.adapter)
        self.assertEqual(sum(url.endswith('/run-new') for _,url in self.transport.calls),1)
        self.sync.workers.clear()
        self.sync.connections.clear()

    def test_terminal_list_never_finishes_without_matching_authoritative_terminal_detail(self):
        row = self.new_row()
        self.sync.project(row,'prompt',text='public question')
        self.sync.project(row,'started')
        key,event = self.held_stream(row)
        self.sync.terminal_seen[key] = 100
        self.transport.details['run-new'] = dict(run('new',status='RUNNING'))
        with patch('cursor_cloud_sync.time.monotonic',return_value=105):
            self.sync.reconcile_terminal_streams(self.member(),self.adapter)
        self.assertFalse(event.is_set())
        self.assertEqual(self.saved(row)['status'],'working')
        self.transport.details['run-new'] = dict(run('new',status='ERROR'),result='native failure')
        with patch('cursor_cloud_sync.time.monotonic',return_value=108):
            self.sync.reconcile_terminal_streams(self.member(),self.adapter)
        self.assertEqual(self.saved(row)['status'],'error')
        self.assertTrue(event.is_set())
        self.sync.workers.clear()
        self.sync.connections.clear()

    def test_missing_prompt_terminal_stream_has_bounded_grace_and_visible_gap(self):
        row = self.new_row()
        key,event = self.held_stream(row)
        self.sync.terminal_seen[key] = 100
        self.transport.details['run-new'] = dict(run('new'),result='native reply')
        with patch('cursor_cloud_sync.time.monotonic',return_value=110):
            self.sync.reconcile_terminal_streams(self.member(),self.adapter)
        self.assertFalse(event.is_set())
        self.assertFalse(any(url.endswith('/run-new') for _,url in self.transport.calls))
        with patch('cursor_cloud_sync.time.monotonic',return_value=115):
            self.sync.reconcile_terminal_streams(self.member(),self.adapter)
        self.assertTrue(event.is_set())
        self.assertEqual(self.sync.decorate_run(self.saved(row))['sync_warning'],'cursor_cloud_prompt_unavailable')
        self.sync.workers.clear()
        self.sync.connections.clear()


if __name__ == '__main__':
    unittest.main()
