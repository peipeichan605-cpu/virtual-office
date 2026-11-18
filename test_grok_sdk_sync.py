"""Offline native-schema contracts; not live Grok/App acceptance evidence."""
import copy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
import uuid

from commands import Commands
from grok_sdk_sync import GrokSdkSync,SyncError,cursor_token,public_entry,token_parts
from history import HistoryStore

BOT='00000000-0000-4000-8000-000000000001'
SID='sdk-00000000-0000-4000-8000-000000000002'
AGENT='grok_bot:'+SID
ACCOUNT='a'*64


class Client:
    def __init__(self):
        self.account=ACCOUNT;self.events=[];self.calls=[];self.pages={None:{'generation':1,'entries':[]}}
    def auth_status(self):return {'account_fingerprint':self.account,'state':'authenticated'}
    def list_transcript(self,bot_id,session_id,limit=100,before_seq=None,generation=None):
        self.calls.append(('history',bot_id,session_id,before_seq))
        return copy.deepcopy(self.pages[before_seq])
    def watch(self,bot_id,session_id,resume_from=None,stop_event=None):
        self.calls.append(('watch',bot_id,session_id,resume_from))
        yield from self.events
    def agent_statuses(self,ids):
        self.calls.append(('state',ids))
        return {'statuses':[{'agentId':BOT,'isRunning':False}]}


class Adapter:
    mode,serial,same_session='grok_sdk',True,True
    def __init__(self):self.client=Client()
    def available(self,sid):return True
    def native_verified(self,sid):return True
    def submit(self,sid,text):raise AssertionError('Observer never sends')


def body_prompt(text='public prompt',nonce=None,batch='batch-one',task=None):
    return {'kind':'message','id':'prompt-one','role':'user','content':text,'isStreaming':False,
            'clientNonce':nonce or 'nonce-one','batchId':batch,'taskId':task,'timestampMs':1730000000000}


