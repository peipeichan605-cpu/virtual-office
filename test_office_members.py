"""Isolated lifecycle/seat integration tests; no native model or user membership."""
import json
from pathlib import Path
import sqlite3
import threading
import time
import unittest
import uuid
from unittest.mock import patch
from agent_inbox import InboxError
from configuration import load_config,save_config
from dashboard import Bridge
from event_ingest import EventConflict
from office_members import MemberError,OFFICE_CAPACITIES
import test_invitations


class MemberTests(unittest.TestCase):
    setUp=test_invitations.InvitationTests.setUp
    request=test_invitations.InvitationTests.request
    invite=test_invitations.InvitationTests.invite
    claim_headers=test_invitations.InvitationTests.claim_headers

    def tearDown(self):
        self.bridge.stop.set()
        self.bridge.commands.close()
        test_invitations.InvitationTests.tearDown(self)

    def member(self,name='Fixture one',index=1):
        invitation=self.invite(name=name,provider='generic',request_id='member-fixture-%02d'%index)
        _,joined=self.request('/api/agents/join',{'claim_id':('%x'%index)*32},key=False,headers=self.claim_headers(invitation))
        return invitation,joined['agent_id'],joined

    def command(self,agent):
        self.bridge.commands.enabled=True
        cid=str(uuid.uuid4());self.bridge.commands.create({'agent_id':agent,'request_id':cid,'text':'Bounded fixture instruction'})
        return cid

    def test_owner_key_same_origin_and_selected_identity_required(self):
        _,agent,_=self.member()
        for path,data in [('/api/office/seating',{'agent_id':agent,'desk_id':'gf-main-003'}),('/api/office/agents/logout',{'agent_id':agent})]:
            self.assertEqual(self.request(path,data,key=False)[0],403)
            self.assertEqual(self.request(path,data,headers={'Origin':'https://untrusted.example'})[0],403)
        self.assertEqual(self.request('/api/office/seating',key=False)[0],403)
        self.assertEqual(self.request('/api/office/seating',{'agent_id':'generic:other','desk_id':'gf-main-003'})[0],404)

    def test_initial_seats_manual_move_restart_and_occupied_desk_are_durable(self):
        _,one,_=self.member();_,two,_=self.member('Fixture two',2)
        code,before=self.request('/api/office/seating');self.assertEqual(code,200)
        self.assertEqual(before['placements'][one],'gf-main-001');self.assertEqual(before['placements'][two],'gf-main-002')
        self.assertEqual(self.request('/api/office/seating',{'agent_id':two,'desk_id':'gf-main-001'})[1]['error'],'desk_occupied')
        _,moved=self.request('/api/office/seating',{'agent_id':one,'desk_id':'gf-office-3-002'})
        self.assertGreater(moved['revision'],before['revision']);self.assertEqual(moved['placements'][two],'gf-main-002')
        _,again=self.request('/api/office/seating',{'agent_id':one,'desk_id':'gf-office-3-002'})
        self.assertEqual(again['revision'],moved['revision'])
        self.assertEqual(self.request('/api/office/seating',{'agent_id':two,'desk_id':'gf-office-3-002'})[0],409)
        restored=Bridge(load_config(self.path),dry_run=True)
        try:
            self.assertEqual(restored.members.snapshot(),{k:moved[k] for k in ('placements','revision')})
            snapshot=restored.work_snapshot()
            self.assertEqual({a['id']:a['ground_desk_id'] for a in snapshot['agents']},moved['placements'])
            self.assertEqual(snapshot['seating_revision'],moved['revision'])
        finally:restored.commands.close();restored.history.close()

    def test_invalid_and_out_of_plan_desks_are_rejected(self):
        _,agent,_=self.member()
        for desk in ('gf-main-000','gf-main-401','gf-main-1','gf-office-0-001','gf-office-6-001','gf-office-1-009','gf-office-2-007','gf-office-3-005','gf-office-4-007','gf-office-5-009','gf-office-1-000','gf-office-1-01','gf-office-01-001','gf-office-1-010','gf-main-002 ',None,2,'../../private'):
            self.assertEqual(self.request('/api/office/seating',{'agent_id':agent,'desk_id':desk})[0],400,repr(desk))
        for desk in ('gf-main-400','gf-office-5-004'):
            self.assertEqual(self.request('/api/office/seating',{'agent_id':agent,'desk_id':desk})[0],200)

    def test_every_room_accepts_only_its_rendered_capacity(self):
        _,agent,_=self.member()
        for room,capacity in OFFICE_CAPACITIES.items():
            desk='gf-office-%d-%03d'%(room,capacity)
            code,moved=self.request('/api/office/seating',{'agent_id':agent,'desk_id':desk})
            self.assertEqual(code,200);self.assertEqual(moved['placements'][agent],desk)
            for invalid in ('gf-office-%d-%03d'%(room,capacity+1),'gf-office-%d-000'%room):
                self.assertEqual(self.request('/api/office/seating',{'agent_id':agent,'desk_id':invalid})[0],400)
                self.assertEqual(self.bridge.members.snapshot(),{k:moved[k] for k in ('placements','revision')})

    def test_new_sixth_and_eighth_seats_survive_restart_without_reseating_others(self):
        _,one,_=self.member();_,two,_=self.member('Fixture two',2);_,other,_=self.member('Unmoved fixture',3)
        before=self.bridge.members.snapshot()
        self.bridge.history.record(one,'generic','Fixture one','saved-history','done',delivery='Keep delivery')
        for agent,desk in ((one,'gf-office-1-008'),(two,'gf-office-2-006')):
            code,moved=self.request('/api/office/seating',{'agent_id':agent,'desk_id':desk})
            self.assertEqual(code,200);self.assertEqual(moved['placements'][agent],desk)
        self.assertEqual(self.request('/api/office/seating',{'agent_id':other,'desk_id':'gf-office-1-008'})[0],409)
        expected=self.bridge.members.snapshot()
        self.assertEqual(expected['placements'][other],before['placements'][other])
        self.assertEqual(expected['revision'],before['revision']+2)
        restored=Bridge(load_config(self.path),dry_run=True)
        try:
            self.assertEqual(restored.members.snapshot(),expected)
            self.assertEqual({a['id']:a['ground_desk_id'] for a in restored.work_snapshot()['agents']},expected['placements'])
            self.assertEqual(restored.history.list({one})[0]['delivery_preview'],'Keep delivery')
        finally:restored.commands.close();restored.history.close()

    def test_single_ceo_seat_is_exclusive_and_survives_restart(self):
        _,ceo,_=self.member('CEO fixture');_,other,_=self.member('Unmoved fixture',2)
        before=self.bridge.members.snapshot()
        code,moved=self.request('/api/office/seating',{'agent_id':ceo,'desk_id':'gf-ceo-001'})
        self.assertEqual(code,200);self.assertEqual(moved['placements'][ceo],'gf-ceo-001')
        self.assertEqual(moved['placements'][other],before['placements'][other])
        self.assertEqual(self.request('/api/office/seating',{'agent_id':other,'desk_id':'gf-ceo-001'})[0],409)
        for invalid in ('gf-ceo-000','gf-ceo-002','gf-ceo-1','gf-ceo-01','gf-ceo-0001','gf-ceo-001 ','gf-ceo-1-001'):
            self.assertEqual(self.request('/api/office/seating',{'agent_id':ceo,'desk_id':invalid})[0],400,invalid)
        expected={k:moved[k] for k in ('placements','revision')}
        self.assertEqual(self.bridge.members.snapshot(),expected)
        restored=Bridge(load_config(self.path),dry_run=True)
        try:
            self.assertEqual(restored.members.snapshot(),expected)
            self.assertEqual({a['id']:a['ground_desk_id'] for a in restored.work_snapshot()['agents']},expected['placements'])
        finally:restored.commands.close();restored.history.close()

    def test_two_tabs_cannot_claim_the_same_empty_desk(self):
        _,one,_=self.member();_,two,_=self.member('Fixture two',2)
        barrier=threading.Barrier(2);codes=[]
        def move(agent):
            barrier.wait();codes.append(self.request('/api/office/seating',{'agent_id':agent,'desk_id':'gf-office-5-004'})[0])
        threads=[threading.Thread(target=move,args=(agent,)) for agent in (one,two)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(3)
        self.assertEqual(sorted(codes),[200,409])
        self.assertEqual(list(self.bridge.members.snapshot()['placements'].values()).count('gf-office-5-004'),1)

    def test_logout_revokes_old_invite_reporter_and_inbox_preserves_history(self):
        invite,agent,joined=self.member();definition=self.bridge.definitions[agent]
        credential=json.loads(Path(joined['credential_path']).read_text())
        inbox_path=Path(joined['conversation']['credential_path']);inbox=json.loads(inbox_path.read_text())
        start={'source_id':definition['source_id'],'event_id':'fixture-start','run_id':'fixture-run','type':'turn_started','text':'Fixture task'}
        self.bridge.accept_agent_event(definition,start)
        self.bridge.accept_agent_event(definition,{**start,'event_id':'fixture-done','type':'turn_completed','text':'Retained delivery'})
        before=len(self.bridge.history.messages({agent},agent));seat=self.bridge.members.snapshot()['placements'][agent]
        _,result=self.request('/api/office/agents/logout',{'agent_id':agent})
        self.assertTrue(result['logged_out']);self.assertFalse(result['native_session_stopped']);self.assertTrue(result['history_preserved'])
        self.assertNotIn(agent,self.bridge.definitions);self.assertNotIn(agent,self.bridge.agents)
        self.assertNotIn(definition['name'],self.bridge.sources);self.assertNotIn(definition['name'],self.bridge.allowed_names)
        self.assertNotIn(agent,self.bridge.members.snapshot()['placements']);self.assertFalse(inbox_path.exists())
        self.assertFalse(self.bridge.inbox.authenticated(agent,inbox['token']))
        self.assertEqual(len(self.bridge.history.messages({agent},agent)),before)
        self.assertEqual(self.bridge.history.list({agent})[0]['delivery_preview'],'Retained delivery')
        self.assertEqual(self.bridge.invitations.get(invite['id'])['state'],'revoked')
        self.assertEqual(self.request('/api/agents/join',{'claim_id':'1'*32},key=False,headers=self.claim_headers(invite))[0],410)
        event_headers={'Authorization':'Bearer '+credential['token'],'X-Agent-Id':agent}
        self.assertEqual(self.request('/api/agents/events',start,key=False,headers=event_headers)[0],403)
        self.assertEqual(self.request('/api/agents/connect',{'adapter':'codex_app'},key=False,headers=event_headers)[0],403)
        with self.assertRaises(EventConflict):self.bridge.accept_agent_event(definition,{**start,'event_id':'late-event'})
        self.bridge.report(definition['name'],{'status':'working','step':'Late observer'},'generic')
        self.assertNotIn(definition['name'],self.bridge.sources)
        self.assertTrue(self.request('/api/office/agents/logout',{'agent_id':agent})[1]['duplicate'])
        _,fresh,_=self.member('Fixture one',2)
        self.assertNotEqual(fresh,agent);self.assertEqual(self.bridge.members.snapshot()['placements'][fresh],seat)
        self.bridge.report(definition['name'],{'status':'working','step':'Stale old observer'},'generic',agent_id=agent)
        self.assertNotEqual(self.bridge.sources[definition['name']]['step'],'Stale old observer')

    def test_logout_cancels_only_never_delivered_pending_commands(self):
        _,agent,_=self.member();cid=self.command(agent)
        _,result=self.request('/api/office/agents/logout',{'agent_id':agent})
        self.assertEqual(result['pending_commands_cancelled'],1)
        row=self.bridge.commands.db.execute('SELECT state,payload,detail_code FROM commands WHERE id=?',(cid,)).fetchone()
        self.assertEqual(tuple(row),('cancelled',None,'agent_logged_out'))
        self.assertEqual(self.bridge.history.db.execute('SELECT source_kind FROM runs WHERE agent_id=? AND run_id=?',(agent,cid)).fetchone()[0],'office_logout')
        self.assertIn('尚未送出',self.bridge.history.list({agent})[0]['delivery_preview'])

    def test_logout_never_reassigns_or_cancels_exposed_or_uncertain_work(self):
        _,agent,_=self.member();cid=self.command(agent)
        for state in ('sending','queued','running','waiting_user','delivery_unknown'):
            with self.bridge.commands.lock,self.bridge.commands.db:self.bridge.commands.db.execute('UPDATE commands SET state=? WHERE id=?',(state,cid))
            self.assertEqual(self.request('/api/office/agents/logout',{'agent_id':agent})[1]['error'],'agent_has_active_commands')
            self.assertIn(agent,self.bridge.definitions)
            self.assertEqual(self.bridge.commands.db.execute('SELECT state FROM commands WHERE id=?',(cid,)).fetchone()[0],state)

    def test_an_already_authenticated_waiter_loses_access_on_logout(self):
        _,agent,joined=self.member();self.bridge.commands.enabled=True
        token=json.loads(Path(joined['conversation']['credential_path']).read_text())['token'];result=[]
        def receive():
            try:self.bridge.inbox.receive(agent,{'action':'receive','receiver_id':str(uuid.uuid4()),'request_id':str(uuid.uuid4()),'wait_seconds':10},authenticated_token=token)
            except InboxError as error:result.append((error.status,error.code))
        thread=threading.Thread(target=receive);thread.start()
        until=time.monotonic()+2
        while not self.bridge.inbox.listening(agent) and time.monotonic()<until:time.sleep(.01)
        self.assertTrue(self.bridge.inbox.listening(agent))
        self.assertEqual(self.request('/api/office/agents/logout',{'agent_id':agent})[0],200)
        thread.join(2);self.assertFalse(thread.is_alive());self.assertEqual(result,[(403,'agent_not_selected')])

    def test_startup_finishes_cleanup_after_atomic_config_retirement(self):
        invitation,agent,joined=self.member();cid=self.command(agent)
        config=load_config(self.path);config['agents']=[];config['command_bindings']={}
        config['logged_out_agent_ids']=[agent];config['revoked_invitation_ids']=[invitation['id']];save_config(self.path,config)
        restored=Bridge(load_config(self.path),dry_run=True)
        try:
            self.assertEqual(restored.members.snapshot()['placements'],{})
            self.assertEqual(restored.invitations.get(invitation['id'])['state'],'revoked')
            self.assertEqual(restored.commands.db.execute('SELECT state,payload FROM commands WHERE id=?',(cid,)).fetchone()[:],('cancelled',None))
            self.assertFalse(Path(joined['conversation']['credential_path']).exists())
            with sqlite3.connect(self.root/'commands.sqlite3') as independent:
                self.assertEqual(independent.execute('SELECT count(*) FROM inbox_credentials WHERE agent_id=?',(agent,)).fetchone()[0],0)
        finally:restored.commands.close();restored.history.close()

    def test_failed_config_save_keeps_membership_seat_and_pending_message(self):
        _,agent,_=self.member();cid=self.command(agent);before=self.bridge.members.snapshot()
        with patch('office_members.save_config',side_effect=OSError('fixture failure')):
            self.assertEqual(self.request('/api/office/agents/logout',{'agent_id':agent})[0],503)
        self.assertIn(agent,self.bridge.definitions);self.assertEqual(before,self.bridge.members.snapshot())
        self.assertEqual(self.bridge.commands.db.execute('SELECT state FROM commands WHERE id=?',(cid,)).fetchone()[0],'pending')

    def test_partial_cleanup_cannot_resurrect_a_member_and_repeat_logout_finishes(self):
        invitation,agent,joined=self.member();cid=self.command(agent)
        with patch.object(self.bridge.members,'_retire_credentials',side_effect=OSError('fixture cleanup failure')):
            self.assertEqual(self.request('/api/office/agents/logout',{'agent_id':agent})[0],503)
        self.assertNotIn(agent,self.bridge.definitions)
        self.assertEqual(load_config(self.path)['agents'],[])
        self.assertFalse(self.bridge.commands.allowed(agent))
        self.assertEqual(self.request('/api/agents/join',{'claim_id':'1'*32},key=False,headers=self.claim_headers(invitation))[0],410)
        _,result=self.request('/api/office/agents/logout',{'agent_id':agent})
        self.assertTrue(result['logged_out']);self.assertTrue(result['duplicate'])
        self.assertEqual(self.bridge.commands.db.execute('SELECT state FROM commands WHERE id=?',(cid,)).fetchone()[0],'cancelled')
        self.assertFalse(Path(joined['conversation']['credential_path']).exists())


if __name__=='__main__':unittest.main()
