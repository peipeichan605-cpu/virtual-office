"""Exact native root request boundaries without reading private Cursor fields."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from cursor_public_store import CursorPublicStore


class CursorRootPublicStoreTests(unittest.TestCase):
    ROOT='selected-root'
    REQUEST='native-request-one'

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve()
        self.path=self.base/'state.vscdb'
        self.db=sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE cursorDiskKV(key TEXT PRIMARY KEY,value TEXT)')
        self.headers=[]
        self.meta={'composerId':self.ROOT,'blobEncryptionKey':'PRIVATE_ROOT_KEY',
            'headerLog':{'chunkSize':256,'chunkCount':1,'count':0,'generation':'header-storage-version'},
            'fullConversationHeadersOnly':[]}
        self.bubble('human-one',1,'PRIVATE_HUMAN_PROMPT',requestId=self.REQUEST,
            context={'private':'PRIVATE_HUMAN_CONTEXT'})
        self.bubble('progress',2,'Public root progress')
        self.flush()
        self.reader=CursorPublicStore(self.path)

    def put(self,key,value):
        self.db.execute('INSERT OR REPLACE INTO cursorDiskKV VALUES (?,?)',(key,json.dumps(value)))

    def bubble(self,ident,kind,text='',**extra):
        data={'bubbleId':ident,'type':kind,'text':text,'createdAt':'2026-11-10T12:10:00Z',**extra}
        group={'isRenderable':bool(text),'hasText':True if text else None}
        if data.get('thinking') is not None:group.update(capabilityType=30,hasThinking=True)
        if data.get('toolFormerData') is not None:group.update(capabilityType=15,toolFormerTool=999)
        if 'capabilityType' in data:group['capabilityType']=data['capabilityType']
        self.headers.append({'bubbleId':ident,'type':kind,'createdAt':data['createdAt'],
            'grouping':{k:v for k,v in group.items() if v is not None}})
        self.put('bubbleId:'+self.ROOT+':'+ident,data)

    def flush(self):
        self.meta['headerLog']['count']=len(self.headers)
        self.put('composerData:'+self.ROOT,self.meta)
        self.put('composerConvHeaders:'+self.ROOT+':000000',self.headers)
        self.db.commit()

    def snapshot(self,**changes):
        return self.reader.root_snapshot(changes.get('conversation_id',self.ROOT),
            changes.get('request_id',self.REQUEST),terminal=changes.get('terminal',False))

    def test_exact_request_public_progress_and_native_terminal_tail(self):
        self.bubble('tool',2,'PRIVATE_TOOL_BODY',capabilityType=15,
            toolFormerData={'params':'PRIVATE_TOOL_ARGUMENTS'})
        self.bubble('final',2,'Actual root delivery')
        self.flush()
        live=self.snapshot()
        self.assertEqual(live['boundary_id'],'human-one')
        self.assertEqual([m['text'] for m in live['messages']],
            ['Public root progress','Actual root delivery'])
        self.assertTrue(live['messages'][0]['closed'])
        self.assertFalse(live['messages'][1]['closed'])
        self.assertIsNone(live['final_id'])
        terminal=self.snapshot(terminal=True)
        self.assertEqual(terminal['final_id'],'final')
        self.assertTrue(all(m['closed'] for m in terminal['messages']))
        self.assertNotIn('PRIVATE',json.dumps(terminal))

    def test_request_id_is_not_header_storage_generation_text_or_latest_human(self):
        self.assertIsNone(self.snapshot(request_id='header-storage-version'))
        self.assertIsNone(self.snapshot(request_id='PRIVATE_HUMAN_PROMPT'))
        self.assertIsNone(self.snapshot(request_id='unseen-request'))
        self.bubble('new-human',1,self.REQUEST,requestId='new-request')
        self.bubble('new-final',2,'New result')
        self.flush()
        old=self.snapshot(terminal=True)
        self.assertEqual([m['text'] for m in old['messages']],['Public root progress'])
        self.assertEqual(old['boundary_id'],'human-one')

    def test_reused_chat_span_stops_at_next_human_request(self):
        self.bubble('first-final',2,'First result')
        self.bubble('human-two',1,'PRIVATE_SECOND_PROMPT',requestId='native-request-two')
        self.bubble('second-final',2,'Second result')
        self.flush()
        first=self.snapshot(terminal=True)
        second=self.snapshot(request_id='native-request-two',terminal=True)
        self.assertEqual([m['text'] for m in first['messages']],['Public root progress','First result'])
        self.assertEqual(first['final_id'],'first-final')
        self.assertEqual([m['text'] for m in second['messages']],['Second result'])
        self.assertEqual(second['boundary_id'],'human-two')

    def test_missing_nonstring_and_ambiguous_request_identity_fail_closed(self):
        for request in (None,123,{},[],True):
            self.put('bubbleId:'+self.ROOT+':human-one',{'bubbleId':'human-one','type':1,'requestId':request})
            self.db.commit()
            self.assertIsNone(self.snapshot())
        self.put('bubbleId:'+self.ROOT+':human-one',{'bubbleId':'human-one','type':1,'requestId':self.REQUEST})
        self.bubble('duplicate-human',1,'SAME_TEXT_DOES_NOT_IDENTIFY',requestId=self.REQUEST)
        self.flush()
        self.assertIsNone(self.snapshot())

    def test_human_boundary_requires_same_bubble_identity_and_native_type(self):
        for change in ({'bubbleId':'foreign-bubble'},{'type':2},{'type':True}):
            data={'bubbleId':'human-one','type':1,'requestId':self.REQUEST,**change}
            self.put('bubbleId:'+self.ROOT+':human-one',data);self.db.commit()
            self.assertIsNone(self.snapshot())

    def test_foreign_conversation_is_never_searched_for_matching_request(self):
        other='foreign-root'
        self.put('composerData:'+other,{'composerId':other,
            'fullConversationHeadersOnly':[{'bubbleId':'foreign-human','type':1}]})
        self.put('bubbleId:'+other+':foreign-human',{'bubbleId':'foreign-human','type':1,'requestId':'foreign-request'})
        self.db.commit()
        self.assertIsNone(self.snapshot(request_id='foreign-request'))
        self.assertIsNone(self.snapshot(conversation_id='missing-root'))
        self.put('composerData:'+self.ROOT,{**self.meta,'composerId':other})
        self.db.commit()
        self.assertIsNone(self.snapshot())

    def test_nonpublic_thought_tool_notification_and_unknown_fields_are_excluded(self):
        for ident,extra in [
            ('thinking',{'thinking':{'text':'PRIVATE_THOUGHT'},'capabilityType':30}),
            ('null-thinking',{'thinking':None}),
            ('tool',{'toolFormerData':{'params':'PRIVATE_TOOL_PARAMS'},'capabilityType':15}),
            ('null-tool',{'toolFormerData':None}),
            ('status',{'serviceStatusUpdate':{'private':'PRIVATE_STATUS'}}),
            ('event',{'eventNotification':{'private':'PRIVATE_EVENT'}}),
            ('simulated',{'isSimulatedMsg':True}),
            ('unknown',{'capabilityType':888}),
            ('error',{'errorDetails':{'message':'PRIVATE_ERROR'}}),
            ('summary',{'conversationSummary':{'text':'PRIVATE_SUMMARY'}}),
        ]:
            self.bubble(ident,2,'PRIVATE_BODY_'+ident,**extra)
        self.flush()
        result=self.snapshot(terminal=True)
        self.assertEqual([m['text'] for m in result['messages']],['Public root progress'])
        self.assertIsNone(result['final_id'])
        self.assertNotIn('PRIVATE',json.dumps(result))

    def test_delayed_final_stays_unconfirmed_until_public_tail_is_persisted(self):
        self.bubble('tool',2,'',capabilityType=15,toolFormerData={'tool':999})
        self.bubble('pending-final',2,'')
        self.flush()
        self.assertIsNone(self.snapshot(terminal=True)['final_id'])
        self.put('bubbleId:'+self.ROOT+':pending-final',{'bubbleId':'pending-final','type':2,
            'text':'Final after flush','createdAt':'2026-11-10T12:11:00Z'})
        self.headers[-1]['grouping']={'hasText':True,'isRenderable':True}
        self.flush()
        result=self.snapshot(terminal=True)
        self.assertEqual(result['final_id'],'pending-final')
        self.assertEqual(result['messages'][-1]['text'],'Final after flush')

    def test_read_only_projected_queries_never_load_human_text_or_private_source(self):
        before=self.db.total_changes
        captured=[]
        connect=sqlite3.connect
        def connected(*args,**kwargs):
            self.assertIn('?mode=ro',args[0]);self.assertTrue(kwargs['uri'])
            c=connect(*args,**kwargs);c.set_trace_callback(captured.append);return c
        with mock.patch('cursor_public_store.sqlite3.connect',side_effect=connected):
            result=self.snapshot(terminal=True)
        self.assertEqual(before,self.db.total_changes)
        self.assertNotIn('PRIVATE',json.dumps(result))
        queries='\n'.join(captured)
        for forbidden in ('SELECT value ','blobEncryptionKey','$.thinking.text','$.context','$.toolFormerData.params'):
            self.assertNotIn(forbidden,queries)
        self.assertFalse(any(q.startswith(('INSERT','UPDATE','DELETE')) for q in captured))

    def test_inline_header_schema_and_invalid_partial_chunk(self):
        inline=copy.deepcopy(self.meta);inline.pop('headerLog')
        inline['fullConversationHeadersOnly']=self.headers
        self.put('composerData:'+self.ROOT,inline);self.db.commit()
        self.assertIsNotNone(self.snapshot())
        self.flush()
        self.put('composerConvHeaders:'+self.ROOT+':000000',self.headers[:1]);self.db.commit()
        self.assertIsNone(self.snapshot())

    def test_invalid_native_ids_and_schema_views_fail_closed(self):
        for changes in ({'conversation_id':"x' OR 1=1 --"},{'request_id':''},
                        {'request_id':'bad\nrequest'},{'request_id':None}):
            self.assertIsNone(self.snapshot(**changes))
        self.db.execute('ALTER TABLE cursorDiskKV RENAME TO actualValues')
        self.db.execute('CREATE VIEW cursorDiskKV AS SELECT key,value FROM actualValues');self.db.commit()
        self.assertIsNone(self.snapshot())

    def test_file_replacement_and_symlink_guards_apply_to_root_snapshots(self):
        self.assertIsNotNone(self.snapshot())
        link=self.base/'linked.vscdb';link.symlink_to(self.path)
        self.assertIsNone(CursorPublicStore(link).root_snapshot(self.ROOT,self.REQUEST))
        self.db.close()
        old=self.base/'old.vscdb';os.replace(self.path,old)
        new=sqlite3.connect(self.path);original=sqlite3.connect(old)
        original.backup(new);original.close();new.close()
        self.db=sqlite3.connect(self.path);self.addCleanup(self.db.close)
        self.assertIsNone(self.snapshot())
        self.assertIsNotNone(self.snapshot())

    def test_foreign_source_owner_is_rejected_without_modification(self):
        if not hasattr(os,'getuid'):
            self.skipTest('POSIX ownership check')
        owner=os.getuid()
        with mock.patch('cursor_public_store.os.getuid',return_value=owner+1000):
            self.assertIsNone(self.snapshot())
        self.assertIsNotNone(self.snapshot())


if __name__=='__main__':
    unittest.main()
