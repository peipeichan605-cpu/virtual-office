"""Local fake-transport fixtures; no live credentials, dispatch or service changes."""
import hashlib
import json
import sqlite3
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from commands import CommandError
from dashboard import proxy_handler
from event_ingest import EventConflict, ingest
import test_platforms as fixtures


class GrokPairingTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp
    tearDown = fixtures.PlatformTests.tearDown
    grok = fixtures.PlatformTests.grok
    submit = fixtures.PlatformTests.submit
    wait = fixtures.PlatformTests.wait

    def queued(self):
        agent = self.grok()
        self.calls = []
        calls = self.calls
        class Transport:
            def open(self, method, url, headers, body):
                calls.append(json.loads(body))
                return fixtures.Connection(), fixtures.Response()
        self.bridge.commands.adapters['grok_webhook'].transport = Transport()
        request = self.submit(agent['id'], 'Exact original\n  task with Unicode 中文')
        self.wait(request, {'queued'})
        start = {'source_id': agent['source_id'], 'run_id': request['request_id'],
                 'event_id': request['request_id'] + ':start', 'type': 'turn_started',
                 'workspace_path': str(self.root / 'approved-workspace'), 'text': calls[0]['prompt']['text']}
        return agent, request, start

    def snapshot(self):
        history = self.bridge.history.db
        tables = ['runs', 'live_runs', 'event_receipts', 'messages', 'workspaces', 'agent_workspaces']
        return {t: [tuple(r) for r in history.execute('SELECT * FROM ' + t)] for t in tables}

    def legacy(self):
        agent, request, start = self.queued()
        # Reproduce the previously accepted malformed start through the old
        # generic ingestion contract, not through the hardened live endpoint.
        wrong = {**start, 'text': '[OFFICE:' + request['request_id'] + ']\nShort summary'}
        ingest(self.bridge.history, agent, wrong)
        terminal = {'source_id': agent['source_id'], 'run_id': request['request_id'],
                    'event_id': request['request_id'] + ':done', 'type': 'turn_completed',
                    'text': 'Actually completed the synthetic report.\n'}
        ingest(self.bridge.history, agent, terminal)
        proof = {'agent_id': agent['id'], 'command_id': request['request_id'],
                 'original_text': request['text'], 'terminal_event': terminal,
                 'review_note': 'Owner independently verified report, tests and background receipt.',
                 'validate_only': True}
        return agent, request, start, proof

    def test_changed_prompt_is_rejected_before_any_history_or_workspace_mutation(self):
        agent, request, start = self.queued()
        before = self.snapshot()
        for text in (start['text'] + ' changed', 'Only a summary', start['text'].replace('task', 'taks')):
            with self.subTest(text=text), self.assertRaises(EventConflict):
                self.bridge.accept_agent_event(agent, {**start, 'text': text})
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.wait(request, {'queued'})['run_id'], None)

    def test_unknown_noncanonical_or_wrong_run_markers_are_rejected(self):
        agent, request, start = self.queued()
        before = self.snapshot()
        cases = [
            {**start, 'text': '[OFFICE:' + str(uuid.uuid4()) + ']\nUnknown'},
            {**start, 'text': '[OFFICE:------------------------------------]\nInvalid'},
            {**start, 'run_id': str(uuid.uuid4())},
            {**start, 'text': '  ' + start['text']},
            {**start, 'text': '[OFFICE:broken]'},
        ]
        for event in cases:
            with self.subTest(event=event), self.assertRaises(EventConflict):
                self.bridge.accept_agent_event(agent, event)
            self.assertEqual(self.snapshot(), before)

    def test_other_agent_uuid_adapter_session_and_pending_state_are_rejected(self):
        agent, request, start = self.queued()
        before = self.snapshot()
        command = self.bridge.commands
        for column, value in [('agent_id', 'grok_bot:another'), ('adapter', 'agent_inbox'),
                              ('session_id', 'another-session'), ('state', 'pending')]:
            old = command.db.execute('SELECT ' + column + ' FROM commands WHERE id=?', (request['request_id'],)).fetchone()[0]
            command.db.execute('UPDATE commands SET ' + column + '=? WHERE id=?', (value, request['request_id']))
            command.db.commit()
            with self.subTest(column=column), self.assertRaises(EventConflict):
                self.bridge.accept_agent_event(agent, start)
            self.assertEqual(self.snapshot(), before)
            command.db.execute('UPDATE commands SET ' + column + '=? WHERE id=?', (old, request['request_id']))
            command.db.commit()

    def test_manual_unrelated_grok_turn_is_still_valid(self):
        agent = self.grok()
        event = {'source_id': agent['source_id'], 'run_id': 'manual-new-run',
                 'event_id': 'manual-start', 'type': 'turn_started', 'text': 'An unrelated manual conversation'}
        self.assertTrue(self.bridge.accept_agent_event(agent, event)['accepted'])

    def test_exact_start_and_identical_duplicate_remain_valid_after_terminal(self):
        agent, request, start = self.queued()
        self.bridge.accept_agent_event(agent, start)
        self.assertEqual(self.wait(request, {'running'})['run_id'], request['request_id'])
        done = {k: v for k, v in {**start, 'type': 'turn_completed',
            'event_id': request['request_id'] + ':done', 'text': 'Real final'}.items() if k != 'workspace_path'}
        self.bridge.accept_agent_event(agent, done)
        before = self.snapshot()
        self.assertTrue(self.bridge.accept_agent_event(agent, start)['duplicate'])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.wait(request, {'completed'})['state'], 'completed')

    def test_corrected_start_cannot_replace_a_previously_accepted_bad_event(self):
        agent, request, start, proof = self.legacy()
        before = self.snapshot()
        with self.assertRaises(EventConflict):
            self.bridge.accept_agent_event(agent, start)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.wait(request, {'queued'})['state'], 'queued')

    def test_owner_dry_run_and_atomic_apply_preserve_all_original_history(self):
        agent, request, start, proof = self.legacy()
        before = self.snapshot()
        command = self.bridge.commands
        checked = command.reconcile_grok_terminal(agent, proof)
        self.assertTrue(checked['validated']); self.assertFalse(checked['applied'])
        self.assertEqual(command.db.execute('SELECT count(*) FROM command_reconciliations').fetchone()[0], 0)
        self.assertEqual(self.wait(request, {'queued'})['run_id'], None)
        self.assertEqual(self.snapshot(), before)
        result = command.reconcile_grok_terminal(agent, {**proof, 'validate_only': False})
        self.assertTrue(result['applied']); self.assertFalse(result['duplicate'])
        self.assertEqual(self.wait(request, {'completed'})['detail_code'], 'owner_reconciled_terminal')
        self.assertEqual(self.snapshot(), before)
        audit = dict(command.db.execute('SELECT * FROM command_reconciliations').fetchone())
        self.assertEqual(audit['prior_state'], 'queued'); self.assertIsNone(audit['prior_run_id'])
        self.assertEqual(audit['terminal_event_fingerprint'], hashlib.sha256(json.dumps(proof['terminal_event'], sort_keys=True, separators=(',', ':')).encode()).hexdigest())
        self.assertNotIn(proof['review_note'], json.dumps(audit))
        repeated = command.reconcile_grok_terminal(agent, {**proof, 'validate_only': False})
        self.assertTrue(repeated['duplicate']); self.assertFalse(repeated['applied'])
        self.assertEqual(command.db.execute('SELECT count(*) FROM command_reconciliations').fetchone()[0], 1)
        with self.assertRaises(CommandError):
            command.reconcile_grok_terminal(agent, {**proof, 'review_note': 'A changed owner review note should conflict.'})

    def test_wrong_original_terminal_receipt_or_identity_proof_never_applies(self):
        agent, request, start, proof = self.legacy()
        before = self.snapshot()
        cases = [
            {**proof, 'original_text': proof['original_text'] + ' changed'},
            {**proof, 'terminal_event': {**proof['terminal_event'], 'text': 'Not the original final'}},
            {**proof, 'terminal_event': {**proof['terminal_event'], 'event_id': 'not-a-receipt'}},
            {**proof, 'terminal_event': {**proof['terminal_event'], 'type': 'message', 'role': 'assistant', 'phase': 'final'}},
            {**proof, 'terminal_event': {**proof['terminal_event'], 'source_id': 'other'}},
            {**proof, 'terminal_event': {**proof['terminal_event'], 'run_id': str(uuid.uuid4())}},
            {**proof, 'original_text': 'x' * 24001},
            {**proof, 'terminal_event': {**proof['terminal_event'], 'text': 'x' * 24001}},
            {**proof, 'validate_only': 'false'},
            {**proof, 'unexpected': 'field'},
        ]
        for candidate in cases:
            with self.subTest(candidate=candidate), self.assertRaises(CommandError):
                self.bridge.commands.reconcile_grok_terminal(agent, {**candidate, 'validate_only': candidate.get('validate_only', False)})
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.wait(request, {'queued'})['run_id'], None)

    def test_mismatching_current_live_run_and_nonqueued_commands_are_rejected(self):
        agent, request, start, proof = self.legacy()
        self.bridge.history.db.execute("UPDATE live_runs SET status='working' WHERE agent_id=?", (agent['id'],))
        self.bridge.history.db.commit()
        with self.assertRaises(CommandError):self.bridge.commands.reconcile_grok_terminal(agent, proof)
        self.bridge.history.db.execute("UPDATE live_runs SET status='done' WHERE agent_id=?", (agent['id'],))
        self.bridge.history.db.commit()
        for state in ('pending', 'sending', 'running', 'waiting_user', 'failed', 'cancelled'):
            self.bridge.commands.db.execute('UPDATE commands SET state=? WHERE id=?', (state, request['request_id']))
            self.bridge.commands.db.commit()
            with self.subTest(state=state), self.assertRaises(CommandError):self.bridge.commands.reconcile_grok_terminal(agent, proof)

    def test_audit_insert_failure_rolls_back_command_update(self):
        agent, request, start, proof = self.legacy()
        command = self.bridge.commands
        command.db.execute("CREATE TRIGGER fixture_fail_audit BEFORE INSERT ON command_reconciliations BEGIN SELECT RAISE(ABORT, 'fixture audit failure'); END")
        command.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            command.reconcile_grok_terminal(agent, {**proof, 'validate_only': False})
        row = self.wait(request, {'queued'})
        self.assertIsNone(row['run_id']); self.assertEqual(row['detail_code'], 'awaiting_agent_callback')
        self.assertEqual(command.db.execute('SELECT count(*) FROM command_reconciliations').fetchone()[0], 0)

    def test_http_reconciliation_is_owner_and_local_origin_only(self):
        agent, request, start, proof = self.legacy()
        server = ThreadingHTTPServer(('127.0.0.1', 0), proxy_handler(self.bridge))
        self.bridge.config['port'] = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def call(owner=True, origin=None, body=proof):
            headers = {'Content-Type': 'application/json'}
            if owner: headers['X-Office-Key'] = self.config['topic']
            if origin: headers['Origin'] = origin
            req = urllib.request.Request('http://127.0.0.1:%s/api/office/commands/reconcile' % server.server_address[1], data=json.dumps(body).encode(), headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=3) as response:return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                with error:return error.code, json.load(error)
        try:
            self.assertEqual(call(owner=False)[0], 403)
            self.assertEqual(call(origin='https://untrusted.invalid')[0], 403)
            code, result = call(); self.assertEqual(code, 200); self.assertTrue(result['validated']); self.assertFalse(result['applied'])
            self.assertEqual(self.wait(request, {'queued'})['state'], 'queued')
            code, result = call(body={**proof, 'validate_only': False}); self.assertEqual(code, 200); self.assertTrue(result['applied'])
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
