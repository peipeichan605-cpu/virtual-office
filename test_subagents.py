"""Native relationship fixtures; no model calls or real Office memberships."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from configuration import initialize, load_config, save_config
from dashboard import Bridge, proxy_handler
from history import timestamp
from subagents import MAX_CHILDREN

ROOT = '00000000-0000-4000-8000-000000000101'
ROOT_ID = 'codex:' + ROOT


def record(kind, at, **payload):
    return {'type': kind, 'timestamp': timestamp(at), 'payload': payload}


class ChildObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.directory = Path(self.tmp.name).resolve()
        self.start = time.time() - 10
        self.database = self.directory / 'source.sqlite'
        with sqlite3.connect(self.database) as db:
            db.execute('''CREATE TABLE threads (id TEXT PRIMARY KEY,rollout_path TEXT,source TEXT,created_at INTEGER,
                created_at_ms INTEGER,updated_at_ms INTEGER,agent_path TEXT,agent_nickname TEXT,cwd TEXT)''')
            db.execute('CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT,status TEXT)')
        self.root_path = self.directory / 'root.jsonl'
        self.root_path.write_text(json.dumps(record('session_meta', self.start, id=ROOT, agent_path='/root')) + '\n')
        self.append(self.root_path, record('event_msg', self.start, type='task_started', turn_id='parent-run', root_turn_id='parent-run', started_at=self.start))
        self.add_source(ROOT, self.root_path, self.start, None, '/root')
        self.config_path = self.directory / 'config.json'; initialize(self.config_path)
        config = load_config(self.config_path)
        config.update(codex_db=str(self.database), run_id='fixture', run_dir=str(self.directory / 'run'))
        config['agents'] = [{'id': ROOT_ID, 'provider': 'codex', 'source_id': ROOT, 'driver': 'codex', 'name': 'Selected parent'}]
        save_config(self.config_path, config)
        self.bridge = Bridge(load_config(self.config_path), dry_run=True)
        self.load_root()

    def tearDown(self):
        self.bridge.commands.close(); self.bridge.history.close(); self.tmp.cleanup()

    def load_root(self):
        self.bridge.load_paths()
        self.bridge.tails[ROOT].read()

    def append(self, path, *rows):
        with path.open('a') as output:
            for row in rows: output.write(json.dumps(row) + '\n')

    def add_source(self, sid, path, born, parent, agent_path, **changes):
        source = {'subagent': {'thread_spawn': {'parent_thread_id': parent}}} if parent else 'cli'
        source = json.dumps(source) if isinstance(source, dict) else source
        values = dict(id=sid, rollout_path=str(path), source=source, created_at=int(born), created_at_ms=int(born * 1000),
                      updated_at_ms=int(born * 1000), agent_path=agent_path, agent_nickname='Helper', cwd=str(self.directory))
        values.update(changes)
        with sqlite3.connect(self.database) as db:
            db.execute('INSERT OR REPLACE INTO threads (' + ','.join(values) + ') VALUES (' + ','.join('?' for _ in values) + ')', tuple(values.values()))
            if parent: db.execute('INSERT INTO thread_spawn_edges VALUES (?,?,?)', (parent, sid, 'open'))

    def child(self, number=1, parent=ROOT, path='/root/helper', parent_path='/root', born=None, parent_run='parent-run', metadata=None, finish=False):
        sid = '00000000-0000-4000-8000-%012d' % (200 + number)
        born = born if born is not None else self.start + number / 10 + 1
        file = self.directory / ('child-%d.jsonl' % number)
        meta = dict(id=sid, timestamp=timestamp(born), parent_thread_id=parent,
                    source={'subagent': {'thread_spawn': {'parent_thread_id': parent}}}, agent_path=path, subagent_history_start_ordinal=4)
        meta.update(metadata or {})
        # Inherited records deliberately use rewritten birth timestamps.
        rows = [record('session_meta', born, **meta),
                record('event_msg', born, type='task_started', turn_id='inherited-run', root_turn_id='inherited-run', started_at=self.start - 100),
                record('response_item', born, type='message', role='user', content=[{'type': 'input_text', 'text': 'INHERITED_PRIVATE_PROMPT'}]),
                record('event_msg', born, type='task_complete', turn_id='inherited-run', last_agent_message='INHERITED_PRIVATE_RESULT'),
                record('event_msg', born + .01, type='task_started', turn_id='child-run-%d' % number, root_turn_id=parent_run, started_at=int(born)),
                record('response_item', born + .02, type='agent_message', author=parent_path, recipient=path,
                       content=[{'type': 'input_text', 'text': 'Delegated task api_key=examplePrivateKey987654321'}, {'type': 'encrypted_content', 'encrypted_content': 'ENCRYPTED_PRIVATE'}]),
                record('response_item', born + .03, type='message', role='assistant', phase='analysis', content=[{'type': 'output_text', 'text': 'INTERNAL_REASONING_PRIVATE'}]),
                record('response_item', born + .04, type='function_call_output', output='TOOL_OUTPUT_PRIVATE'),
                record('response_item', born + .05, type='message', role='assistant', phase='commentary', content=[{'type': 'output_text', 'text': 'Child public progress'}])]
        file.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        self.add_source(sid, file, born, parent, path)
        if finish: self.finish(file, number, born + .06, parent_run)
        return sid, file, born

    def finish(self, file, number, at=None, root_run='parent-run'):
        self.append(file, record('event_msg', at or time.time(), type='task_complete', turn_id='child-run-%d' % number, root_turn_id=root_run, last_agent_message='Child delivered result'))

    def poll(self): self.bridge.children.poll(force=True)
    def children(self): return [r for r in self.bridge.work_snapshot()['agents'] if r.get('is_subagent')]

    def test_real_relationship_admits_child_without_membership_or_action_authority(self):
        self.child(); self.poll(); child = self.children()[0]
        self.assertEqual(child['parent_agent_id'], ROOT_ID)
        self.assertEqual(child['root_agent_id'], ROOT_ID)
        self.assertEqual(child['depth'], 1); self.assertEqual(child['state'], 'working')
        self.assertEqual(child['parent_run_id'], 'parent-run')
        self.assertEqual(child['workspace'], self.bridge.work_snapshot()['agents'][0]['workspace'])
        self.assertFalse(child['command_capability']['can_send'])
        self.assertIsNone(child['ground_desk_id'])
        self.assertNotIn(child['id'], self.bridge.agents)
        self.assertNotIn(child['id'], self.bridge.definitions)
        self.assertNotIn(child['id'], self.bridge.commands.bindings)
        self.assertEqual(len(load_config(self.config_path)['agents']), 1)

    def test_ordinal_boundary_excludes_rewritten_parent_context_and_private_fields(self):
        self.child(); self.poll(); child = self.children()[0]
        rows = self.bridge.history.messages(self.bridge.history_agents(), child['id'])
        text = json.dumps(rows, ensure_ascii=False)
        for forbidden in ('INHERITED_', 'ENCRYPTED_PRIVATE', 'INTERNAL_REASONING_PRIVATE', 'TOOL_OUTPUT_PRIVATE', 'examplePrivateKey'):
            self.assertNotIn(forbidden, text)
        self.assertIn('Child public progress', text)
        self.assertIn('Delegated task', text)
        self.assertIn('已遮蓋', text)

    def test_encrypted_header_only_task_is_reported_as_unavailable_without_decrypting(self):
        _,file,_=self.child()
        records=[json.loads(line) for line in file.read_text().splitlines()]
        records[5]['payload']['content']=[
            {'type':'input_text','text':'Message Type: NEW_TASK\nTask name: /root/helper\nSender: /root\nPayload:\n'},
            {'type':'encrypted_content','encrypted_content':'OPAQUE_TASK_DO_NOT_DECRYPT'}]
        file.write_text(''.join(json.dumps(row)+'\n' for row in records))
        self.poll();child=self.children()[0]
        messages=self.bridge.history.messages(self.bridge.history_agents(),child['id'])
        prompt=next(message['text'] for message in messages if message['phase']=='prompt')
        self.assertEqual(prompt,'原平台未提供可讀任務內容；請查看子 Agent 的公開進度與交付。')
        self.assertNotIn('OPAQUE_TASK',json.dumps(messages))
        run=self.bridge.history.list(self.bridge.history_agents(),agent=child['id'])[0]
        self.assertEqual(run['prompt_preview'],prompt)

    def test_historical_open_edges_and_unrelated_sessions_never_appear(self):
        self.child(born=self.start - 100)
        self.child(2, parent='unselected-root')
        self.poll(); self.assertEqual(self.children(), [])
        self.assertEqual(self.bridge.children.allowed(), {})

    def test_mismatched_parent_and_bad_boundary_cannot_block_valid_sibling(self):
        self.child(1, metadata={'parent_thread_id': 'wrong'})
        self.child(2, metadata={'source': {'subagent': {'thread_spawn': {'parent_thread_id': 'wrong'}}}})
        self.child(3, metadata={'subagent_history_start_ordinal': None})
        self.child(4)
        self.poll(); self.assertEqual(len(self.children()), 1)
        self.assertFalse(self.bridge.work_snapshot()['subagents_ok'])

    def test_wrong_session_identity_is_rejected(self):
        self.child(metadata={'id': 'other-session'}); self.poll()
        self.assertEqual(self.children(), [])

    def test_nested_children_follow_verified_chain(self):
        child, _, _ = self.child()
        self.child(2, parent=child, path='/root/helper/review', parent_path='/root/helper')
        self.poll(); rows = self.children()
        self.assertEqual(len(rows), 2)
        one = next(r for r in rows if r['depth'] == 1)
        two = next(r for r in rows if r['depth'] == 2)
        self.assertEqual(two['parent_agent_id'], one['id']); self.assertEqual(two['root_agent_id'], ROOT_ID)
        self.assertIn('Delegated task', json.dumps(self.bridge.history.messages(self.bridge.history_agents(), two['id'])))

    def test_source_json_fallback_without_edges(self):
        self.child()
        with sqlite3.connect(self.database) as db: db.execute('DROP TABLE thread_spawn_edges')
        self.poll(); self.assertEqual(len(self.children()), 1)

    def test_selected_root_can_itself_have_a_native_parent_path_and_context(self):
        self.bridge.tails[ROOT].agent_path='/root/enrolled'
        self.bridge.tails[ROOT].native_root_run='ancestor-native-run'
        self.child(parent_path='/root/enrolled',parent_run='ancestor-native-run',path='/root/enrolled/helper')
        self.poll(); child=self.children()[0]
        self.assertEqual(child['parent_run_id'],'parent-run')
        messages=self.bridge.history.messages(self.bridge.history_agents(),child['id'])
        self.assertTrue(any(m['phase']=='prompt' for m in messages))

    def test_completion_retains_history_and_duplicate_replay_does_not_duplicate_messages(self):
        _, file, _ = self.child(); self.poll(); ident = self.children()[0]['id']
        self.finish(file, 1); self.poll(); self.assertEqual(self.children()[0]['state'], 'done')
        before = self.bridge.history.messages(self.bridge.history_agents(), ident)
        self.poll(); self.assertEqual(self.bridge.history.messages(self.bridge.history_agents(), ident), before)
        with patch('subagents.time.time', return_value=time.time() + 40):
            self.assertEqual(self.children(), [])
        rows = self.bridge.history.list(self.bridge.history_agents(), terminal=True)
        done = next(r for r in self.bridge.children.decorate_runs(rows) if r['agent_id'] == ident)
        self.assertEqual(done['root_agent_id'], ROOT_ID)
        self.assertEqual(done['parent_run_id'], 'parent-run')
        self.assertEqual(self.bridge.history.detail(done['id'], self.bridge.history_agents())['delivery'], 'Child delivered result')

    def test_missing_rotation_and_stale_source_are_unknown_never_done(self):
        _, file, _ = self.child(); self.poll()
        with patch('subagents.time.time', return_value=time.time() + 1900):
            self.poll(); self.assertEqual(self.children()[0]['state'], 'waiting_user')
        contents = file.read_text(); file.unlink(); file.write_text(contents)
        self.poll(); self.assertEqual(self.children()[0]['state'], 'waiting_user')
        self.assertEqual(self.bridge.history.list(self.bridge.history_agents(), terminal=True), [])

    def test_parent_final_or_new_turn_does_not_finish_working_child(self):
        self.child(); self.poll()
        self.append(self.root_path, record('event_msg', time.time(), type='task_complete', turn_id='parent-run'))
        self.load_root(); self.poll(); self.assertEqual(self.children()[0]['state'], 'working')
        self.append(self.root_path, record('event_msg', time.time(), type='task_started', turn_id='next-parent-run', root_turn_id='next-parent-run', started_at=time.time()))
        self.load_root(); self.poll(); self.assertEqual(self.children()[0]['state'], 'working')
        self.assertEqual(self.children()[0]['parent_run_id'], 'parent-run')

    def test_restart_preserves_generation_history_and_does_not_resurrect_old_children(self):
        _, file, _ = self.child(); self.child(2, born=self.start - 100); self.poll(); ident = self.children()[0]['id']
        self.finish(file, 1); self.poll()
        old = self.bridge; self.bridge = Bridge(load_config(self.config_path), dry_run=True)
        old.commands.close(); old.history.close(); self.load_root(); self.poll()
        self.assertEqual(self.children()[0]['id'], ident); self.assertEqual(self.children()[0]['state'], 'done')
        messages = self.bridge.history.messages(self.bridge.history_agents(), ident)
        self.assertEqual(sum(m['text'] == 'Child delivered result' for m in messages), 1)
        self.assertEqual(len(self.bridge.children.allowed()), 1)

    def test_parent_logout_and_rebind_revoke_observation_without_deleting_history(self):
        self.child(); self.poll(); ident = self.children()[0]['id']
        original = load_config(self.config_path)
        empty = dict(original, agents=[], command_bindings={}); self.bridge.refresh_connections(empty)
        self.assertEqual(self.children(), []); self.assertNotIn(ident, self.bridge.history_agents())
        self.assertIsNotNone(self.bridge.history.db.execute('SELECT 1 FROM runs WHERE agent_id=?', (ident,)).fetchone())
        original['agents'][0]['native_observe_after'] = timestamp(time.time())
        self.bridge.refresh_connections(original); self.poll()
        self.assertNotIn(ident, self.bridge.history_agents()); self.assertEqual(self.children(), [])

    def test_independently_enrolled_child_session_is_not_duplicated(self):
        sid, _, _ = self.child()
        config = load_config(self.config_path)
        config['agents'].append({'id': 'codex:' + sid, 'provider': 'codex', 'source_id': sid, 'driver': 'codex', 'name': 'Selected child'})
        self.bridge.refresh_connections(config); self.poll()
        self.assertEqual(self.children(), []); self.assertEqual(len(self.bridge.agents), 2)

    def test_terminal_history_does_not_exhaust_lifetime_admission_cap(self):
        self.child(finish=True); self.poll()
        row = self.bridge.children.rows()[0]
        with self.bridge.history.lock, self.bridge.history.db:
            for number in range(MAX_CHILDREN + 2):
                old = dict(row, id='child:codex:old-%d' % number, native_id='old-%d' % number, terminal_at=self.start - 100)
                self.bridge.history.db.execute('INSERT INTO observed_agents (' + ','.join(old) + ') VALUES (' + ','.join('?' for _ in old) + ')', tuple(old.values()))
        self.child(2); self.poll()
        self.assertTrue(any(r['state'] == 'working' for r in self.children()))

    def test_terminal_revision_is_consumed_once_and_reused_child_resumes(self):
        _,file,born=self.child(finish=True); self.poll(); ident=self.children()[0]['id']
        with patch('subagents.time.time',return_value=time.time()+40):
            self.poll(); self.assertNotIn(ident,self.bridge.children.tails)
            self.poll(); self.assertNotIn(ident,self.bridge.children.watch_ids)
        now=time.time()
        self.append(self.root_path,record('event_msg',now,type='task_started',turn_id='second-parent',root_turn_id='second-parent',started_at=now))
        self.load_root()
        self.append(file,record('event_msg',now+.01,type='task_started',turn_id='second-child',root_turn_id='second-parent',started_at=now),
                    record('response_item',now+.02,type='message',role='assistant',phase='commentary',content=[{'type':'output_text','text':'Resumed child'}]))
        self.poll(); child=self.children()[0]
        self.assertEqual(child['id'],ident); self.assertEqual(child['state'],'working'); self.assertEqual(child['parent_run_id'],'second-parent')
        self.assertEqual(child['latest_message']['text'],'Resumed child')

    def test_replayed_start_cannot_revive_a_completed_child_run(self):
        _,file,born=self.child(finish=True); self.poll()
        self.append(file,record('event_msg',time.time(),type='task_started',turn_id='child-run-1',root_turn_id='parent-run',started_at=int(born)))
        self.poll(); self.assertEqual(self.children()[0]['state'],'done')

    def test_large_inherited_history_is_processed_with_bounded_reads(self):
        _,file,_=self.child()
        rows=file.read_text().splitlines()
        rows[2]=json.dumps(record('response_item',time.time(),type='message',role='user',content=[{'type':'input_text','text':'X'*(3*1024*1024)}]))
        file.write_text('\n'.join(rows)+'\n')
        self.poll(); self.assertEqual(self.children()[0]['state'],'waiting_user')
        self.poll(); self.assertEqual(self.children()[0]['state'],'working')
        text=json.dumps(self.bridge.history.messages(self.bridge.history_agents(),self.children()[0]['id']))
        self.assertNotIn('XXXXX',text)

    def test_private_history_routes_work_but_child_never_gets_native_mutation_authority(self):
        self.child(finish=True);self.poll();ident=self.children()[0]['id']
        server=ThreadingHTTPServer(('127.0.0.1',0),proxy_handler(self.bridge))
        self.bridge.config['port']=server.server_address[1]
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(self.bridge.config['port'])
        def request(route,payload=None,key=True):
            headers={'Content-Type':'application/json'}
            if key:headers['X-Office-Key']=self.bridge.config['topic']
            req=urllib.request.Request(base+route,data=json.dumps(payload).encode() if payload is not None else None,headers=headers)
            try:
                with urllib.request.urlopen(req,timeout=3) as response:return response.status,json.load(response)
            except urllib.error.HTTPError as error:return error.code,json.load(error)
        try:
            query='?agent='+urllib.parse.quote(ident)
            self.assertEqual(request('/api/office/messages'+query,key=False)[0],403)
            self.assertEqual(request('/api/office/messages'+query)[0],200)
            code,history=request('/api/office/history'+query)
            self.assertEqual(code,200);self.assertEqual(history['runs'][0]['root_agent_id'],ROOT_ID)
            run=history['runs'][0]['id']
            self.assertEqual(request('/api/office/run?id='+run)[0],200)
            for route,payload in [('/api/office/commands',{'agent_id':ident,'request_id':'fake','text':'Do not run'}),
                                  ('/api/office/seating',{'agent_id':ident,'desk_id':'gf-main-020'}),
                                  ('/api/office/agents/logout',{'agent_id':ident}),
                                  ('/api/office/workspaces/assign',{'agent_id':ident,'mode':'auto'})]:
                self.assertEqual(request(route,payload)[0],404,route)
            empty=dict(load_config(self.config_path),agents=[],command_bindings={})
            self.bridge.refresh_connections(empty)
            self.assertEqual(request('/api/office/messages'+query)[0],404)
            self.assertEqual(request('/api/office/history'+query)[0],404)
            self.assertEqual(request('/api/office/run?id='+run)[0],404)
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__ == '__main__': unittest.main()
