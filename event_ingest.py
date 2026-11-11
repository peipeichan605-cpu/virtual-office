"""Provider-neutral public event ingestion, with durable receipts and run fencing."""
import hashlib
import json
import re
from contextlib import nullcontext
from history import timestamp
from workspaces import workspace_path

TYPES = {'turn_started', 'message', 'turn_completed', 'turn_failed', 'turn_cancelled', 'heartbeat'}

class EventConflict(ValueError):
    pass

def validate_event(event, source_id):
    if not isinstance(event, dict) or set(event) - {'event_id','run_id','type','text','role','phase','source_id','at','workspace_path'}:
        raise ValueError('Unsupported event fields')
    if event.get('source_id') != source_id:
        raise ValueError('Source identity mismatch')
    for key in ('event_id','run_id'):
        if not isinstance(event.get(key), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}' if key=='run_id' else r'[A-Za-z0-9_.:-]{1,160}',event[key]):
            raise ValueError('Invalid event identity')
    if event.get('type') not in TYPES:
        raise ValueError('Unsupported event type')
    if 'workspace_path' in event:
        if event['type']!='turn_started':raise ValueError('Workspace is only accepted at turn start')
        workspace_path(event['workspace_path'])
    if 'text' in event and (not isinstance(event['text'],str) or len(event['text']) > 24000):
        raise ValueError('Text must be at most 24000 characters')
    if event['type'] == 'message' and (event.get('role'),event.get('phase')) not in {
        ('user','prompt'),('assistant','commentary'),('assistant','final')}:
        raise ValueError('Only public conversation messages are accepted')
    if event.get('at') is not None and (not isinstance(event['at'],str) or len(event['at'])>50):
        raise ValueError('Invalid timestamp')
    return event

def ingest(store, agent, event, on_start=None, display_text=None, transaction=True):
    validate_event(event, agent['source_id'])
    agent_id, run_id = agent['id'], event['run_id']
    # Fingerprints contain no plaintext. Stored messages are redacted before commit.
    fingerprint = hashlib.sha256(json.dumps(event,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    at = timestamp(event.get('at'))
    kind, text = event['type'], event.get('text','') if display_text is None else display_text
    with store.lock, store.db if transaction else nullcontext():
        prior = store.db.execute('SELECT fingerprint FROM event_receipts WHERE agent_id=? AND event_id=?',
                                (agent_id,event['event_id'])).fetchone()
        if prior:
            if prior[0] != fingerprint:
                raise EventConflict('Event ID reused with different content')
            return {'accepted':True,'duplicate':True}
        live = store.db.execute('SELECT * FROM live_runs WHERE agent_id=?',(agent_id,)).fetchone()
        old = store.db.execute('SELECT status FROM runs WHERE agent_id=? AND run_id=?',(agent_id,run_id)).fetchone()
        if kind == 'turn_started':
            if old:
                raise EventConflict('Run already exists; use a new run ID')
            if live and live['status']=='working':
                store.record(agent_id,agent['provider'],agent['name'],live['run_id'],'unknown',at=at,commit=False)
            status = 'working'
            if on_start:on_start(event)
        else:
            if not live or live['run_id'] != run_id:
                raise EventConflict('Run is not current; a start event is required')
            status = live['status']
            if kind in {'turn_completed','turn_failed','turn_cancelled'}:
                if status in {'done','error','cancelled'}:
                    raise EventConflict('Run is already terminal')
                status = {'turn_completed':'done','turn_failed':'error','turn_cancelled':'cancelled'}[kind]
            elif kind=='heartbeat' and status!='working':
                raise EventConflict('Heartbeat cannot restart a terminal run')
        prompt = text if kind=='turn_started' or (kind=='message' and event['role']=='user') else None
        delivery = text if kind=='turn_completed' or (kind=='message' and event['phase']=='final') else None
        store.record(agent_id,agent['provider'],agent['name'],run_id,status,at=at,prompt=prompt,delivery=delivery,commit=False)
        if text and kind in {'turn_started','turn_completed','message'}:
            role,phase = ('user','prompt') if kind=='turn_started' else ('assistant','final') if kind=='turn_completed' else (event['role'],event['phase'])
            store.message(agent_id,run_id,role,phase,text,at=at,event_id=event['event_id'],commit=False)
        store.db.execute('INSERT INTO live_runs VALUES (?,?,?,?) ON CONFLICT(agent_id) DO UPDATE SET run_id=excluded.run_id,status=excluded.status,updated_at=excluded.updated_at',
                         (agent_id,run_id,status,timestamp()))
        store.db.execute('INSERT INTO event_receipts VALUES (?,?,?,?)',(agent_id,event['event_id'],fingerprint,run_id))
    return {'accepted':True,'duplicate':False,'status':status,'run_id':run_id,'at':at}
