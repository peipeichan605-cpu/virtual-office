"""Owner-controlled Office membership and durable, provider-neutral G/F seats."""
import hashlib
from pathlib import Path
import re
import uuid
from configuration import load_config, save_config
from history import timestamp


class MemberError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


# Keep this physical floor-plan contract aligned with overview.OFFICE_CAPACITIES.
OFFICE_CAPACITIES={1:8,2:6,3:4,4:6,5:8}


def valid_desk(value):
    if not isinstance(value, str):return False
    if value=='gf-ceo-001':return True
    main=re.fullmatch(r'gf-main-([0-9]{3})',value)
    if main:return 1<=int(main[1])<=400
    office=re.fullmatch(r'gf-office-([1-5])-([0-9]{3})',value)
    return bool(office and 1<=int(office[2])<=OFFICE_CAPACITIES[int(office[1])])


class OfficeMembers:
    def __init__(self, bridge):
        self.bridge=bridge
        with bridge.history.lock,bridge.history.db:
            bridge.history.db.execute('CREATE TABLE IF NOT EXISTS office_seats (agent_id TEXT PRIMARY KEY,desk_id TEXT UNIQUE NOT NULL)')
            bridge.history.db.execute('CREATE TABLE IF NOT EXISTS office_seating_meta (id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL)')
            bridge.history.db.execute('INSERT OR IGNORE INTO office_seating_meta VALUES (1,0)')
            bridge.history.db.execute('CREATE TABLE IF NOT EXISTS office_membership_events (event_id TEXT PRIMARY KEY,agent_id TEXT NOT NULL,kind TEXT NOT NULL,at TEXT NOT NULL)')

    def _changed(self):
        self.bridge.history.db.execute('UPDATE office_seating_meta SET revision=revision+1 WHERE id=1')

    def snapshot(self):
        with self.bridge.history.lock:
            selected=self.bridge.definitions
            rows=self.bridge.history.db.execute('SELECT agent_id,desk_id FROM office_seats ORDER BY agent_id').fetchall()
            return {'placements':{r['agent_id']:r['desk_id'] for r in rows if r['agent_id'] in selected},
                'revision':self.bridge.history.db.execute('SELECT revision FROM office_seating_meta WHERE id=1').fetchone()[0]}

    def sync(self, config):
        """Preserve assignments, release departed slots, fill holes for arrivals."""
        b=self.bridge
        with b.commands.lock,b.history.lock,b.history.db:
            selected={a['id'] for a in config['agents']}
            rows=b.history.db.execute('SELECT agent_id,desk_id FROM office_seats').fetchall()
            changed=False
            for row in rows:
                if row['agent_id'] not in selected:
                    b.history.db.execute('DELETE FROM office_seats WHERE agent_id=?',(row['agent_id'],));changed=True
            assigned={r['agent_id']:r['desk_id'] for r in b.history.db.execute('SELECT * FROM office_seats')}
            free=iter('gf-main-%03d'%i for i in range(1,401) if 'gf-main-%03d'%i not in set(assigned.values()))
            for agent in sorted(selected-set(assigned)):
                desk=next(free,None)
                if desk is None:break  # The current floor plan supports 400 main lounge seats.
                b.history.db.execute('INSERT INTO office_seats VALUES (?,?)',(agent,desk));changed=True
            if changed:self._changed()
            # Complete a logout interrupted after the atomic config update.
            retired=set(config.get('logged_out_agent_ids',[]))-selected
            for agent in retired:self._retire_credentials(agent)
            for ident in config.get('revoked_invitation_ids',[]):
                b.history.db.execute("UPDATE invitations SET state='revoked' WHERE id=?",(ident,))
        # This also commits commands.sqlite3 credential revocations, including
        # when a retired agent has no pending messages.
        for agent in retired:self._cancel_pending(agent)

    def assign(self, data):
        if not isinstance(data,dict) or set(data)!={'agent_id','desk_id'} or not isinstance(data['agent_id'],str) or not valid_desk(data['desk_id']):
            raise MemberError('invalid_seating')
        b=self.bridge
        with b.commands.lock,b.history.lock,b.history.db:
            if data['agent_id'] not in b.definitions:raise MemberError('agent_not_selected',404)
            occupant=b.history.db.execute('SELECT agent_id FROM office_seats WHERE desk_id=?',(data['desk_id'],)).fetchone()
            if occupant and occupant['agent_id']!=data['agent_id']:raise MemberError('desk_occupied',409)
            previous=b.history.db.execute('SELECT desk_id FROM office_seats WHERE agent_id=?',(data['agent_id'],)).fetchone()
            if not previous or previous['desk_id']!=data['desk_id']:
                b.history.db.execute('INSERT INTO office_seats VALUES (?,?) ON CONFLICT(agent_id) DO UPDATE SET desk_id=excluded.desk_id',(data['agent_id'],data['desk_id']))
                self._changed()
            return dict(self.snapshot(),agent_id=data['agent_id'],desk_id=data['desk_id'])

    def _retire_credentials(self, agent):
        b=self.bridge
        b.commands.db.execute('DELETE FROM inbox_credentials WHERE agent_id=?',(agent,))
        credential=Path(b.config['data_dir'])/'channels'/hashlib.sha256(agent.encode()).hexdigest()/'credential.json'
        if credential.exists():credential.rename(credential.with_name('credential.logged-out-'+uuid.uuid4().hex+'.json'))

    def _cancel_pending(self, agent):
        b=self.bridge
        with b.commands.lock,b.history.lock:
            rows=b.commands.db.execute("SELECT * FROM commands WHERE agent_id=? AND state='pending' AND native_message_id IS NULL AND run_id IS NULL",(agent,)).fetchall()
            previous=b.history.db.execute('SELECT name FROM runs WHERE agent_id=? ORDER BY updated_at DESC LIMIT 1',(agent,)).fetchone()
            invitation=b.history.db.execute('SELECT name FROM invitations WHERE provider=? AND source_id=? LIMIT 1',tuple(agent.split(':',1))).fetchone()
            name=previous['name'] if previous else invitation['name'] if invitation else agent
            for row in rows:
                b.history.record(agent,agent.split(':',1)[0],name,row['id'],'cancelled',prompt=row['preview'],
                    delivery='Agent 已登出 OFFICE；尚未送出的指示已取消。',source_kind='office_logout')
                b.commands.db.execute("UPDATE commands SET state='cancelled',payload=NULL,detail_code='agent_logged_out',updated_at=? WHERE id=?",(timestamp(),row['id']))
            b.commands.db.commit()
            return len(rows)

    def logout(self, data):
        if not isinstance(data,dict) or set(data)!={'agent_id'} or not isinstance(data['agent_id'],str):raise MemberError('invalid_logout')
        b=self.bridge;agent_id=data['agent_id']
        with b.commands.lock,b.history.lock:
            config=load_config(b.config['config_path'])
            agent=next((a for a in config['agents'] if a['id']==agent_id),None)
            if not agent:
                if agent_id in config.get('logged_out_agent_ids',[]):
                    b.refresh_connections(config)
                    return {'logged_out':True,'agent_id':agent_id,'duplicate':True,'history_preserved':True,'native_session_stopped':False}
                raise MemberError('agent_not_selected',404)
            active=b.commands.db.execute("SELECT 1 FROM commands WHERE agent_id=? AND (state IN ('sending','queued','running','waiting_user','delivery_unknown') OR (state='pending' AND (native_message_id IS NOT NULL OR run_id IS NOT NULL))) LIMIT 1",(agent_id,)).fetchone()
            if active:raise MemberError('agent_has_active_commands',409)
            revoked=[r[0] for r in b.history.db.execute('SELECT id FROM invitations WHERE provider=? AND source_id=?',(agent['provider'],agent['source_id']))]
            if hasattr(b,'get_connection_checks'):
                b.get_connection_checks().route_changed(agent_id)
            config['agents']=[a for a in config['agents'] if a['id']!=agent_id]
            binding=config.get('command_bindings',{}).pop(agent_id,None)
            if binding:
                sessions=config.get('command_adapters',{}).get(binding['adapter'],{}).get('sessions',{})
                if not any(v==binding for v in config.get('command_bindings',{}).values()):sessions.pop(binding['session_id'],None)
            config['disabled_command_agents']=[a for a in config.get('disabled_command_agents',[]) if a!=agent_id]
            config['logged_out_agent_ids']=sorted(set(config.get('logged_out_agent_ids',[]))|{agent_id})
            config['revoked_invitation_ids']=sorted(set(config.get('revoked_invitation_ids',[]))|set(revoked))
            # Atomic on-disk removal fences old invitations across a crash.
            save_config(config['config_path'],config)
            cancelled=b.commands.db.execute("SELECT COUNT(*) FROM commands WHERE agent_id=? AND state='pending' AND native_message_id IS NULL AND run_id IS NULL",(agent_id,)).fetchone()[0]
            b.refresh_connections(config)
            with b.history.db:
                b.history.db.execute('INSERT INTO office_membership_events VALUES (?,?,?,?)',(uuid.uuid4().hex,agent_id,'logged_out',timestamp()))
            b.commands.db.commit()
            return {'logged_out':True,'agent_id':agent_id,'duplicate':False,'history_preserved':True,
                'pending_commands_cancelled':cancelled,'native_session_stopped':False}
