import copy
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).parent / 'adapters'))
from cursor_children import (CursorMapper, HookError, Journal, handle,
                             hook_id, HOOK_ID_PREFIX, native_id,
                             load_observer, observer_paths, post, process_observer)


class CursorChildrenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.path = self.directory / 'observer.json'
        self.observer = {'endpoint': 'http://127.0.0.1:4318/api/agents/children/events',
            'root_event_endpoint': 'http://127.0.0.1:4318/api/agents/observers/root-events',
            'observer_id': 'observer-one', 'agent_id': 'cursor:selected', 'provider': 'cursor',
            'native_session_id': 'parent-conversation', 'generation': 'membership-one',
            'token': 'fixture-observer-token-771fac', 'source_kind': 'cursor_hook', 'evidence': 'native_hook'}
        self.path.write_text(json.dumps(self.observer)); self.path.chmod(0o600)
        self.state = self.directory / 'state'
        self.sent = []

    def tearDown(self):
        self.temp.cleanup()

    def transport(self, observer, item, timeout=.6):
        self.sent.append(copy.deepcopy(item))
        return 'delivered', None

    def emit(self, kind, **values):
        data = {'hook_event_name': kind, 'conversation_id': 'parent-conversation',
                'generation_id': 'parent-generation', **values}
        handle(self.observer, self.state, data, kind, self.transport)

    def begin(self):
        self.emit('beforeSubmitPrompt', prompt='Bounded public task')

    def child(self, sid='child-native', call='task-call', **values):
        self.emit('subagentStart', parent_conversation_id='parent-conversation',
                  subagent_id=sid, tool_call_id=call, subagent_type='explore',
                  task='Read the public README', **values)

    def alias(self, conversation='child-conversation', generation='child-generation', call='task-call'):
        self.emit('preToolUse', conversation_id=conversation, generation_id=generation,
                  parent_tool_call_id=call, tool_use_id='read-tool',
                  tool_input={'secret': 'PRIVATE_TOOL_INPUT'}, tool_output='PRIVATE_TOOL_RESULT',
                  thought='PRIVATE_THINKING')

    def stored(self):
        return json.loads((self.state / 'state.json').read_text())

    def test_real_root_start_precedes_native_child_start_and_alias_proof(self):
        self.begin(); self.child(); self.alias()
        self.assertEqual([r['route'] for r in self.sent], ['root', 'child', 'child'])
        root, child, proof = [r['body'] for r in self.sent]
        self.assertEqual(root['type'], 'turn_started')
        self.assertEqual(root['run_id'], 'parent-generation')
        self.assertEqual(child['type'], 'started')
        self.assertEqual(child['native_child_id'], 'child-native')
        self.assertEqual(child['native_alias'], {'kind': 'tool_call', 'id': 'task-call'})
        self.assertEqual(proof['native_alias'], {'kind': 'conversation', 'id': 'child-conversation', 'parent_tool_call_id': 'task-call'})
        self.assertEqual(proof['text'], '')

    def test_native_lf_task_id_start_alias_public_stop_preserves_exact_identity(self):
        raw = 'A' * 42 + '\n' + 'B' * 43  # Actual native failure shape: 86 chars with LF.
        wire = hook_id(raw)
        self.begin(); self.child(raw, raw); self.alias(call=raw)
        self.emit('afterAgentResponse', conversation_id='child-conversation', generation_id='child-generation', text='Native public progress')
        self.emit('subagentStop', subagent_id=raw, tool_call_id=raw, child_conversation_id='child-conversation',
                  status='completed', summary='Native public delivery')
        events = [item['body'] for item in self.sent if item['route'] == 'child']
        self.assertEqual([event['type'] for event in events], ['started', 'message', 'message', 'completed'])
        self.assertTrue(all(event['native_child_id'] == wire for event in events))
        self.assertEqual(events[0]['native_alias']['id'], wire)
        self.assertEqual(events[1]['native_alias']['parent_tool_call_id'], wire)
        decoded = base64.urlsafe_b64decode(wire[len(HOOK_ID_PREFIX):] + '=' * (-len(wire[len(HOOK_ID_PREFIX):]) % 4)).decode('utf-8')
        self.assertEqual(decoded, raw)
        self.assertNotEqual(wire, hook_id(raw.replace('\n', '')))
        self.assertNotIn(raw, self.stored()['children'])
        self.assertFalse(self.stored()['roots']['parent-generation']['terminal'])

    def test_parallel_lf_task_ids_never_alias_each_other(self):
        one, two = 'Task\nOne', 'Task\nTwo'
        self.begin(); self.child(one, one); self.child(two, two)
        self.alias('conversation-two', 'generation-two', two)
        self.alias('conversation-one', 'generation-one', one)
        self.emit('afterAgentResponse', conversation_id='conversation-two', generation_id='generation-two', text='Second child public')
        self.assertEqual(self.sent[-1]['body']['native_child_id'], hook_id(two))
        self.emit('subagentStop', subagent_id=one, tool_call_id=one, status='completed', summary='First delivery')
        self.assertEqual(self.sent[-1]['body']['native_child_id'], hook_id(one))
        self.assertNotEqual(hook_id(one), hook_id(two))
        state = self.stored()
        self.assertFalse(state['invocations'][state['current'][hook_id(two)]]['terminal'])

    def test_native_opaque_codec_reserved_prefix_is_escaped_without_collision(self):
        original = 'Task\nOpaque'
        encoded = hook_id(original)
        self.assertTrue(encoded.startswith(HOOK_ID_PREFIX))
        self.assertNotEqual(hook_id(encoded), encoded)
        self.assertEqual(hook_id('legacy-task:/@-._123'), 'legacy-task:/@-._123')
        self.assertEqual(hook_id('legacy-' + 'a' * 193), 'legacy-' + 'a' * 193)
        self.assertNotEqual(hook_id('a\nb'), hook_id('ab'))
        self.assertNotEqual(hook_id('e\u0301'), hook_id('\u00e9'))

    def test_native_opaque_codec_rejects_invalid_and_overlong_ids_but_credentials_remain_strict(self):
        from child_events import identity
        maximum = '\n' + 'x' * 171
        self.assertEqual(len(hook_id(maximum)), 240)
        self.assertEqual(identity(hook_id(maximum)), hook_id(maximum))
        for value in ('', '\0', 'Task\0x', '\ud800', '\n' + 'x' * 172, '\u00e9' * 87, HOOK_ID_PREFIX + 'x' * 163, 'x' * 201):
            self.assertIsNone(hook_id(value))
        self.assertIsNone(native_id('Task\nNative'))
        for field in ('observer_id', 'agent_id', 'native_session_id', 'generation'):
            self.path.write_text(json.dumps(dict(self.observer, **{field: 'Task\nNative'})))
            with self.assertRaises(HookError): load_observer(self.path)

    def test_opaque_child_routing_cache_normalizes_native_call_and_conversation(self):
        raw, conversation = 'Native\nTask', 'Child\nConversation'
        self.begin(); self.child(raw, raw)
        data = {'hook_event_name': 'preToolUse', 'conversation_id': conversation,
                'generation_id': 'child-generation', 'parent_tool_call_id': raw}
        self.assertTrue(process_observer(self.path, data, 'preToolUse', self.state, self.transport))
        self.assertEqual(self.sent[-1]['body']['native_alias']['id'], hook_id(conversation))
        public = dict(data, hook_event_name='afterAgentResponse', text='Scoped opaque child text')
        public.pop('parent_tool_call_id')
        self.assertTrue(process_observer(self.path, public, 'afterAgentResponse', self.state, self.transport))
        self.assertEqual(self.sent[-1]['body']['native_child_id'], hook_id(raw))

    def test_child_start_without_observed_root_start_is_not_synthesized(self):
        self.child()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.stored()['invocations'], {})

    def test_public_child_text_and_stop_summary_do_not_finish_root(self):
        self.begin(); self.child(); self.alias()
        self.emit('afterAgentResponse', conversation_id='child-conversation', generation_id='child-generation', text='Public progress')
        self.emit('subagentStop', child_conversation_id='child-conversation', status='completed', summary='Public delivery')
        self.assertEqual(self.sent[-1]['route'], 'child')
        self.assertEqual(self.sent[-1]['body']['type'], 'completed')
        self.assertEqual(self.sent[-1]['body']['text'], 'Public delivery')
        self.assertFalse(self.stored()['roots']['parent-generation']['terminal'])
        self.assertFalse(any(x['route'] == 'root' and x['body']['type'] == 'turn_completed' for x in self.sent))
        self.emit('stop', conversation_id='child-conversation', generation_id='child-generation', status='completed')
        self.assertFalse(self.stored()['roots']['parent-generation']['terminal'])
        self.emit('afterAgentResponse', text='Parent delivery')
        self.emit('stop', status='completed')
        self.assertEqual(self.sent[-1]['route'], 'root')
        self.assertEqual(self.sent[-1]['body']['text'], 'Parent delivery')

    def test_unknown_alias_parallel_same_task_is_never_guessed(self):
        self.begin(); self.child(); self.child('second-native', 'second-task')
        before = len(self.sent)
        self.emit('subagentStop', child_conversation_id='unobserved-child', status='completed',
                  task='Read the public README', subagent_type='explore', summary='PRIVATE_UNKNOWN_SUMMARY')
        self.emit('afterAgentResponse', conversation_id='unobserved-child', generation_id='unknown-generation', text='PRIVATE_UNKNOWN_TEXT')
        self.assertEqual(len(self.sent), before)
        self.assertNotIn('PRIVATE_UNKNOWN', (self.state / 'state.json').read_text())
        self.alias('second-conversation', 'second-generation', 'second-task')
        self.emit('afterAgentResponse', conversation_id='second-conversation', generation_id='second-generation', text='Correct second child')
        self.assertEqual(self.sent[-1]['body']['native_child_id'], 'second-native')

    def test_root_stop_before_public_response_maps_late_text_as_final_enrichment(self):
        self.begin()
        self.emit('stop', status='completed')
        terminal = copy.deepcopy(self.sent[-1]['body'])
        self.assertEqual(terminal['type'], 'turn_completed')
        self.assertEqual(terminal['text'], '')
        self.emit('afterAgentResponse', text='Delayed root public delivery')
        final = self.sent[-1]['body']
        self.assertEqual(final['type'], 'message')
        self.assertEqual(final['phase'], 'final')
        self.assertEqual(final['text'], 'Delayed root public delivery')
        self.assertEqual(final['run_id'], terminal['run_id'])
        self.assertTrue(self.stored()['roots']['parent-generation']['terminal'])
        count = len(self.sent)
        self.emit('afterAgentResponse', text='Delayed root public delivery')
        self.assertEqual(len(self.sent), count)

    def test_repeated_root_public_text_after_stop_does_not_conflict_with_commentary_id(self):
        self.begin()
        self.emit('afterAgentResponse', text='Root delivery already observed')
        self.assertEqual(self.sent[-1]['body']['phase'], 'commentary')
        self.emit('stop', status='completed')
        count = len(self.sent)
        self.emit('afterAgentResponse', text='Root delivery already observed')
        self.assertEqual(len(self.sent), count)
        self.assertEqual(self.sent[-1]['body']['type'], 'turn_completed')
        self.assertEqual(self.stored()['last_error'], None)

    def test_quiet_stop_uses_explicit_native_sid_without_guessing_alias(self):
        self.begin(); self.child()
        self.emit('subagentStop', subagent_id='child-native', child_conversation_id='quiet-conversation',
                  status='completed', summary='Quiet native delivery')
        stopped = self.sent[-1]['body']
        self.assertEqual(stopped['type'], 'completed')
        self.assertEqual(stopped['native_child_id'], 'child-native')
        self.assertEqual(stopped['native_alias'], {'kind': 'conversation', 'id': 'quiet-conversation',
                                                 'parent_tool_call_id': 'task-call'})
        self.assertFalse(self.stored()['roots']['parent-generation']['terminal'])

    def test_stop_known_task_id_is_fenced_by_parent_generation(self):
        self.begin(); self.child()
        before = len(self.sent)
        self.emit('subagentStop', tool_call_id='task-call', generation_id='unrelated-generation',
                  status='completed', summary='PRIVATE_WRONG_GENERATION')
        self.assertEqual(len(self.sent), before)
        self.emit('subagentStop', tool_call_id='task-call', status='completed', summary='Task metadata delivery')
        self.assertEqual(self.sent[-1]['body']['type'], 'completed')
        self.assertNotIn('PRIVATE_WRONG_GENERATION', (self.state / 'state.json').read_text())

    def test_resumed_same_sid_same_parent_generation_requires_task_discriminator(self):
        self.begin(); self.child()
        self.emit('subagentStop', subagent_id='child-native', status='completed', summary='First result')
        self.child(call='resume-task')
        before = len(self.sent)
        self.emit('subagentStop', subagent_id='child-native', status='completed', summary='PRIVATE_AMBIGUOUS')
        self.assertEqual(len(self.sent), before)
        self.emit('subagentStop', subagent_id='child-native', tool_call_id='resume-task', status='completed', summary='Second result')
        self.assertEqual(self.sent[-1]['body']['text'], 'Second result')

    def test_nested_start_sends_parent_alias_first_and_nested_stop_finishes_child_only(self):
        self.begin(); self.child()
        self.emit('subagentStart', conversation_id='child-conversation', generation_id='child-generation',
                  parent_conversation_id='child-conversation', parent_tool_call_id='task-call',
                  subagent_id='grandchild-native', tool_call_id='grandchild-task', task='Read docs')
        alias, nested = [item['body'] for item in self.sent[-2:]]
        self.assertEqual(alias['native_child_id'], 'child-native')
        self.assertEqual(alias['native_alias']['id'], 'child-conversation')
        self.assertEqual(nested['native_parent_id'], 'child-native')
        self.assertEqual(nested['type'], 'started')
        self.emit('subagentStop', conversation_id='child-conversation', generation_id='child-generation',
                  parent_tool_call_id='task-call', subagent_id='grandchild-native',
                  child_conversation_id='grandchild-conversation', status='completed', summary='Grandchild delivery')
        self.assertEqual(self.sent[-1]['body']['native_child_id'], 'grandchild-native')
        state = self.stored()
        self.assertFalse(state['invocations'][state['current']['child-native']]['terminal'])
        self.assertFalse(state['roots']['parent-generation']['terminal'])

    def test_terminal_parent_cannot_spawn_new_descendant(self):
        self.begin(); self.child(); self.alias()
        self.emit('subagentStop', child_conversation_id='child-conversation', status='completed', summary='Done')
        before = len(self.sent)
        self.emit('subagentStart', conversation_id='child-conversation', generation_id='child-generation',
                  parent_conversation_id='child-conversation', parent_tool_call_id='task-call',
                  subagent_id='late-grandchild', tool_call_id='late-task', task='PRIVATE_LATE_TASK')
        self.assertEqual(len(self.sent), before)

    def test_quiet_nested_stop_never_attaches_parent_conversation_alias(self):
        self.begin(); self.child()
        self.emit('subagentStart', conversation_id='child-conversation', generation_id='child-generation',
                  parent_conversation_id='child-conversation', parent_tool_call_id='task-call',
                  subagent_id='grandchild-native', tool_call_id='grandchild-task', task='Read docs')
        self.emit('subagentStop', conversation_id='child-conversation', generation_id='child-generation',
                  parent_tool_call_id='task-call', subagent_id='grandchild-native', status='completed', summary='Quiet result')
        body = self.sent[-1]['body']
        self.assertEqual(body['native_child_id'], 'grandchild-native')
        self.assertEqual(body['type'], 'completed')
        self.assertNotIn('native_alias', body)

    def test_full_outbox_recovers_without_losing_current_callback(self):
        mapper = CursorMapper(self.observer)
        journal = Journal(self.state, self.observer)
        with journal.transaction() as state:
            mapper.translate(state, {'hook_event_name': 'beforeSubmitPrompt',
                'conversation_id': 'parent-conversation', 'generation_id': 'parent-generation',
                'prompt': 'Public task'}, 'beforeSubmitPrompt')
            for index in range(127):
                mapper.translate(state, {'hook_event_name': 'afterAgentResponse',
                    'conversation_id': 'parent-conversation', 'generation_id': 'parent-generation',
                    'text': 'Public callback ' + str(index)}, 'afterAgentResponse')
        original = copy.deepcopy(self.stored()['outbox'][0])
        self.emit('afterAgentResponse', text='Incoming callback during recovery')
        self.assertEqual(self.sent[0], original)
        self.assertTrue(any(item['body'].get('text') == 'Incoming callback during recovery'
                            for item in self.stored()['outbox']))
        self.assertEqual(len(self.stored()['outbox']), 113)

    def test_full_outbox_during_continued_outage_records_explicit_gap_metadata(self):
        self.test_full_outbox_recovers_without_losing_current_callback()
        self.sent.clear()
        def unavailable(observer, item, timeout=.6):
            return 'pending', 'office_unavailable'
        mapper = CursorMapper(self.observer)
        with Journal(self.state, self.observer).transaction() as state:
            for index in range(15):
                mapper.translate(state, {'hook_event_name': 'afterAgentResponse',
                    'conversation_id': 'parent-conversation', 'generation_id': 'parent-generation',
                    'text': 'More public callback ' + str(index)}, 'afterAgentResponse')
        data = {'hook_event_name': 'afterAgentResponse', 'conversation_id': 'parent-conversation',
                'generation_id': 'parent-generation', 'text': 'Callback over explicit offline limit'}
        handle(self.observer, self.state, data, 'afterAgentResponse', unavailable)
        state = self.stored()
        self.assertEqual(state['dropped_callbacks'], 1)
        self.assertEqual(state['last_error'], 'observer_journal_full')
        self.assertEqual(len(state['outbox']), 128)

    def test_more_than_256_delivered_invocations_do_not_disable_observer_or_revive_evicted_start(self):
        self.begin()
        for index in range(280):
            sid, call = 'child-' + str(index), 'task-' + str(index)
            self.child(sid, call)
            self.emit('subagentStop', subagent_id=sid, tool_call_id=call, status='completed', summary='Done')
        state = self.stored()
        self.assertLessEqual(len(state['invocations']), 128)
        self.assertNotIn('child-0', state['children'])
        count = len(self.sent)
        self.child('child-0', 'task-0')
        self.assertEqual(len(self.sent), count)
        self.assertNotIn('child-0', self.stored()['current'])
        self.child('new-child', 'new-task')
        self.assertEqual(self.sent[-1]['body']['native_child_id'], 'new-child')

    def test_native_root_workspace_is_single_absolute_root_only(self):
        self.emit('beforeSubmitPrompt', workspace_roots=['/tmp/public-project'])
        self.assertEqual(self.sent[-1]['body']['workspace_path'], '/tmp/public-project')
        self.emit('beforeSubmitPrompt', generation_id='multiroot', workspace_roots=['/tmp/a', '/tmp/b'])
        self.assertNotIn('workspace_path', self.sent[-1]['body'])

    def test_unrelated_existing_observer_routes_without_reading_full_text_journal(self):
        self.begin(); self.child(); self.alias()
        (self.state / 'state.json').write_text('not a readable JSON journal')
        routing = self.state / 'routing.json'
        value = json.loads(routing.read_text())
        value['state_mtime_ns'] = (self.state / 'state.json').stat().st_mtime_ns
        routing.write_text(json.dumps(value))
        data = {'hook_event_name': 'afterAgentResponse', 'conversation_id': 'unselected',
                'generation_id': 'unselected-generation', 'text': 'PRIVATE_OTHER_SESSION'}
        self.assertFalse(process_observer(self.path, data, 'afterAgentResponse', self.state, self.transport))
        self.assertNotIn('PRIVATE_OTHER_SESSION', routing.read_text())

    def test_active_index_prevents_retired_credential_starvation_and_traversal(self):
        for index in range(135):
            (self.directory / ('retired-' + str(index) + '.json')).write_text('{}')
        name = 'a' * 32 + '.json'
        current = self.directory / name
        current.write_text(json.dumps(self.observer)); current.chmod(0o600)
        index = self.directory / '.cursor-active.json'
        index.write_text(json.dumps({'version': 1, 'credentials': [name]})); index.chmod(0o600)
        self.assertEqual(observer_paths(self.directory), [current])
        for names in [['../outside.json'], [name, name], [name] * 129]:
            index.write_text(json.dumps({'version': 1, 'credentials': names}))
            with self.assertRaises(HookError): observer_paths(self.directory)

    def test_without_active_index_newest_credentials_are_considered_first(self):
        for index in range(135):
            path = self.directory / ('retired-' + str(index) + '.json')
            path.write_text('{}'); os.utime(path, (100, 100))
        newest = self.directory / ('b' * 32 + '.json')
        newest.write_text(json.dumps(self.observer))
        self.assertEqual(observer_paths(self.directory)[0], newest)
        self.assertEqual(len(observer_paths(self.directory)), 128)

    def test_duplicate_hooks_keep_single_events_with_original_timestamps(self):
        self.begin(); self.child(); self.alias()
        first = copy.deepcopy(self.sent)
        self.begin(); self.child(); self.alias()
        self.assertEqual(self.sent, first)
        self.assertEqual(self.stored()['outbox'], [])

    def test_network_failure_replays_exact_local_event_before_child(self):
        attempts = []
        def unavailable(observer, item, timeout=.6):
            attempts.append(copy.deepcopy(item)); return 'pending', 'office_unavailable'
        data = {'hook_event_name': 'beforeSubmitPrompt', 'conversation_id': 'parent-conversation',
                'generation_id': 'parent-generation', 'prompt': 'Public task'}
        handle(self.observer, self.state, data, 'beforeSubmitPrompt', unavailable)
        self.assertEqual(len(self.stored()['outbox']), 1)
        first = copy.deepcopy(attempts[0])
        self.child()
        self.assertEqual(self.sent[0], first)
        self.assertEqual(self.sent[1]['body']['type'], 'started')

    def test_reused_child_has_stable_native_identity_and_new_run_fence(self):
        self.begin(); self.child(); self.alias()
        first_run = self.sent[1]['body']['child_run_id']
        self.emit('subagentStop', child_conversation_id='child-conversation', status='completed', summary='First result')
        self.emit('beforeSubmitPrompt', generation_id='parent-two', prompt='Second parent task')
        self.child(call='second-call', generation_id='parent-two')
        second_run = self.sent[-1]['body']['child_run_id']
        self.alias(generation='child-two', call='second-call')
        self.emit('afterAgentResponse', conversation_id='child-conversation', generation_id='child-generation', text='Late old progress')
        self.assertNotEqual(first_run, second_run)
        self.assertEqual(self.sent[-1]['body']['child_run_id'], first_run)
        self.assertEqual(self.sent[-1]['body']['parent_run_id'], 'parent-generation')

    def test_native_conversation_alias_cannot_move_to_other_child(self):
        self.begin(); self.child(); self.alias()
        self.child('second-native', 'second-task')
        with self.assertRaises(HookError):
            self.alias(call='second-task')
        self.assertEqual(self.stored()['conversations']['child-conversation'], 'child-native')

    def test_unknown_root_no_network_and_no_text_journal(self):
        data = {'hook_event_name': 'beforeSubmitPrompt', 'conversation_id': 'unselected',
                'generation_id': 'other', 'prompt': 'PRIVATE_OTHER_CONVERSATION'}
        process_observer(self.path, data, 'beforeSubmitPrompt', self.state, self.transport)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.sent, [])

    def test_no_thoughts_tool_bodies_or_observer_token_are_persisted(self):
        self.begin(); self.child(); self.alias()
        self.emit('afterAgentResponse', conversation_id='child-conversation', generation_id='child-generation',
                  text='Public api_key=PRIVATE_SECRET_VALUE ' + self.observer['token'])
        text = (self.state / 'state.json').read_text() + json.dumps(self.sent)
        for value in ('PRIVATE_TOOL_INPUT', 'PRIVATE_TOOL_RESULT', 'PRIVATE_THINKING', 'PRIVATE_SECRET_VALUE', self.observer['token']):
            self.assertNotIn(value, text)
        self.assertEqual((self.state / 'state.json').stat().st_mode & 0o777, 0o600)

    def test_revoked_observer_blocks_further_collection_and_requests(self):
        def revoked(observer, item, timeout=.6):
            self.sent.append(copy.deepcopy(item)); return 'blocked', 'observer_not_active'
        data = {'hook_event_name': 'beforeSubmitPrompt', 'conversation_id': 'parent-conversation',
                'generation_id': 'parent-generation', 'prompt': 'Public task'}
        handle(self.observer, self.state, data, 'beforeSubmitPrompt', revoked)
        self.child()
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn('Read the public README', (self.state / 'state.json').read_text())

    def test_all_hook_failures_still_return_valid_nonblocking_stdout(self):
        script = Path(__file__).parent / 'adapters/cursor_children.py'
        for event, expected in [('subagentStart', {'permission': 'allow'}), ('preToolUse', {'permission': 'allow'}),
                                ('beforeSubmitPrompt', {'continue': True}), ('afterAgentResponse', {})]:
            result = subprocess.run([sys.executable, str(script), '--observer', str(self.directory / 'absent.json'), '--event', event],
                input='{not JSON PRIVATE_SENTINEL', text=True, capture_output=True, timeout=3)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout), expected)
            self.assertNotIn('PRIVATE_SENTINEL', result.stdout + result.stderr)

    def test_observer_endpoint_and_private_file_validation(self):
        self.assertEqual(load_observer(self.path)['native_session_id'], 'parent-conversation')
        for url in ('https://127.0.0.1:4318/api/agents/children/events',
                    'http://example.com:4318/api/agents/children/events',
                    'http://token@127.0.0.1:4318/api/agents/children/events',
                    'http://127.0.0.1:4318/api/agents/children/events?token=private'):
            self.path.write_text(json.dumps(dict(self.observer, endpoint=url)))
            with self.assertRaises(HookError): load_observer(self.path)
        self.path.write_text(json.dumps(self.observer)); self.path.chmod(0o644)
        with self.assertRaises(HookError): load_observer(self.path)