def body_reply(text='public reply',batch='batch-one',task=None,streaming=False):
    return {'kind':'send-message','id':'reply-one','batchId':batch,'streaming':streaming,
            'message':{'type':'text','content':text,'task_id':task},'timestampMs':1730000001000}


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.h=HistoryStore(Path(self.temp.name)/'history.sqlite3',secrets=('fixture-secret',))
        self.definition={'id':AGENT,'source_id':SID,'provider':'grok_bot','driver':'grok_sdk','name':'Fixture'}
        self.config={'data_dir':self.temp.name,'topic':'fixture-topic','agents':[self.definition],
            'command_bindings':{AGENT:{'adapter':'grok_sdk','session_id':SID}},
            'command_adapters':{'grok_sdk':{'sessions':{SID:{'bot_id':BOT,'native_session_id':'','account_fingerprint':ACCOUNT}}}}}
        self.adapter=Adapter();self.commands=Commands(self.config,self.h,adapters={'grok_sdk':self.adapter})
        self.status=[]
        self.bridge=SimpleNamespace(config=self.config,history=self.h,commands=self.commands,definitions={AGENT:self.definition},
            report=lambda *args,**kwargs:self.status.append((args,kwargs)),native_status=lambda *args:self.status.append(args))
        self.sync=GrokSdkSync(self.bridge);self.member=self.sync._member(AGENT)
        self.sync.bootstrap(self.member,self.adapter.client)
    def tearDown(self):
        self.sync.close();self.commands.close();self.h.close();self.temp.cleanup()
    def event(self,body,entry='prompt-one',seq=1,updated=None,kind='entry',generation=1):
        updated=seq if updated is None else updated
        return {'type':kind,'id':cursor_token(BOT,'',generation,updated),
            'data':{'botId':BOT,'sessionId':'','generation':generation,'seq':str(seq),'updatedSeq':str(updated),'entryId':entry,'entry':body}}
    def feed(self,body,**kw):return self.sync.process_event(self.member,self.event(body,**kw))
    def state(self,running=False,settlement=None):
        live={'agentId':BOT,'sessionId':'','isRunning':running}
        if settlement:live['lastTurnSettlement']=settlement
        return self.sync.process_event(self.member,{'type':'agent.state','data':{'botId':BOT,'sessionId':'','live':[live]}})
    def settlement(self,nonce='nonce-one',turn='batch-one',outcome='success'):
        return self.state(settlement={'clientNonce':nonce,'turnId':turn,'outcome':outcome,'settledAtMs':'1730000002000'})
    def runs(self):return [dict(r) for r in self.h.db.execute('SELECT * FROM grok_sdk_runs')]
    def messages(self):return self.h.messages({AGENT},AGENT,limit=100)

    def test_only_public_native_shapes_are_extracted(self):
        self.assertEqual(public_entry(body_prompt())['text'],'public prompt')
        self.assertEqual(public_entry(body_reply())['text'],'public reply')
        for body in [{'kind':'message','role':'assistant','content':'private reasoning'},
                     {'kind':'model','text':'private reasoning'},
                     dict(body_prompt(),fromAgent='peer'),dict(body_prompt(),isStreaming=True),
                     {'kind':'send-message','message':{'type':'text','content':'external','channel':'slack'}}]:
            self.assertIsNone(public_entry(body))
        omitted=body_prompt();omitted.pop('isStreaming')
        self.assertEqual(public_entry(omitted)['kind'],'prompt')
        for flag in [True,'false',0,1,{},[]]:
            self.assertIsNone(public_entry(dict(omitted,isStreaming=flag)))

    def test_live_request_id_pairs_multiple_replies_independent_of_turn_and_nonce(self):
        prompt=dict(body_prompt(batch=None),requestId='native-request-A')
        first=dict(body_reply('first',batch=None),requestId='native-request-A')
        second=dict(body_reply('second',batch=None),requestId='native-request-A')
        self.feed(prompt);self.feed(first,entry='first',seq=2);self.feed(second,entry='second',seq=3)
        self.assertEqual(len(self.runs()),1);self.assertEqual(self.runs()[0]['delivery'],'first\n\nsecond')
        self.settlement(turn='distinct-settlement-turn')
        self.assertEqual(self.runs()[0]['status'],'done')
        self.assertEqual(self.runs()[0]['native_request'],'native-request-A')

    def test_request_id_ambiguity_never_assigns_one_reply_to_multiple_inputs(self):
        self.feed(dict(body_prompt(batch=None),requestId='shared'))
        self.feed(dict(body_prompt('second',nonce='second',batch=None),requestId='shared'),entry='second-input',seq=2)
        self.feed(dict(body_reply(batch=None),requestId='shared'),entry='reply-one',seq=3)
        self.assertEqual(len(self.runs()),3)
        self.assertEqual(self.runs()[-1]['warning'],'grok_sdk_reply_unpaired')
        self.settlement();self.assertEqual(self.runs()[0]['status'],'working')

    def test_request_id_mismatch_cannot_fallback_to_conflicting_batch(self):
        self.feed(dict(body_prompt(),requestId='request-A'))
        self.feed(dict(body_reply(),requestId='request-B'),entry='reply-one',seq=2)
        self.assertEqual(len(self.runs()),2);self.settlement()
        self.assertEqual(self.runs()[0]['status'],'working')

    def test_reply_before_prompt_is_repaired_by_unique_request_id(self):
        self.feed(dict(body_reply('early visible reply',batch=None),requestId='native-request'),entry='reply-one',seq=2)
        self.feed(dict(body_prompt(batch=None),requestId='native-request'),entry='prompt-one',seq=1,updated=3)
        self.assertEqual(len(self.runs()),1)
        self.assertEqual(self.runs()[0]['delivery'],'early visible reply')
        self.settlement(turn='native-turn')
        self.assertEqual(self.runs()[0]['status'],'done')

    def test_later_prompt_metadata_repairs_request_pairing(self):
        self.feed(body_prompt(batch=None))
        self.feed(dict(body_reply(batch=None),requestId='later-request'),entry='reply-one',seq=2)
        self.assertEqual(len(self.runs()),2)
        self.feed(dict(body_prompt(batch=None),requestId='later-request'),entry='prompt-one',seq=1,updated=3)
        self.assertEqual(len(self.runs()),1)
        self.settlement();self.assertEqual(self.runs()[0]['status'],'done')

    def test_revised_entry_cannot_silently_change_request_identity(self):
        self.feed(dict(body_prompt(),requestId='request-A'))
        before=self.sync._member(AGENT)['cursor']
        with self.assertRaises(SyncError):
            self.feed(dict(body_prompt(),requestId='request-B'),entry='prompt-one',seq=1,updated=2)
        self.assertEqual(self.sync._member(AGENT)['cursor'],before)

    def test_public_prompt_reply_and_exact_settlement(self):
        self.feed(body_prompt());self.state(True);self.feed(body_reply(),entry='reply-one',seq=2)
        self.assertTrue(self.sync.busy(AGENT));self.assertEqual(self.runs()[0]['status'],'working')
        self.settlement();self.assertEqual(self.runs()[0]['status'],'done')
        self.assertEqual([m['role'] for m in self.messages()],['user','assistant'])
        self.assertEqual(self.messages()[-1]['phase'],'final')
        self.assertEqual(self.h.db.execute('SELECT delivery FROM runs').fetchone()[0],'public reply')

    def test_decorates_history_list_and_detail_without_assuming_run_id(self):
        self.feed(body_reply(batch=None),entry='orphan',seq=1)
        item=self.h.list({AGENT},AGENT)[0]
        self.assertNotIn('run_id',item)
        self.assertEqual(self.sync.decorate_run(item)['sync_warning'],'grok_sdk_reply_unpaired')
        detail=self.h.detail(item['id'],{AGENT})
        self.assertEqual(self.sync.decorate_run(detail)['sync_warning'],'grok_sdk_reply_unpaired')
        self.assertEqual(self.sync.decorate_run({'agent_id':'codex:other','id':'a'}),{'agent_id':'codex:other','id':'a'})

    def test_idle_ack_and_stream_end_are_not_completion(self):
        self.feed(body_prompt());self.feed(body_reply(),entry='reply-one',seq=2)
        self.state(False)
        self.sync.process_event(self.member,{'type':'connected','data':{'absoluteLifetimeMs':'3600000'}})
        self.sync.observe(self.member,self.adapter.client,threading.Event())
        self.assertEqual(self.runs()[0]['status'],'working')
        self.assertEqual(self.messages()[-1]['phase'],'commentary')

    def test_settlement_before_reply_waits_for_actual_delivery(self):
        self.feed(body_prompt());self.settlement()
        self.assertEqual(self.runs()[0]['status'],'working')
        self.feed(body_reply(),entry='reply-one',seq=2)
        self.assertEqual(self.runs()[0]['status'],'done')

    def test_streaming_reply_and_settlement_wait_for_full_public_body(self):
        self.feed(body_prompt());self.feed(body_reply('partial',streaming=True),entry='reply-one',seq=2)
        self.settlement();self.assertEqual(self.runs()[0]['status'],'working')
        self.feed(body_reply('finished public body'),entry='reply-one',seq=2,updated=3)
        self.assertEqual(self.runs()[0]['status'],'done')
        self.assertEqual(self.messages()[-1]['text'],'finished public body')

    def test_office_command_exact_nonce_and_fingerprint_complete_once(self):
        command=str(uuid.uuid4());text='Office public test'
        self.commands.create({'request_id':command,'agent_id':AGENT,'text':text})
        self.commands.db.execute("UPDATE commands SET state='sending' WHERE id=?",(command,));self.commands.db.commit()
        self.feed(body_prompt('[OFFICE:'+command+']\n'+text,nonce=command))
        row=self.commands.db.execute('SELECT * FROM commands WHERE id=?',(command,)).fetchone()
        self.assertEqual(row['state'],'running');self.assertEqual(row['native_message_id'],command)
        self.feed(body_reply(),entry='reply-one',seq=2);self.settlement(nonce=command)
        row=self.commands.db.execute('SELECT * FROM commands WHERE id=?',(command,)).fetchone()
        self.assertEqual(row['state'],'completed');self.assertEqual(len(self.messages()),2)
        self.assertEqual(self.messages()[0]['text'],text)
        self.assertTrue(self.sync.describe(AGENT)['execution_verified'])

    def test_marker_without_matching_native_nonce_cannot_claim_command(self):
        command=str(uuid.uuid4());self.commands.create({'request_id':command,'agent_id':AGENT,'text':'same'})
        self.commands.db.execute("UPDATE commands SET state='sending' WHERE id=?",(command,));self.commands.db.commit()
        self.feed(body_prompt('[OFFICE:'+command+']\nsame',nonce='wrong'))
        self.assertIsNone(self.runs()[0]['command_id'])
        self.assertEqual(self.commands.db.execute('SELECT state FROM commands WHERE id=?',(command,)).fetchone()[0],'sending')

    def test_mismatched_nonce_does_not_complete(self):
        self.feed(body_prompt());self.feed(body_reply(),entry='reply-one',seq=2)
        self.settlement(nonce='different');self.assertEqual(self.runs()[0]['status'],'working')

    def test_replay_and_revised_text_upsert_one_bubble(self):
        self.feed(body_prompt());self.feed(body_reply(),entry='reply-one',seq=2)
        self.feed(body_reply(),entry='reply-one',seq=2)
        self.feed(body_reply('revised'),entry='reply-one',seq=2,updated=3)
        self.assertEqual(len(self.messages()),2);self.assertEqual(self.messages()[-1]['text'],'revised')
        self.feed(body_reply('old'),entry='reply-one',seq=2,updated=2)
        self.assertEqual(self.messages()[-1]['text'],'revised')

    def test_update_after_terminal_retains_final_phase(self):
        self.feed(body_prompt());self.feed(body_reply(),entry='reply-one',seq=2);self.settlement()
        self.feed(body_reply('revised final'),entry='reply-one',seq=2,updated=3)
        self.assertEqual(self.messages()[-1]['phase'],'final');self.assertEqual(self.runs()[0]['status'],'done')

    def test_other_bot_or_session_never_projects_or_checkpoints(self):
        before=self.sync._member(AGENT)['cursor']
        for key,value in [('botId','other'),('sessionId','other')]:
            event=self.event(body_prompt());event['data'][key]=value
            with self.assertRaises(SyncError):self.sync.process_event(self.member,event)
        self.assertEqual(self.messages(),[]);self.assertEqual(self.sync._member(AGENT)['cursor'],before)

    def test_cursor_id_scope_and_uint64_are_checked(self):
        token=cursor_token(BOT,'',1,'9007199254740993')
        self.assertEqual(token_parts(token,BOT,''),(1,9007199254740993))
        with self.assertRaises(SyncError):token_parts(token,BOT,'other')
        event=self.event(body_prompt());event['data']['updatedSeq']='2'
        with self.assertRaises(SyncError):self.sync.process_event(self.member,event)

    def test_missing_body_persists_gap_and_blocks_success(self):
        self.feed(body_prompt());self.feed(None,entry='missing',seq=2,kind='entry.unavailable')
        self.feed(body_reply(),entry='reply-one',seq=3);self.settlement()
        self.assertEqual(self.runs()[0]['status'],'working')
        self.assertEqual(self.runs()[0]['warning'],'grok_sdk_transcript_gap')
        self.assertFalse(self.sync.describe(AGENT)['history_complete'])

    def test_gap_recovered_by_exact_history_body_can_complete(self):
        self.feed(body_prompt());self.feed(None,entry='missing',seq=2,kind='entry.unavailable')
        self.feed(body_reply(),entry='reply-one',seq=3);self.settlement()
        self.feed({'kind':'internal-no-public-text'},entry='missing',seq=2)
        self.assertEqual(self.runs()[0]['status'],'done')
        self.assertTrue(self.sync.describe(AGENT)['history_complete'])
        self.assertEqual(token_parts(self.sync._member(AGENT)['cursor'],BOT,''),(1,3))

    def test_dispatch_waits_until_observer_and_native_status_are_ready(self):
        self.assertTrue(self.sync.busy(AGENT))
        self.sync.process_event(self.member,{'type':'connected','data':{}})
        self.assertTrue(self.sync.busy(AGENT))
        self.state(False);self.assertFalse(self.sync.busy(AGENT))

    def test_native_awaiting_blocks_new_dispatch_and_reports_waiting(self):
        self.sync.process_event(self.member,{'type':'agent.state','data':{'botId':BOT,'sessionId':'','live':[
            {'agentId':BOT,'sessionId':'','isRunning':False,'awaiting':{'reason':'approval','sinceMs':'10','tabId':'tab'}}]}})
        self.assertTrue(self.sync.busy(AGENT));self.assertEqual(self.status[-1][0][1]['status'],'waiting_user')

    def test_delivery_contains_all_public_replies_in_native_order(self):
        self.feed(body_prompt());self.feed(body_reply('First'),entry='reply-one',seq=2)
        self.feed(body_reply('Second'),entry='reply-two',seq=3)
        self.feed(body_reply('First revised'),entry='reply-one',seq=2,updated=4);self.settlement()
        self.assertEqual(self.runs()[0]['delivery'],'First revised\n\nSecond')
        self.assertEqual(self.h.db.execute('SELECT delivery FROM runs').fetchone()[0],'First revised\n\nSecond')

    def test_registration_tail_baseline_precedes_immediate_app_prompt(self):
        self.sync.close()
        self.h.db.execute('DELETE FROM grok_sdk_members')
        self.h.db.execute('CREATE TABLE grok_sdk_connections (agent_id TEXT,settings TEXT,membership_epoch TEXT,baseline TEXT,state TEXT)')
        settings=self.config['command_adapters']['grok_sdk']['sessions'][SID]
        baseline={'cursor':cursor_token(BOT,'',1,10),'native_generation':1,'floor_seq':'10','captured_at':'before-entry'}
        self.h.db.execute('INSERT INTO grok_sdk_connections VALUES (?,?,?,?,?)',(AGENT,json.dumps(settings),None,json.dumps(baseline),'connecting'));self.h.db.commit()
        self.sync=GrokSdkSync(self.bridge);self.member=self.sync._member(AGENT)
        self.assertEqual(self.member['floor_seq'],'10')
        self.feed(body_prompt('sent immediately after joining'),entry='new',seq=11)
        self.assertEqual(self.messages()[0]['text'],'sent immediately after joining')

    def test_unknown_private_entry_does_not_leak(self):
        self.feed({'kind':'model','reasoning':'never display me','content':'private'},entry='private',seq=1)
        self.assertEqual(self.messages(),[])
        row=self.h.db.execute('SELECT text,kind FROM grok_sdk_entries').fetchone()
        self.assertIsNone(row['text']);self.assertEqual(row['kind'],'private')

    def test_secret_redaction_is_applied_before_storage(self):
        self.feed(body_prompt('public fixture-secret api_key=fixture-private-value'))
        text=self.messages()[0]['text'];self.assertNotIn('fixture-secret',text);self.assertNotIn('fixture-private-value',text)

    def test_logout_and_account_switch_fence_inflight_events(self):
        self.adapter.client.account='b'*64
        self.assertFalse(self.feed(body_prompt()));self.assertEqual(self.messages(),[])
        self.adapter.client.account=ACCOUNT
        self.config['agents']=[];self.sync.sync_members()
        self.assertFalse(self.feed(body_prompt()));self.assertEqual(self.messages(),[])

    def test_rejoin_rotates_membership_and_discards_old_callback(self):
        old=copy.deepcopy(self.member);self.config['agents']=[];self.sync.sync_members()
        self.config['agents']=[self.definition];self.sync.sync_members()
        self.assertNotEqual(old['generation'],self.sync._member(AGENT)['generation'])
        self.assertFalse(self.sync.process_event(old,self.event(body_prompt())))

    def test_bootstrap_starts_after_existing_history(self):
        self.adapter.client.pages[None]={'generation':1,'entries':[{'seq':'10','updatedSeq':'12','entryId':'old'}]}
        self.sync.bootstrap(self.member,self.adapter.client)
        self.feed(body_prompt('old private history'),entry='old',seq=10,updated=13)
        self.feed(body_prompt('new chosen turn'),entry='new',seq=11,updated=14)
        self.assertEqual([m['text'] for m in self.messages()],['new chosen turn'])

    def test_restart_reuses_checkpoint_and_deduplicates(self):
        self.feed(body_prompt());self.feed(body_reply(),entry='reply-one',seq=2)
        before=self.sync._member(AGENT)['cursor'];self.sync.close()
        self.sync=GrokSdkSync(self.bridge);self.member=self.sync._member(AGENT)
        self.assertEqual(self.member['cursor'],before)
        self.feed(body_reply(),entry='reply-one',seq=2);self.assertEqual(len(self.messages()),2)

    def test_same_task_with_multiple_turns_is_not_false_reply_pairing(self):
        self.feed(body_prompt(task='task-one',batch='turn-one'))
        self.feed(body_prompt('second',nonce='nonce-two',task='task-one',batch='turn-two'),entry='prompt-two',seq=2)
        self.feed(body_reply(task='task-one',batch=None),entry='reply-one',seq=3)
        self.assertEqual(len(self.runs()),3);self.settlement(turn='turn-one')
        self.assertEqual(self.runs()[0]['status'],'working')

    def test_unpaired_reply_is_resolved_only_by_exact_settled_turn(self):
        self.feed(body_prompt(batch=None));self.feed(body_reply(batch='native-turn'),entry='reply-one',seq=2)
        self.assertEqual(len(self.runs()),2)
        self.settlement(turn='native-turn');self.assertEqual(len(self.runs()),1)
        self.assertEqual(self.runs()[0]['status'],'done');self.assertEqual(len(self.messages()),2)

    def test_user_form_blocks_success_without_approving_it(self):
        self.feed(body_prompt())
        form=body_reply();form['message']={'type':'user-form','fields':[{'private':'never render'}]}
        self.feed(form,entry='form',seq=2);self.settlement()
        self.assertEqual(self.runs()[0]['status'],'working')
        self.assertEqual(self.runs()[0]['warning'],'grok_sdk_user_action_required')
        self.assertNotIn('never render',json.dumps(self.messages()))

    def test_native_error_and_cancel_are_not_success(self):
        for outcome,expected in [('error','error'),('cancelled','cancelled')]:
            with self.subTest(outcome=outcome):
                self.feed(body_prompt(nonce=outcome,batch=outcome),entry=outcome,seq=len(self.messages())+1)
                self.settlement(nonce=outcome,turn=outcome,outcome=outcome)
                self.assertEqual(self.runs()[-1]['status'],expected)

    def test_deleted_public_entry_removed_from_history_delivery(self):
        self.feed(body_prompt());self.feed(body_reply(),entry='reply-one',seq=2);self.settlement()
        self.feed(None,entry='reply-one',seq=2,updated=3,kind='message.deleted')
        self.assertEqual(len(self.messages()),1)
        self.assertEqual(self.h.db.execute('SELECT delivery FROM runs').fetchone()[0],'')

    def test_resync_reloads_selected_history_without_resending(self):
        self.adapter.client.pages[None]={'generation':1,'entries':[
            {'seq':'1','updatedSeq':'1','entryId':'prompt-one','body':body_prompt()},
            {'seq':'2','updatedSeq':'2','entryId':'reply-one','body':body_reply()}]}
        self.sync.resync(self.member,self.adapter.client)
        self.assertEqual(len(self.messages()),2)
        self.assertEqual(token_parts(self.sync._member(AGENT)['cursor'],BOT,''),(1,2))
        self.assertTrue(all(c[1:3]==(BOT,'') for c in self.adapter.client.calls))

    def test_projection_failure_does_not_advance_cursor(self):
        before=self.sync._member(AGENT)['cursor'];original=self.h.record
        def fail(*args,**kwargs):raise sqlite3.OperationalError('fixture')
        import sqlite3
        self.h.record=fail
        with self.assertRaises(sqlite3.OperationalError):self.feed(body_prompt())
        self.assertEqual(self.sync._member(AGENT)['cursor'],before)
        self.h.record=original;self.feed(body_prompt());self.assertEqual(len(self.messages()),1)


if __name__=='__main__':unittest.main()
