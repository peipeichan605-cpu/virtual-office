import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from cursor_public_store import CursorPublicStore, default_cursor_db_path, hook_id


class CursorPublicStoreTests(unittest.TestCase):
    ROOT = 'root-conversation'
    CHILD = 'child-conversation'
    RUN = 'root-generation'
    CALL = 'call-provider\nfc-native_0'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.path = self.base / 'state.vscdb'
        self.db = sqlite3.connect(self.path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE cursorDiskKV(key TEXT PRIMARY KEY,value TEXT)')
        self.meta = {'composerId': self.CHILD, 'blobEncryptionKey': 'PRIVATE_BLOB_KEY',
            'subagentInfo': {'parentComposerId': self.ROOT, 'rootParentConversationId': self.ROOT,
                'rootParentRequestId': self.RUN, 'toolCallId': self.CALL, 'toolCallIdHistory': [self.CALL]},
            'headerLog': {'chunkSize': 256, 'chunkCount': 1, 'count': 0, 'generation': 'header-version-1'},
            'fullConversationHeadersOnly': []}
        self.put('composerData:' + self.ROOT, {'composerId': self.ROOT, 'subagentComposerIds': [self.CHILD], 'blobEncryptionKey': 'PRIVATE_PARENT_KEY'})
        self.headers = []
        self.bubble('boundary', 1, 'PRIVATE_TASK_PROMPT', subagentSpawnTaskToolCallId=self.CALL)
        self.bubble('progress', 2, 'Public progress')
        self.flush()
        self.reader = CursorPublicStore(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def put(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO cursorDiskKV VALUES (?,?)', (key, json.dumps(value)))

    def bubble(self, identity, kind, text='', **extra):
        data = {'bubbleId': identity, 'type': kind, 'text': text, 'createdAt': '2026-11-10T06:10:00Z', **extra}
        group = {'isRenderable': bool(text), 'hasText': True if text else None}
        if data.get('thinking') is not None:
            group.update({'capabilityType': 30, 'hasThinking': True})
        if data.get('toolFormerData') is not None:
            group.update({'capabilityType': 15, 'toolFormerTool': 999})
        if 'capabilityType' in data:
            group['capabilityType'] = data['capabilityType']
        # Native JSON omits optional false flags, as its header projector does.
        group = {k: v for k, v in group.items() if v is not None}
        self.headers.append({'bubbleId': identity, 'type': kind, 'createdAt': data['createdAt'], 'grouping': group})
        self.put('bubbleId:' + self.CHILD + ':' + identity, data)

    def flush(self):
        self.meta['headerLog']['count'] = len(self.headers)
        self.put('composerData:' + self.CHILD, self.meta)
        self.put('composerConvHeaders:' + self.CHILD + ':000000', self.headers)
        self.db.commit()

    def snapshot(self, **changes):
        args = dict(root_id=self.ROOT, parent_id=self.ROOT, parent_run_id=self.RUN,
                    tool_call_id=hook_id(self.CALL))
        args.update(changes)
        return self.reader.snapshot(**args)

    def test_native_chunked_wal_public_progress_and_exact_final(self):
        self.bubble('tool\nopaque-call', 2, 'PRIVATE_TOOL_BODY', capabilityType=15, toolFormerData={'params': 'PRIVATE_TOOL_ARGUMENTS'})
        self.bubble('final', 2, 'Actual delivery\n437')
        self.flush()
        live = self.snapshot()
        self.assertEqual([m['text'] for m in live['messages']], ['Public progress', 'Actual delivery\n437'])
        self.assertTrue(live['messages'][0]['closed'])
        self.assertFalse(live['messages'][1]['closed'])
        self.assertIsNone(live['final_id'])
        final = self.snapshot(terminal=True)
        self.assertEqual(final['final_id'], 'final')
        self.assertTrue(all(m['closed'] for m in final['messages']))
        self.assertEqual(final['boundary_id'], 'boundary')
        self.assertEqual(final['messages'][1]['at'], '2026-11-10T06:10:00Z')
        self.assertNotIn('PRIVATE', json.dumps(final))

    def test_no_thought_tool_status_or_unknown_capability_is_exported(self):
        self.bubble('thinking', 2, 'PRIVATE_REASONING', thinking={'text': 'PRIVATE_THOUGHT'}, capabilityType=30)
        self.bubble('thinking-null', 2, 'PRIVATE_NULL_THOUGHT', thinking=None)
        self.bubble('tool-null', 2, 'PRIVATE_TOOL_NULL', toolFormerData=None)
        self.bubble('status', 2, 'PRIVATE_STATUS', serviceStatusUpdate={'detail': 'PRIVATE_SERVICE'})
        self.bubble('event', 2, 'PRIVATE_EVENT', eventNotification={'detail': 'PRIVATE_EVENT_DETAIL'})
        self.bubble('simulated', 2, 'PRIVATE_SIMULATED', isSimulatedMsg=True)
        self.bubble('unknown', 2, 'PRIVATE_CAPABILITY', capabilityType=888)
        self.bubble('error', 2, 'PRIVATE_ERROR', errorDetails={'message': 'private'})
        self.bubble('summary', 2, 'PRIVATE_SUMMARY', conversationSummary={'text': 'private'})
        self.flush()
        data = self.snapshot(terminal=True)
        self.assertEqual([m['text'] for m in data['messages']], ['Public progress'])
        self.assertNotIn('PRIVATE', json.dumps(data))
        self.assertIsNone(data['final_id'])

    def test_resumed_child_uses_exact_current_task_boundary_not_old_text(self):
        newer = 'call-provider\nfc-native_1'
        self.meta['subagentInfo']['toolCallId'] = newer
        self.meta['subagentInfo']['toolCallIdHistory'].append(newer)
        self.bubble('new-boundary', 1, 'New delegated task', subagentSpawnTaskToolCallId=newer)
        self.bubble('new-final', 2, 'New result')
        self.flush()
        new = self.snapshot(tool_call_id=hook_id(newer), terminal=True)
        old = self.snapshot(terminal=True)
        self.assertEqual([m['text'] for m in new['messages']], ['New result'])
        self.assertEqual([m['text'] for m in old['messages']], ['Public progress'])
        self.assertEqual(new['final_id'], 'new-final')

    def test_foreign_root_parent_generation_or_call_is_rejected(self):
        for field, value in [('parentComposerId', 'foreign-parent'), ('rootParentConversationId', 'foreign-root'), ('rootParentRequestId', 'foreign-generation'), ('toolCallId', 'foreign-call')]:
            with self.subTest(field=field):
                old = copy.deepcopy(self.meta)
                self.meta['subagentInfo'][field] = value
                if field == 'toolCallId':
                    self.meta['subagentInfo']['toolCallIdHistory'] = [value]
                self.flush()
                self.assertIsNone(self.snapshot())
                self.meta = old
                self.flush()

    def test_title_and_same_task_text_never_supply_identity(self):
        self.meta['name'] = self.CALL
        self.put('bubbleId:' + self.CHILD + ':boundary', {'bubbleId': 'boundary', 'type': 1, 'text': self.CALL, 'subagentSpawnTaskToolCallId': self.CALL.replace('\n', '')})
        self.flush()
        self.assertIsNone(self.snapshot())

    def test_duplicate_task_boundary_and_ambiguous_children_fail_closed(self):
        self.bubble('duplicate-boundary', 1, 'same task', subagentSpawnTaskToolCallId=self.CALL)
        self.flush()
        self.assertIsNone(self.snapshot())
        self.headers.pop()
        self.flush()
        other = copy.deepcopy(self.meta)
        other['composerId'] = 'other-child'
        other['fullConversationHeadersOnly'] = self.headers
        other.pop('headerLog')
        self.put('composerData:other-child', other)
        for identity in ['boundary', 'progress']:
            data = json.loads(self.db.execute('SELECT value FROM cursorDiskKV WHERE key=?', ('bubbleId:' + self.CHILD + ':' + identity,)).fetchone()[0])
            self.put('bubbleId:other-child:' + identity, data)
        self.put('composerData:' + self.ROOT, {'composerId': self.ROOT, 'subagentComposerIds': [self.CHILD, 'other-child']})
        self.db.commit()
        self.assertIsNone(self.snapshot())
        self.assertEqual(self.snapshot(child_hint=self.CHILD)['conversation_id'], self.CHILD)
        self.db.execute('DELETE FROM cursorDiskKV WHERE key=?', ('bubbleId:other-child:boundary',))
        self.db.commit()
        self.assertIsNone(self.snapshot())

    def test_missing_or_partial_chunk_and_invalid_json_fail_closed(self):
        key = 'composerConvHeaders:' + self.CHILD + ':000000'
        for value in ['[', '{}', json.dumps(self.headers[:1])]:
            self.db.execute('UPDATE cursorDiskKV SET value=? WHERE key=?', (value, key)); self.db.commit()
            self.assertIsNone(self.snapshot())
        self.flush()
        self.assertIsNotNone(self.snapshot())

    def test_native_stop_does_not_promote_pretool_progress_or_blank_pending_final(self):
        self.bubble('tool', 2, '', capabilityType=15, toolFormerData={'tool': 999})
        self.bubble('pending-final', 2, '')
        self.flush()
        self.assertIsNone(self.snapshot(terminal=True)['final_id'])
        self.put('bubbleId:' + self.CHILD + ':pending-final', {'bubbleId': 'pending-final', 'type': 2, 'text': 'Final after flush', 'createdAt': '2026-11-10T06:11:00Z'})
        self.headers[-1]['grouping'] = {'hasText': True, 'isRenderable': True}
        self.flush()
        self.assertEqual(self.snapshot(terminal=True)['final_id'], 'pending-final')

    def test_inline_headers_supported_without_reading_whole_composer(self):
        self.meta.pop('headerLog')
        self.meta['fullConversationHeadersOnly'] = self.headers
        self.put('composerData:' + self.CHILD, self.meta)
        self.db.commit()
        self.assertEqual(self.snapshot()['messages'][0]['text'], 'Public progress')

    def test_invalid_ids_limits_and_sql_injection_are_rejected(self):
        self.assertIsNone(self.snapshot(root_id="x' OR 1=1 --"))
        self.meta['headerLog']['chunkCount'] = 100000
        self.flush()
        self.assertIsNone(self.snapshot())

    def test_read_only_reader_does_not_change_source_or_query_private_fields(self):
        before = self.db.total_changes
        captured = []
        real_connect = sqlite3.connect
        def connected(*args, **kwargs):
            self.assertIn('?mode=ro', args[0])
            self.assertTrue(kwargs['uri'])
            c = real_connect(*args, **kwargs)
            c.set_trace_callback(captured.append)
            return c
        with mock.patch('cursor_public_store.sqlite3.connect', side_effect=connected):
            value = self.snapshot(terminal=True)
        self.assertEqual(before, self.db.total_changes)
        self.assertNotIn('PRIVATE', json.dumps(value))
        queries = '\n'.join(captured)
        self.assertNotIn('SELECT value ', queries)
        self.assertNotIn('blobEncryptionKey', queries)
        self.assertNotIn('$.thinking.text', queries)
        self.assertNotIn('$.toolFormerData.params', queries)
        self.assertFalse(any(q.startswith(('INSERT', 'UPDATE', 'DELETE')) for q in captured))

    def test_posix_foreign_owner_is_rejected_without_changing_ownership(self):
        if not hasattr(os, 'getuid'):
            self.skipTest('POSIX ownership check')
        owner = os.getuid()
        with mock.patch('cursor_public_store.os.getuid', return_value=owner + 1000):
            self.assertIsNone(self.snapshot())
        self.assertIsNotNone(self.snapshot())

    def test_own_hint_can_recover_before_parent_child_list_flushes(self):
        self.put('composerData:' + self.ROOT, {'composerId': self.ROOT, 'subagentComposerIds': []})
        self.db.commit()
        self.assertIsNone(self.snapshot())
        self.assertEqual(self.snapshot(child_hint=self.CHILD)['conversation_id'], self.CHILD)

    def test_boolean_header_flags_false_are_not_private_bubble_presence(self):
        self.headers[-1]['grouping'].update(hasThinking=False, hasServiceStatus=False,
            hasEventNotification=False, isSimulatedMsg=False)
        self.flush()
        self.assertEqual(self.snapshot()['messages'][0]['text'], 'Public progress')

    def test_header_metadata_is_not_accepted_through_sqlite_view(self):
        self.db.execute('ALTER TABLE cursorDiskKV RENAME TO actualValues')
        self.db.execute('CREATE VIEW cursorDiskKV AS SELECT key,value FROM actualValues')
        self.db.commit()
        self.assertIsNone(self.snapshot())

    def test_symlink_database_and_sidecar_fail_closed(self):
        link = self.base / 'linked.vscdb'
        link.symlink_to(self.path)
        self.assertIsNone(CursorPublicStore(link).snapshot(self.ROOT, self.ROOT, self.RUN, hook_id(self.CALL)))
        self.db.close()
        wal = self.path.with_name(self.path.name + '-wal')
        if wal.exists():
            wal.unlink()
        wal.symlink_to(self.base / 'nonexistent')
        self.assertIsNone(self.snapshot())
        wal.unlink()
        self.db = sqlite3.connect(self.path)

    def test_file_replacement_is_fenced_before_reestablishing_native_scope(self):
        self.assertIsNotNone(self.snapshot())
        self.db.close()
        replaced = self.base / 'old.vscdb'
        os.replace(self.path, replaced)
        new = sqlite3.connect(self.path)
        old = sqlite3.connect(replaced)
        old.backup(new); old.close(); new.close()
        self.db = sqlite3.connect(self.path)
        self.assertIsNone(self.snapshot())
        self.assertIsNotNone(self.snapshot())

    def test_defaults_are_portable_across_users_and_os_and_custom_paths(self):
        self.assertEqual(default_cursor_db_path('/different/person', 'darwin'), Path('/different/person/Library/Application Support/Cursor/User/globalStorage/state.vscdb'))
        self.assertEqual(default_cursor_db_path('/person', 'linux', {'XDG_CONFIG_HOME': '/config/custom'}), Path('/config/custom/Cursor/User/globalStorage/state.vscdb'))
        self.assertEqual(default_cursor_db_path('/person', 'linux', {}), Path('/person/.config/Cursor/User/globalStorage/state.vscdb'))
        self.assertEqual(default_cursor_db_path('/person', 'win32', {'APPDATA': '/roaming'}), Path('/roaming/Cursor/User/globalStorage/state.vscdb'))
        self.assertEqual(CursorPublicStore(self.path).path, self.path)

    def test_unrelated_installed_adapters_package_cannot_shadow_local_codec(self):
        site = self.base / 'unrelated-site'
        (site / 'adapters').mkdir(parents=True)
        (site / 'adapters/__init__.py').write_text('raise RuntimeError("unrelated package imported")\n')
        project = str(Path(__file__).resolve().parent)
        script = 'import sys; sys.path[:0]=sys.argv[1:]; import cursor_public_store; assert cursor_public_store.hook_id("call\\nopaque").startswith("cursor:v1:")'
        result = subprocess.run([sys.executable, '-c', script, project, str(site)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
