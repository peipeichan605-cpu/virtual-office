import json
import os
from pathlib import Path
import tempfile
import sqlite3
import unittest
from unittest.mock import patch, MagicMock
from dashboard import CodexTail, CroutonSource, Bridge, utc


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'selected.jsonl'
        self.path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'chosen'}}) + '\n')
        self.tail = CodexTail('chosen', self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, kind, run='turn1', **extra):
        with self.path.open('a') as f:
            f.write(json.dumps({'timestamp': utc(), 'type': 'event_msg', 'payload': {'type': kind, 'turn_id': run, **extra}}) + '\n')

    def test_actual_start_and_completion_only(self):
        self.tail.read()
        self.add('task_started', prompt='PASSWORD MUST NEVER LEAVE')
        start = self.tail.read()
        self.assertEqual(start[0]['status'], 'working')
        self.assertNotIn('PASSWORD', json.dumps(start))
        self.add('task_complete')
        self.assertEqual(self.tail.read()[0]['status'], 'done')
        self.assertEqual(self.tail.read(), [])

    def test_old_run_cannot_finish_current(self):
        self.tail.read()
        self.add('task_started', 'new')
        self.tail.read()
        self.add('task_complete', 'old')
        self.assertEqual(self.tail.read(), [])
        self.assertEqual(self.tail.state[0], 'working')

    def test_incomplete_line_waits(self):
        self.tail.read()
        record = json.dumps({'timestamp': utc(), 'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'r'}})
        with self.path.open('a') as f:
            f.write(record[:15])
        self.assertEqual(self.tail.read(), [])
        with self.path.open('a') as f:
            f.write(record[15:] + '\n')
        self.assertEqual(self.tail.read()[0]['status'], 'working')

    def test_wrong_session_is_rejected(self):
        with self.assertRaises(ValueError):
            CodexTail('other', self.path).read()

    def test_historical_completion_is_not_new_success(self):
        self.add('task_complete')
        self.assertEqual(self.tail.read()[0]['status'], 'idle')

    def test_completed_envelope_with_error_is_not_success(self):
        self.tail.read()
        self.add('task_started')
        self.tail.read()
        self.add('task_complete', error={'message': 'unsupported model and private details'})
        events = self.tail.read()
        self.assertEqual(events[0]['status'], 'error')
        self.assertNotIn('private details', json.dumps(events))

    def test_truncation_is_unknown_not_completed(self):
        self.tail.read()
        self.path.write_text('')
        with self.assertRaises(ValueError):
            self.tail.read()


class SourceLookupTests(unittest.TestCase):
    def bridge(self):
        b = Bridge.__new__(Bridge)
        b.config = {'codex_db': '/test-only/missing.sqlite'}
        b.names, b.tails, b.health = {'chosen': 'Chosen'}, {}, {}
        return b

    def test_database_failure_does_not_stop_lookup_and_can_recover(self):
        b = self.bridge()
        for failure_on in ('connect', 'execute'):
            con = MagicMock()
            con.execute.side_effect = sqlite3.OperationalError('private filesystem detail')
            with patch('dashboard.sqlite3.connect', side_effect=sqlite3.OperationalError('private') if failure_on == 'connect' else None, return_value=con):
                b.load_paths()
            self.assertEqual(b.health['source_lookup_error'], 'OperationalError')
            self.assertEqual(b.tails, {})
        recovered = MagicMock()
        recovered.execute.return_value.fetchone.return_value = ('/test-only/selected.jsonl',)
        with patch('dashboard.sqlite3.connect', return_value=recovered):
            b.load_paths()
        self.assertEqual(b.tails['chosen'].identity, 'chosen')
        self.assertNotIn('source_lookup_error', b.health)
        recovered.close.assert_called_once()

    def test_existing_tail_survives_database_unavailability(self):
        b = self.bridge()
        tail = object()
        b.tails['chosen'] = tail
        with patch('dashboard.sqlite3.connect', side_effect=sqlite3.OperationalError('offline')) as connect:
            b.load_paths()
        connect.assert_not_called()
        self.assertIs(b.tails['chosen'], tail)


class CroutonTests(unittest.TestCase):
    def external_fixture(self, d):
        workspace = Path(d).resolve()
        root = workspace / 'runtime'
        (root / 'agents').mkdir(parents=True)
        folder = workspace / 'another-project' / 'work' / 'selected-run'
        folder.mkdir(parents=True)
        batch = '00000000-0000-0000-0000-000000000001'
        run = 'crouton-01-' + batch
        lease_path = root / 'agents' / 'crouton-01.lock'
        lease_path.write_text(json.dumps({'batch_id': batch, 'run_id': run, 'pid': os.getpid(), 'runDir': str(folder)}))
        record = {'batchId': batch, 'runId': run, 'status': 'running', 'pid': os.getpid()}
        (folder / 'status.json').write_text(json.dumps(record))
        return root, folder, lease_path, record

    def test_external_reserved_run_and_actual_completion(self):
        with tempfile.TemporaryDirectory() as d:
            root, folder, lease_path, record = self.external_fixture(d)
            source = CroutonSource(root, d)
            self.assertEqual(source.inspect('crouton-01')['status'], 'working')
            lease_path.unlink()
            record['status'] = 'completed'
            (folder / 'status.json').write_text(json.dumps(record))
            self.assertEqual(source.inspect('crouton-01')['status'], 'done')

    def test_external_run_requires_matching_identity_and_live_worker(self):
        with tempfile.TemporaryDirectory() as d:
            root, folder, _, record = self.external_fixture(d)
            source = CroutonSource(root, d)
            for changed in ({'runId': 'wrong'}, {'batchId': 'wrong'}, {'pid': -1}):
                (folder / 'status.json').write_text(json.dumps({**record, **changed}))
                with self.assertRaises(ValueError):
                    source.inspect('crouton-01')

    def test_external_path_requires_explicit_workspace_root(self):
        with tempfile.TemporaryDirectory() as d:
            root, _, _, _ = self.external_fixture(d)
            with self.assertRaises(ValueError):
                CroutonSource(root).inspect('crouton-01')

    def test_external_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root, folder, lease_path, _ = self.external_fixture(d)
            alias = Path(d).resolve() / 'alias'
            alias.symlink_to(folder, target_is_directory=True)
            lease = json.loads(lease_path.read_text())
            lease['runDir'] = str(alias)
            lease_path.write_text(json.dumps(lease))
            with self.assertRaises(ValueError):
                CroutonSource(root, d).inspect('crouton-01')

    def test_missing_lease_means_not_running(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'agents').mkdir()
            self.assertEqual(CroutonSource(d).inspect('crouton-01')['source_state'], 'not_running')

    def test_dead_worker_does_not_mean_success(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            batch = '00000000-0000-0000-0000-000000000001'
            run = 'crouton-01-' + batch
            (root / 'agents').mkdir()
            folder = root / 'batches' / batch / 'crouton-01'
            folder.mkdir(parents=True)
            (root / 'agents' / 'crouton-01.lock').write_text(json.dumps({'batch_id': batch, 'run_id': run, 'pid': os.getpid()}))
            (folder / 'status.json').write_text(json.dumps({'agent_id': 'crouton-01', 'run_id': run, 'status': 'running', 'workerPid': -1}))
            with self.assertRaises(ValueError):
                CroutonSource(root).inspect('crouton-01')


class EnrollmentTests(unittest.TestCase):
    def test_publisher_pause_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            b = Bridge.__new__(Bridge)
            b.health = {}
            b.backoff_path = Path(d) / 'backoff.json'
            b.backoff_path.write_text(json.dumps({'until': 9999999999}))
            b.restore_publish_pause()
            self.assertEqual(b.limit_until, 9999999999)
            self.assertEqual(b.health['last_publish_error'], 'HTTP 429')

    def test_local_snapshot_is_authoritative_even_when_publisher_is_paused(self):
        b = Bridge.__new__(Bridge)
        b.last, b.sources, b.counts = {}, {}, {'codex_events': 0}
        b.health = {'last_publish_error': 'HTTP 429'}
        b.config = {}
        b.history = MagicMock(); b.history.list.return_value = []
        b.history.latest_messages.return_value = {}
        b.commands=MagicMock();b.commands.capability.return_value={'can_send':False,'same_session':False}
        b.workspaces = MagicMock(); b.workspaces.catalog.return_value = []
        b.workspaces.for_agent.return_value = {'id':'workspace-unassigned','name':'未指定 Workspace'}
        b.agents = {'codex:chosen': {'id': 'codex:chosen', 'name': 'Chosen', 'provider': 'codex'}}
        b.enqueue = MagicMock()
        self.assertFalse(b.local_ready())
        event = {'status': 'working', 'step': '處理訊息', 'run_id': 'real-run'}
        with patch('dashboard.utc', side_effect=['2026-11-05T09:00:00Z', '2026-11-05T09:00:10Z']):
            b.report('Chosen', event, 'codex')
            first = b.work_snapshot()
            b.report('Chosen', event, 'codex')
        self.assertEqual(b.work_snapshot(), first)
        self.assertTrue(first['local_ok'])
        self.assertEqual(first['agents'][0]['state'], 'working')
        self.assertEqual(first['agents'][0]['state_changed_at'], '2026-11-05T09:00:00Z')
        b.enqueue.assert_called_once()

    def test_initial_idle_clears_prior_feed_state_once(self):
        b = Bridge.__new__(Bridge)
        b.last, b.sources, b.counts = {}, {}, {'crouton_events': 0}
        sent = []
        b.enqueue = sent.append
        event = {'status': 'idle', 'step': '按需啟動；目前未執行', 'initial': True}
        b.report('Crouton-01', event, 'crouton')
        b.report('Crouton-01', event, 'crouton')
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['status'], 'idle')

    def bridge(self, names):
        bridge = Bridge.__new__(Bridge)
        bridge.allowed_names = set(names)
        return bridge

    def row(self, message):
        return {'event': 'message', 'id': 'fixture', 'time': 1, 'message': json.dumps(message)}

    def test_roster_is_not_enrollment(self):
        b = self.bridge([])
        row = b.filter_record(self.row({'roster': ['uninvited', 'someone else']}))
        self.assertEqual(json.loads(row['message']), {'roster': []})

    def test_unselected_agent_cannot_enter_by_posting(self):
        b = self.bridge(['chosen'])
        self.assertIsNone(b.filter_record(self.row({'bot': 'uninvited', 'status': 'working'})))

    def test_selected_status_discards_free_text_and_prompt(self):
        b = self.bridge(['chosen'])
        row = b.filter_record(self.row({'bot': 'chosen', 'status': 'working', 'step': 'a secret', 'prompt': 'private conversation'}))
        self.assertEqual(json.loads(row['message']), {'bot': 'chosen', 'status': 'working', 'step': '處理訊息'})

    def test_account_usage_is_not_implicitly_selected(self):
        self.assertIsNone(self.bridge(['chosen']).filter_record(self.row({'type': 'usage', 'total_used_pct': 30})))


class MetadataAppearanceTests(unittest.TestCase):
    def metadata(self, agents):
        bridge = Bridge.__new__(Bridge)
        bridge.agents = {agent['id']: agent for agent in agents}
        emitted = []
        bridge.enqueue = emitted.append
        bridge.metadata()
        return emitted

    def test_grok_uses_original_orange_stem_by_provider_without_changing_other_looks(self):
        agents = [
            {'id': 'grok_bot:one', 'provider': 'grok_bot', 'name': 'Custom Grok name'},
            {'id': 'grok_bot:two', 'provider': 'grok_bot', 'name': 'Codex / misleading name'},
            {'id': 'codex:one', 'provider': 'codex', 'name': 'Codex / Existing'},
            {'id': 'cursor:one', 'provider': 'cursor', 'name': 'Cursor existing'},
            {'id': 'hermes:one', 'provider': 'hermes', 'name': 'Hermes existing'},
            {'id': 'generic:one', 'provider': 'generic', 'name': 'Grok Bot in a generic name'},
        ]
        emitted = self.metadata(agents)
        self.assertEqual(emitted[0], {'roster': [a['name'] for a in agents]})
        self.assertEqual(emitted[1]['groups']['Custom Grok name'], 'Grok Bot')
        looks = emitted[2]['looks']
        canonical = {'shape': 'sphere', 'color': '#FC6A14', 'accessory': 'stem'}
        self.assertEqual(looks['Custom Grok name'], canonical)
        self.assertEqual(looks['Codex / misleading name'], canonical)
        self.assertEqual(looks['Codex / Existing'], {'shape': 'rounded_cube', 'color': '#4087E8', 'accessory': 'antenna'})
        for name in ['Cursor existing', 'Hermes existing', 'Grok Bot in a generic name']:
            self.assertEqual(looks[name], {'shape': 'sphere', 'color': '#6CAC72', 'accessory': 'glasses'})

    def test_grok_metadata_remains_scoped_to_each_roster_chunk(self):
        agents = [{'id': 'generic:%d' % i, 'provider': 'generic', 'name': 'Agent %d' % i} for i in range(15)]
        agents.append({'id': 'grok_bot:last', 'provider': 'grok_bot', 'name': 'Last Grok'})
        emitted = self.metadata(agents)
        self.assertEqual(len(emitted), 6)
        for offset, expected in [(0, agents[:15]), (3, agents[15:])]:
            names = {a['name'] for a in expected}
            self.assertEqual(set(emitted[offset]['roster']), names)
            self.assertEqual(set(emitted[offset + 1]['groups']), names)
            self.assertEqual(set(emitted[offset + 2]['looks']), names)
        self.assertEqual(emitted[5]['looks']['Last Grok'], {'shape': 'sphere', 'color': '#FC6A14', 'accessory': 'stem'})


if __name__ == '__main__':
    unittest.main()