class CursorChildrenHTTPTests(unittest.TestCase):
    setUp = CursorChildrenTests.setUp
    tearDown = CursorChildrenTests.tearDown
    def server(self, mode='ok', bind=False):
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.requests.append({'path': self.path, 'body': body})
                if bind and self.path == '/api/agents/observers/bind':
                    current = dict(outer.observer, native_session_id=body['native_session_id'],
                                   generation='bound-generation', token='rotated-fixture-observer-token')
                    outer.path.write_text(json.dumps(current)); outer.path.chmod(0o600)
                    response = {'pending': False}; status = 200
                elif mode == 'redirect':
                    response = {}; status = 302
                elif mode == 'error':
                    response = {'error': 'PRIVATE_HTTP_BODY_TOKEN'}; status = 503
                else:
                    response = {'accepted': True}; status = 200
                raw = json.dumps(response).encode()
                self.send_response(status)
                if status == 302: self.send_header('Location', '/would-leak')
                self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
        self.requests = []
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        base = 'http://127.0.0.1:' + str(server.server_address[1])
        self.observer.update(endpoint=base + '/api/agents/children/events',
                             root_event_endpoint=base + '/api/agents/observers/root-events')
        self.path.write_text(json.dumps(self.observer)); self.path.chmod(0o600)

    def test_actual_loopback_http_errors_and_redirects_never_leak_details(self):
        self.server(mode='redirect')
        result = post(self.observer, {'route': 'child', 'body': {'event_id': 'fixture'}})
        self.assertEqual(result, ('pending', 'office_unavailable'))
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn('would-leak', json.dumps(self.requests))

    def test_actual_http_503_leaves_local_event_for_replay_without_response_text(self):
        self.server(mode='error')
        result = post(self.observer, {'route': 'child', 'body': {'event_id': 'fixture'}})
        self.assertEqual(result, ('pending', 'office_unavailable'))
        self.assertNotIn('PRIVATE_HTTP_BODY_TOKEN', repr(result))

    def test_cli_hook_budget_with_128_pending_observers_and_unavailable_bind(self):
        self.server(mode='error')
        names = []
        for index in range(128):
            name = format(index, '032x') + '.json'; names.append(name)
            value = dict(self.observer, observer_id='observer-' + str(index), native_session_id='pending:' + str(index),
                         activation_marker='OFFICE_CURSOR_BIND_' + str(index))
            path = self.directory / name
            path.write_text(json.dumps(value)); path.chmod(0o600)
        index = self.directory / '.cursor-active.json'
        index.write_text(json.dumps({'version': 1, 'credentials': names})); index.chmod(0o600)
        script = Path(__file__).parent / 'adapters/cursor_children.py'
        data = {'hook_event_name': 'afterAgentResponse', 'conversation_id': 'actual-new-native',
                'generation_id': 'onboarding', 'text': 'OFFICE_CURSOR_BIND_127'}
        start = time.monotonic()
        result = subprocess.run([sys.executable, str(script), '--observer-dir', str(self.directory),
                                 '--event', 'afterAgentResponse'], input=json.dumps(data),
                                text=True, capture_output=True, timeout=3)
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {})
        self.assertEqual(len(self.requests), 1)

    def test_pending_marker_binds_file_then_waits_for_next_real_prompt(self):
        self.observer.update(native_session_id='pending:one', activation_marker='OFFICE_CURSOR_BIND_fixture')
        self.server(bind=True)
        data = {'hook_event_name': 'afterAgentResponse', 'conversation_id': 'new-native-parent',
                'generation_id': 'onboarding', 'text': 'OFFICE_CURSOR_BIND_fixture'}
        self.assertTrue(process_observer(self.path, data, 'afterAgentResponse'))
        current = load_observer(self.path)
        self.assertEqual(current['native_session_id'], 'new-native-parent')
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]['path'], '/api/agents/observers/bind')
        state = self.path.parent / (current['observer_id'] + '-cursor-state')
        self.assertFalse(state.exists())
        process_observer(self.path, dict(data, hook_event_name='stop', status='completed'), 'stop')
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(json.loads((state / 'state.json').read_text())['roots'], {})
        task = dict(data, hook_event_name='beforeSubmitPrompt', generation_id='real-task', prompt='Real next task')
        process_observer(self.path, task, 'beforeSubmitPrompt')
        self.assertEqual(self.requests[-1]['path'], '/api/agents/observers/root-events')
        self.assertEqual(self.requests[-1]['body']['run_id'], 'real-task')
        self.assertEqual(self.requests[-1]['body']['type'], 'turn_started')

    def test_pending_marker_never_binds_on_prompt_or_substring(self):
        self.observer.update(native_session_id='pending:one', activation_marker='OFFICE_CURSOR_BIND_fixture')
        self.server(bind=True)
        for kind, text in [('beforeSubmitPrompt', 'OFFICE_CURSOR_BIND_fixture'),
                           ('afterAgentResponse', 'OFFICE_CURSOR_BIND_fixture_extra')]:
            data = {'hook_event_name': kind, 'conversation_id': 'new-native-parent',
                    'generation_id': 'onboarding', 'text': text, 'prompt': text}
            self.assertFalse(process_observer(self.path, data, kind))
        self.assertEqual(self.requests, [])


class CursorChildrenBackendTests(unittest.TestCase):
    def test_delayed_root_response_preserves_real_backend_terminal_time_and_delivery(self):
        from test_child_events import ChildEventsTests as BackendFixture
        fixture = BackendFixture()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        observer = fixture.credential
        state = fixture.directory / 'delayed-root-journal'
        outcomes = []
        def transport(credential, item, timeout=.6):
            result = fixture.b.child_events.root_event(credential['observer_id'], credential['token'], item['body'])
            outcomes.append(result)
            return 'delivered', None
        def emit(kind, **values):
            data = {'hook_event_name': kind, 'conversation_id': 'cursor-native-root',
                    'generation_id': 'delayed-native-turn', **values}
            handle(observer, state, data, kind, transport)
        def row():
            return fixture.b.history.db.execute('SELECT * FROM runs WHERE agent_id=? AND run_id=?',
                (fixture.roots['cursor']['id'], 'delayed-native-turn')).fetchone()

        emit('beforeSubmitPrompt', prompt='Public task with delayed delivery')
        emit('stop', status='completed')
        finished = row()['finished_at']
        self.assertEqual(row()['status'], 'done')
        self.assertEqual(row()['delivery'], '')
        emit('afterAgentResponse', text='Delayed public delivery password=hidden-fixture')
        self.assertEqual(len(outcomes), 3)
        self.assertTrue(all(result.get('accepted') for result in outcomes))
        self.assertEqual(row()['status'], 'done')
        self.assertEqual(row()['finished_at'], finished)
        self.assertIn('Delayed public delivery', row()['delivery'])
        self.assertNotIn('hidden-fixture', row()['delivery'])
        messages = fixture.b.history.messages(fixture.b.history_agents(), fixture.roots['cursor']['id'], run='delayed-native-turn')
        self.assertEqual([message['phase'] for message in messages], ['prompt', 'final'])
        self.assertEqual(json.loads((state / 'state.json').read_text())['outbox'], [])

    def test_mapper_native_lineage_and_public_delivery_are_accepted_by_real_backend(self):
        from test_child_events import ChildEventsTests as BackendFixture
        fixture = BackendFixture()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        observer, responses = fixture.credential, []
        native_child, native_call = 'native\nchild', 'actual\ntask-call'
        state = fixture.directory / 'native-cursor-journal'
        def transport(credential, item, timeout=.6):
            if item['route'] == 'root':
                result = fixture.b.child_events.root_event(credential['observer_id'], credential['token'], item['body'])
            else:
                result = fixture.b.child_events.accept('observer', credential['observer_id'], credential['token'], item['body'])
            responses.append((copy.deepcopy(item), result))
            return 'delivered', None
        def emit(kind, **values):
            data = {'hook_event_name': kind, 'conversation_id': 'cursor-native-root',
                    'generation_id': 'actual-native-root-turn', **values}
            handle(observer, state, data, kind, transport)
        emit('beforeSubmitPrompt', prompt='Public actual native task', workspace_roots=['/tmp/native-project'])
        emit('subagentStart', parent_conversation_id='cursor-native-root', subagent_id=native_child,
             tool_call_id=native_call, subagent_type='explore', task='Public child task')
        emit('subagentStart', conversation_id='actual-child-conversation', generation_id='actual-child-turn',
             parent_conversation_id='actual-child-conversation', parent_tool_call_id=native_call,
             subagent_id='native-grandchild', tool_call_id='actual-grandchild-task', task='Public grandchild task')
        emit('subagentStop', conversation_id='actual-child-conversation', generation_id='actual-child-turn',
             parent_tool_call_id=native_call, subagent_id='native-grandchild', status='completed', summary='Public grandchild result')
        emit('afterAgentResponse', conversation_id='actual-child-conversation', generation_id='actual-child-turn', text='Public child progress')
        emit('subagentStop', subagent_id=native_child, child_conversation_id='actual-child-conversation',
             status='completed', summary='Public child delivery')
        self.assertEqual(len(responses), 7)
        children = fixture.children()
        self.assertEqual(len(children), 2)
        self.assertTrue(all(child['state'] == 'done' for child in children))
        self.assertTrue(all(child['evidence'] == 'native_hook' for child in children))
        row = fixture.b.history.db.execute('SELECT status FROM live_runs WHERE agent_id=?',
                                           (fixture.roots['cursor']['id'],)).fetchone()
        self.assertEqual(row[0], 'working')
        self.assertEqual(json.loads((state / 'state.json').read_text())['outbox'], [])


if __name__ == '__main__': unittest.main()
