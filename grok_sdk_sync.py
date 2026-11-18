"""Selected Grok Bot session observation, independent of cooperative reporting.

Only explicit public user/message shapes are projected. The native SDK stream
is not a task completion signal: acknowledgement, idle, disconnect and missing
bodies never manufacture a successful delivery. Every checkpoint follows the
durable public projection and is fenced by membership and account identity.
"""
import base64
import hashlib
import hmac
import json
import re
import sqlite3
import threading
import time
import uuid

from history import redact, timestamp


class SyncError(Exception):
    pass


def uint(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise SyncError('grok_sdk_invalid_cursor')
    text = str(value)
    if not re.fullmatch(r'\d{1,20}', text) or int(text) > 18446744073709551615:
        raise SyncError('grok_sdk_invalid_cursor')
    return int(text)


def native_generation(value):
    number = uint(value)
    if number > 4294967295:
        raise SyncError('grok_sdk_invalid_cursor')
    return number


def cursor_token(bot, session, generation, updated):
    value = {'botId': bot, 'sessionId': session, 'generation': native_generation(generation),
             'afterUpdatedSeq': str(uint(updated))}
    return base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).decode().rstrip('=')


def token_parts(token, bot, session):
    if not isinstance(token, str) or len(token) > 2048 or not re.fullmatch(r'[A-Za-z0-9_-]+', token):
        raise SyncError('grok_sdk_invalid_cursor')
    try:
        value = json.loads(base64.urlsafe_b64decode(token + '=' * (-len(token) % 4)))
        if value.get('botId') != bot or value.get('sessionId') != session:
            raise ValueError()
        return native_generation(value['generation']), uint(value['afterUpdatedSeq'])
    except (ValueError, KeyError, TypeError):
        raise SyncError('grok_sdk_invalid_cursor') from None


def ident(value):
    return value if isinstance(value, str) and 0 < len(value) <= 256 and not any(ord(c) < 32 for c in value) else None


def public_entry(body):
    """Desktop public transcript shapes, never recursive arbitrary text search.

    Human shape is the Desktop renderer's public `message` entry. Text sent
    by the Bot uses the documented `send-message` shape. Tool/model entries,
    inter-Agent messages, streaming model text and hidden reasoning are ignored.
    """
    if not isinstance(body, dict):
        return None
    kind = body.get('kind')
    if kind == 'message' and body.get('role') == 'user':
        text = body.get('content')
        flag=body.get('isStreaming')
        if (not isinstance(text, str) or (flag is not None and flag is not False)
                or any(body.get(k) not in (None, '') for k in ('fromAgent', 'toAgent', 'channel'))):
            return None
        return {'kind': 'prompt', 'text': text, 'message_id': ident(body.get('clientNonce')),
                'task_id': ident(body.get('taskId')), 'batch_id': ident(body.get('batchId')),
                'request_id':ident(body.get('requestId')),
                'reply_to': ident(body.get('replyTo')), 'at': timestamp(body.get('timestampMs'))}
    if kind == 'send-message':
        message = body.get('message')
        if not isinstance(message, dict) or message.get('type') not in {'text','user-form'}:
            return None
        if message.get('type')=='text' and not isinstance(message.get('content'),str):
            return None
        # Destination/channel fields belong to peer integrations, not the
        # selected human chat. A future schema must be reviewed explicitly.
        if any(message.get(k) not in (None, '') for k in ('toAgent', 'fromAgent', 'channel')):
            return None
        waiting=message.get('type')=='user-form'
        return {'kind': 'reply', 'text': 'Grok Bot 正等待你在原 App 回覆。' if waiting else message['content'], 'message_id': ident(body.get('id')),
                'task_id': ident(message.get('task_id')), 'batch_id': ident(body.get('batchId')),
                'request_id':ident(body.get('requestId')),
                'reply_to': ident(body.get('replyTo')), 'at': timestamp(body.get('timestampMs')),
                'streaming': body.get('streaming') is True, 'waiting':waiting}
    return None


class GrokSdkSync:
    interval = 3.0
    max_workers = 4
    history_page_size = 100
    history_pages = 10

    def __init__(self, bridge, enabled=True):
        self.bridge, self.enabled = bridge, enabled
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.workers, self.worker_stops, self.retry_at, self.failures = {}, {}, {}, {}
        h = bridge.history
        with bridge.commands.lock, h.lock, h.db:
            h.db.execute('''CREATE TABLE IF NOT EXISTS grok_sdk_members (
                agent_id TEXT PRIMARY KEY, local_session TEXT NOT NULL, bot_id TEXT NOT NULL,
                native_session TEXT NOT NULL, account_fingerprint TEXT NOT NULL,
                generation TEXT NOT NULL, active INTEGER NOT NULL, joined_at TEXT NOT NULL,
                cursor TEXT, native_generation INTEGER, floor_seq TEXT, last_ok REAL,
                error TEXT, running INTEGER, state_at REAL)''')
            if 'waiting' not in {r[1] for r in h.db.execute('PRAGMA table_info(grok_sdk_members)')}:
                h.db.execute('ALTER TABLE grok_sdk_members ADD COLUMN waiting INTEGER NOT NULL DEFAULT 0')
            h.db.execute('''CREATE TABLE IF NOT EXISTS grok_sdk_entries (
                agent_id TEXT NOT NULL, generation TEXT NOT NULL, native_generation INTEGER NOT NULL,
                entry_id TEXT NOT NULL, seq TEXT NOT NULL, updated_seq TEXT NOT NULL,
                kind TEXT NOT NULL, local_run TEXT, public_role TEXT, text TEXT,
                gap INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(agent_id,generation,native_generation,entry_id))''')
            h.db.execute('''CREATE TABLE IF NOT EXISTS grok_sdk_runs (
                agent_id TEXT NOT NULL, generation TEXT NOT NULL, local_run TEXT NOT NULL,
                task_id TEXT, batch_id TEXT, prompt_entry TEXT, message_id TEXT, command_id TEXT,
                status TEXT NOT NULL, started_at TEXT NOT NULL, last_seq TEXT NOT NULL,
                delivery TEXT NOT NULL DEFAULT '', warning TEXT,
                PRIMARY KEY(agent_id,generation,local_run))''')
            columns={r[1] for r in h.db.execute('PRAGMA table_info(grok_sdk_runs)')}
            for name in ('native_outcome','settled_at','native_turn','native_request'):
                if name not in columns:
                    h.db.execute('ALTER TABLE grok_sdk_runs ADD COLUMN '+name+' TEXT')
            if 'reply_ready' not in columns:
                h.db.execute('ALTER TABLE grok_sdk_runs ADD COLUMN reply_ready INTEGER NOT NULL DEFAULT 0')
        self.sync_members()

    def _adapter(self):
        return self.bridge.commands.adapters.get('grok_sdk')

    def selected(self):
        b = self.bridge
        settings = b.config.get('command_adapters', {}).get('grok_sdk', {}).get('sessions', {})
        result = {}
        for definition in b.config.get('agents', []):
            binding = b.commands.binding(definition['id']) or {}
            if definition.get('provider') != 'grok_bot' or binding.get('adapter') != 'grok_sdk':
                continue
            sid = binding.get('session_id')
            item = settings.get(sid)
            if (not isinstance(item, dict) or not ident(item.get('bot_id'))
                    or not isinstance(item.get('native_session_id'), str)
                    or len(item['native_session_id']) > 256
                    or not re.fullmatch(r'[a-f0-9]{64}', item.get('account_fingerprint', ''))):
                continue
            result[definition['id']] = (sid, item['bot_id'], item['native_session_id'], item['account_fingerprint'])
        return result

    def sync_members(self):
        b, h = self.bridge, self.bridge.history
        with b.commands.lock, h.lock, h.db:
            selected = self.selected()
            rows = {r['agent_id']: r for r in h.db.execute('SELECT * FROM grok_sdk_members')}
            for agent, row in rows.items():
                old = tuple(row[k] for k in ('local_session', 'bot_id', 'native_session', 'account_fingerprint'))
                if selected.get(agent) != old:
                    h.db.execute('UPDATE grok_sdk_members SET active=0 WHERE agent_id=?', (agent,))
            for agent, target in selected.items():
                old = rows.get(agent)
                if old and old['active'] and target == tuple(old[k] for k in ('local_session','bot_id','native_session','account_fingerprint')):
                    continue
                baseline=self._registration_baseline(agent,target)
                h.db.execute('INSERT OR REPLACE INTO grok_sdk_members (agent_id,local_session,bot_id,native_session,account_fingerprint,generation,active,joined_at,cursor,native_generation,floor_seq,last_ok,error,running,state_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (agent, *target, uuid.uuid4().hex, 1, timestamp(),
                     baseline.get('cursor'),baseline.get('native_generation'),baseline.get('floor_seq'),None,None,None,None))
            active = {(r['agent_id'], r['generation']) for r in h.db.execute('SELECT * FROM grok_sdk_members WHERE active=1')}
        with self.lock:
            for key, stop in self.worker_stops.items():
                if key not in active:
                    stop.set()

    def _registration_baseline(self,agent,target):
        """Registration captures the tail before the Agent becomes visible."""
        h=self.bridge.history
        tables={r[0] for r in h.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'grok_sdk_connections' not in tables:return {}
        if 'baseline' not in {r[1] for r in h.db.execute('PRAGMA table_info(grok_sdk_connections)')}:return {}
        epoch=None
        if 'office_membership_events' in tables:
            row=h.db.execute('SELECT event_id FROM office_membership_events WHERE agent_id=? ORDER BY rowid DESC LIMIT 1',(agent,)).fetchone()
            epoch=row['event_id'] if row else None
        settings={'bot_id':target[1],'native_session_id':target[2],'account_fingerprint':target[3]}
        rows=h.db.execute("SELECT settings,membership_epoch,baseline FROM grok_sdk_connections WHERE agent_id=? AND state IN ('connecting','completed','unknown') ORDER BY rowid DESC",(agent,)).fetchall()
        for row in rows:
            try:
                if row['membership_epoch']!=epoch or json.loads(row['settings'])!=settings:continue
                value=json.loads(row['baseline'])
                ng,updated=token_parts(value['cursor'],target[1],target[2])
                if native_generation(value['native_generation'])!=ng:continue
                floor=uint(value['floor_seq'])
                if floor>updated:continue
                return {'cursor':value['cursor'],'native_generation':ng,'floor_seq':str(floor)}
            except (ValueError,KeyError,TypeError,SyncError):continue
        return {}

    def _member(self, agent):
        row = self.bridge.history.db.execute('SELECT * FROM grok_sdk_members WHERE agent_id=?', (agent,)).fetchone()
        return dict(row) if row else None

    def valid(self, member, auth=True):
        row = self._member(member['agent_id'])
        target = tuple(member[k] for k in ('local_session','bot_id','native_session','account_fingerprint'))
        if (self.stop.is_set() or not row or not row['active'] or row['generation'] != member['generation']
                or self.selected().get(member['agent_id']) != target):
            return False
        if auth:
            adapter = self._adapter()
            if not adapter:
                return False
            current = adapter.client.auth_status()
            if current.get('state') != 'authenticated' or current.get('account_fingerprint') != member['account_fingerprint']:
                return False
        return True

    def describe(self, agent):
        if not self.enabled or self.stop.is_set():
            return None
        with self.bridge.commands.lock, self.bridge.history.lock:
            row = self._member(agent)
            if not row or not row['active']:
                return None
            authenticated=self.valid(row)
            fresh = authenticated and row['last_ok'] is not None and time.time() - row['last_ok'] < 45 and not row['error']
            gap=self.bridge.history.db.execute('SELECT 1 FROM grok_sdk_entries WHERE agent_id=? AND generation=? AND gap=1 LIMIT 1',
                (agent,row['generation'])).fetchone() is not None
            proofs=self.bridge.history.db.execute("SELECT command_id FROM grok_sdk_runs WHERE agent_id=? AND generation=? AND status='done' AND native_outcome='success' AND delivery!='' AND warning IS NULL",
                (agent,row['generation'])).fetchall()
            return {'state': 'configured' if fresh else 'recovering' if row['last_ok'] else 'connecting',
                    'active': fresh, 'history_complete':not gap,
                    'execution_verified':authenticated and any(p['command_id'] for p in proofs),
                    'manual_work_verified':authenticated and any(p['command_id'] is None for p in proofs),
                    'reason': None if fresh else 'grok_sdk_login_required' if not authenticated else row['error'] or 'grok_sdk_sync_connecting'}

    observation = describe

    def decorate_run(self, item):
        if not item or not isinstance(item,dict) or not item.get('agent_id','').startswith('grok_bot:'):
            return item
        with self.bridge.history.lock:
            run=item.get('run_id')
            if not run and item.get('id'):
                original=self.bridge.history.db.execute('SELECT run_id FROM runs WHERE id=? AND agent_id=?',(item['id'],item['agent_id'])).fetchone()
                run=original['run_id'] if original else None
            if not run:return item
            row = self.bridge.history.db.execute('SELECT warning FROM grok_sdk_runs WHERE agent_id=? AND local_run=? ORDER BY rowid DESC LIMIT 1',
                (item['agent_id'], run)).fetchone()
        return dict(item, sync_warning=row['warning']) if row and row['warning'] else item

    def busy(self, agent):
        with self.bridge.commands.lock, self.bridge.history.lock:
            row = self._member(agent)
            # A stale working state remains a dispatch fence; silence does not
            # authorize a new prompt into an uncertain native task.
            pending_form=self.bridge.history.db.execute("SELECT 1 FROM grok_sdk_runs WHERE agent_id=? AND generation=? AND warning='grok_sdk_user_action_required' AND status='working' LIMIT 1",
                (agent,row['generation'])).fetchone() if row else None
            return bool(row and row['active'] and (row['running'] or row['waiting'] or pending_form or not row['cursor'] or not row['last_ok']
                or row['state_at'] is None or row['error'] or not self.valid(row)))

    def _health(self, member, error=None, successful=False):
        with self.bridge.commands.lock, self.bridge.history.lock, self.bridge.history.db:
            if self.valid(member):
                self.bridge.history.db.execute('UPDATE grok_sdk_members SET error=?,last_ok=CASE WHEN ? THEN ? ELSE last_ok END WHERE agent_id=?',
                    (error, int(successful), time.time(), member['agent_id']))

    def _public_message(self, member, run, role, phase, text, entry_id, at, command_id=None):
        h = self.bridge.history
        event_id = 'grok-sdk:' + hashlib.sha256((member['generation'] + ':' + str(member['native_generation']) + ':' + entry_id).encode()).hexdigest()
        if role=='user' and command_id:
            event_id='grok_sdk:'+command_id+':prompt'
        clean = redact(text, h.secrets)
        old = h.db.execute('SELECT run_id,role,phase,text FROM messages WHERE agent_id=? AND event_id=?', (member['agent_id'], event_id)).fetchone()
        if old and tuple(old) == (run, role, phase, clean):
            return
        h.db.execute('DELETE FROM messages WHERE agent_id=? AND event_id=?', (member['agent_id'], event_id))
        h.message(member['agent_id'], run, role, phase, clean, at=at, event_id=event_id, commit=False)

    def _command(self, member, public, entry_id):
        """A public marker alone does not authorize matching another turn."""
        from commands import MARKER
        text = public['text']
        match = MARKER.match(text)
        if not match or public.get('message_id') != match[1]:
            return None
        c = self.bridge.commands
        row = c.db.execute('SELECT * FROM commands WHERE id=? AND agent_id=?', (match[1], member['agent_id'])).fetchone()
        if (not row or c.detached(row['id']) or row['adapter'] != 'grok_sdk'
                or row['session_id'] != member['local_session'] or row['state'] == 'pending'
                or row['native_message_id'] not in (None, public['message_id'])
                or not c.matches_fingerprint(member['agent_id'], text[match.end():].strip(), row['fingerprint'])):
            return None
        return dict(row)

    def _run_for(self, member, public, entry_id, seq):
        h, agent, generation = self.bridge.history, member['agent_id'], member['generation']
        previous = h.db.execute('SELECT local_run FROM grok_sdk_entries WHERE agent_id=? AND generation=? AND native_generation=? AND entry_id=?',
            (agent, generation, member['native_generation'], entry_id)).fetchone()
        if previous and previous['local_run']:
            old=h.db.execute('SELECT * FROM grok_sdk_runs WHERE agent_id=? AND generation=? AND local_run=?',
                (agent,generation,previous['local_run'])).fetchone()
            request=public.get('request_id')
            if request and old['native_request'] and old['native_request']!=request:
                raise SyncError('grok_sdk_entry_correlation_changed')
            if request and not old['native_request']:
                h.db.execute('UPDATE grok_sdk_runs SET native_request=? WHERE agent_id=? AND generation=? AND local_run=?',
                    (request,agent,generation,previous['local_run']))
                old=h.db.execute('SELECT * FROM grok_sdk_runs WHERE agent_id=? AND generation=? AND local_run=?',
                    (agent,generation,previous['local_run'])).fetchone()
                if old['prompt_entry']:self._bind_orphan_replies(member,dict(old),old['native_turn'])
            return h.db.execute('SELECT * FROM grok_sdk_runs WHERE agent_id=? AND generation=? AND local_run=?',
                (agent,generation,previous['local_run'])).fetchone()
        command = self._command(member, public, entry_id) if public['kind'] == 'prompt' else None
        if public['kind'] == 'reply':
            # Native App reply selection uses the shared requestId. It is
            # independent of the client nonce and settlement turnId. Never
            # infer this relationship from timestamps or entry-name prefixes.
            request=public.get('request_id')
            if request:
                rows=h.db.execute('SELECT * FROM grok_sdk_runs WHERE agent_id=? AND generation=? AND native_request=? AND prompt_entry IS NOT NULL',
                    (agent,generation,request)).fetchall()
                if len(rows)==1:return rows[0]
                if len(rows)>1:return self._new_run(member,public,entry_id,seq,None)
            clauses, args = [], []
            # A task can contain many human turns. Task ID alone cannot pair
            # a delivery to the instruction that triggered it.
            for column, value in [('batch_id', public.get('batch_id')), ('native_turn',public.get('batch_id')),
                                  ('prompt_entry', public.get('reply_to')),('message_id',public.get('reply_to'))]:
                if value:
                    clauses.append(column + '=?'); args.append(value)
            if clauses:
                rows = h.db.execute('SELECT * FROM grok_sdk_runs WHERE agent_id=? AND generation=? AND prompt_entry IS NOT NULL AND (' + ' OR '.join(clauses) + ')',
                    (agent, generation, *args)).fetchall()
                if len(rows) == 1 and not (request and rows[0]['native_request'] and rows[0]['native_request']!=request):
                    return rows[0]
        return self._new_run(member,public,entry_id,seq,command)

    def _new_run(self,member,public,entry_id,seq,command):
        h,agent,generation=self.bridge.history,member['agent_id'],member['generation']
        local = command['id'] if command else 'grok-' + hashlib.sha256((generation + ':' + str(member['native_generation']) + ':' + entry_id).encode()).hexdigest()[:48]
        status = 'working' if public['kind'] == 'prompt' else 'unknown'
        warning = None if public['kind'] == 'prompt' else 'grok_sdk_reply_unpaired'
        h.db.execute('INSERT OR IGNORE INTO grok_sdk_runs (agent_id,generation,local_run,task_id,batch_id,prompt_entry,message_id,command_id,status,started_at,last_seq,delivery,warning,native_request) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (agent, generation, local, public.get('task_id'), public.get('batch_id'),
             entry_id if public['kind'] == 'prompt' else None, public.get('message_id'),
             command['id'] if command else None, status, public['at'], str(seq), '', warning,public.get('request_id')))
        if command:
            event = {'adapter':'grok_sdk','session_id':member['local_session'],'run_id':local,
                     'native_run_id':command['id']}
            if command['native_message_id'] is None:
                self.bridge.commands.observe_native(agent, dict(event, type='accepted'))
            self.bridge.commands.observe_native(agent, dict(event, type='started', text=public['text']))
        row=h.db.execute('SELECT * FROM grok_sdk_runs WHERE agent_id=? AND generation=? AND local_run=?',
            (agent,generation,local)).fetchone()
        if public['kind']=='prompt':self._bind_orphan_replies(member,dict(row),None)
        return h.db.execute('SELECT * FROM grok_sdk_runs WHERE agent_id=? AND generation=? AND local_run=?',
            (agent,generation,local)).fetchone()

    def _project_entry(self, member, data, body=None, missing=False, deleted=False):
        h, agent = self.bridge.history, member['agent_id']
        entry_id = ident(data.get('entryId'))
        if not entry_id:
            raise SyncError('grok_sdk_entry_identity_missing')
        seq, updated = uint(data.get('seq')), uint(data.get('updatedSeq'))
        ng = native_generation(data.get('generation'))
        previous_generation=member.get('native_generation')
        member = dict(member, native_generation=ng)
        old = h.db.execute('SELECT * FROM grok_sdk_entries WHERE agent_id=? AND generation=? AND native_generation=? AND entry_id=?',
            (agent, member['generation'], ng, entry_id)).fetchone()
        if old and uint(old['updated_seq']) >= updated and not (old['gap'] and not missing):
            return
        if member.get('floor_seq') is not None and ng == previous_generation and seq <= uint(member['floor_seq']) and old is None:
            return
        public = public_entry(body) if not missing and not deleted else None
        kind = public['kind'] if public else 'missing' if missing else 'deleted' if deleted else 'private'
        role, text, local = None, None, old['local_run'] if old else None
        if public:
            run = self._run_for(member, public, entry_id, seq)
            local, role = run['local_run'], 'user' if public['kind'] == 'prompt' else 'assistant'
            text = redact(public['text'], h.secrets)
            definition = self.bridge.definitions.get(agent)
            if not definition:
                return
            if role == 'user':
                # A later explicit human turn resumes the selected chat. It
                # does not fabricate completion for an older paused form.
                h.db.execute("UPDATE grok_sdk_runs SET warning='grok_sdk_form_superseded' WHERE agent_id=? AND generation=? AND warning='grok_sdk_user_action_required' AND local_run!=?",
                    (agent,member['generation'],local))
                if run['command_id']:
                    from commands import MARKER
                    marker = MARKER.match(public['text'])
                    text = redact(public['text'][marker.end():].strip(), h.secrets)
                h.record(agent, 'grok_bot', definition['name'], local, run['status'], prompt=text,
                    source_kind='grok_sdk_native', source_ref=member['bot_id'], started_at=run['started_at'], commit=False)
            else:
                h.db.execute('UPDATE grok_sdk_runs SET last_seq=?,reply_ready=? WHERE agent_id=? AND generation=? AND local_run=?',
                    (str(seq),int(not public.get('streaming') and not public.get('waiting')),agent,member['generation'],local))
                if public.get('waiting'):
                    h.db.execute('UPDATE grok_sdk_runs SET warning=? WHERE agent_id=? AND generation=? AND local_run=?',
                        ('grok_sdk_user_action_required',agent,member['generation'],local))
            self._public_message(member, local, role, 'prompt' if role=='user' else 'final' if run['status'] in {'done','error','cancelled'} else 'commentary', text, entry_id, public['at'],run['command_id'])
            if role=='assistant':
                delivery=self._delivery(member,local,entry_id,seq,text)
                h.db.execute('UPDATE grok_sdk_runs SET delivery=? WHERE agent_id=? AND generation=? AND local_run=?',
                    (delivery,agent,member['generation'],local))
                h.record(agent,'grok_bot',definition['name'],local,run['status'],delivery=delivery,
                    source_kind='grok_sdk_native',source_ref=member['bot_id'],started_at=run['started_at'],commit=False)
            if role=='assistant' and not public.get('streaming'):
                latest=h.db.execute('SELECT * FROM grok_sdk_runs WHERE agent_id=? AND generation=? AND local_run=?',
                    (agent,member['generation'],local)).fetchone()
                if latest['native_outcome']:
                    state={'success':'completed','cancelled':'cancelled','error':'failed'}[latest['native_outcome']]
                    self.finish(member,dict(latest),state)
        elif missing:
            h.db.execute('UPDATE grok_sdk_runs SET warning=? WHERE agent_id=? AND generation=? AND status IN (?,?)',
                ('grok_sdk_transcript_gap',agent,member['generation'],'working','unknown'))
        elif deleted and local:
            event_id='grok-sdk:'+hashlib.sha256((member['generation']+':'+str(ng)+':'+entry_id).encode()).hexdigest()
            h.db.execute('DELETE FROM messages WHERE agent_id=? AND event_id=?',(agent,event_id))
            h.db.execute('UPDATE grok_sdk_runs SET warning=? WHERE agent_id=? AND generation=? AND local_run=?',
                ('grok_sdk_entry_deleted',agent,member['generation'],local))
            replies=h.db.execute("SELECT text FROM messages WHERE agent_id=? AND run_id=? AND role='assistant' ORDER BY seq",(agent,local)).fetchall()
            delivery='\n\n'.join(r['text'] for r in replies)
            h.db.execute('UPDATE grok_sdk_runs SET delivery=? WHERE agent_id=? AND generation=? AND local_run=?',(delivery,agent,member['generation'],local))
            h.db.execute('UPDATE runs SET delivery=? WHERE agent_id=? AND run_id=?',(delivery,agent,local))
        h.db.execute('INSERT OR REPLACE INTO grok_sdk_entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (agent,member['generation'],ng,entry_id,str(seq),str(updated),kind,local,role,text,int(missing),int(deleted)))
        if old and old['gap'] and not missing:
            remaining=h.db.execute('SELECT 1 FROM grok_sdk_entries WHERE agent_id=? AND generation=? AND gap=1 LIMIT 1',
                (agent,member['generation'])).fetchone()
            if not remaining:
                h.db.execute('UPDATE grok_sdk_runs SET warning=NULL WHERE agent_id=? AND generation=? AND warning=?',
                    (agent,member['generation'],'grok_sdk_transcript_gap'))
                for pending in h.db.execute('SELECT * FROM grok_sdk_runs WHERE agent_id=? AND generation=? AND native_outcome IS NOT NULL AND status=?',
                        (agent,member['generation'],'working')).fetchall():
                    self.finish(member,dict(pending),{'success':'completed','cancelled':'cancelled','error':'failed'}[pending['native_outcome']])

    def _delivery(self,member,local,entry_id=None,seq=None,text=None):
        rows=self.bridge.history.db.execute("SELECT entry_id,seq,text FROM grok_sdk_entries WHERE agent_id=? AND generation=? AND local_run=? AND public_role='assistant' AND deleted=0",
            (member['agent_id'],member['generation'],local)).fetchall()
        values={r['entry_id']:(uint(r['seq']),r['text']) for r in rows}
        if entry_id is not None:values[entry_id]=(seq,text)
        return '\n\n'.join(value[1] for value in sorted(values.values(),key=lambda pair:pair[0]) if value[1])[:256000]

    def process_event(self, member, event):
        """Called only for a selected session; safe for replay and unit fixtures."""
        if not isinstance(event, dict) or not isinstance(event.get('data'), dict):
            raise SyncError('grok_sdk_invalid_event')
        kind, data = event.get('type'), event['data']
        if kind not in {'connected','heartbeat','box.state','page.title.changed'}:
            if data.get('botId') != member['bot_id']:
                raise SyncError('grok_sdk_event_scope_mismatch')
            if 'sessionId' in data and data['sessionId'] != member['native_session']:
                raise SyncError('grok_sdk_event_scope_mismatch')
        token = event.get('id')
        if token is not None:
            ng, updated = token_parts(token, member['bot_id'], member['native_session'])
            if 'generation' in data and native_generation(data['generation']) != ng:
                raise SyncError('grok_sdk_event_cursor_mismatch')
            if 'updatedSeq' in data and uint(data['updatedSeq']) != updated:
                raise SyncError('grok_sdk_event_cursor_mismatch')
        b,h=self.bridge,self.bridge.history
        with b.commands.lock,h.lock,h.db:
            if not self.valid(member):
                return False
            if kind == 'reset':
                h.db.execute('UPDATE grok_sdk_members SET native_generation=?,floor_seq=? WHERE agent_id=?',
                    (native_generation(data['generation']),'0',member['agent_id']))
                member.update(native_generation=native_generation(data['generation']),floor_seq='0')
                h.db.execute('UPDATE grok_sdk_runs SET warning=? WHERE agent_id=? AND generation=? AND status IN (?,?)',
                    ('grok_sdk_session_reset',member['agent_id'],member['generation'],'working','unknown'))
            elif kind in {'message','entry','entry.unavailable','message.deleted'}:
                body = data.get('entry')
                if kind == 'message':
                    # A worker enriches normalized text from a scoped history
                    # read when possible; otherwise preserve an unpaired reply.
                    body = data.get('entry') or {'kind':'send-message','id':data.get('entryId'),
                        'message':{'type':'text','content':data.get('text')}}
                self._project_entry(member,data,body,missing=kind=='entry.unavailable',deleted=kind=='message.deleted')
            elif kind == 'agent.state':
                self._agent_state(member,data)
            elif kind == 'resync-required':
                h.db.execute('UPDATE grok_sdk_members SET error=? WHERE agent_id=?',('grok_sdk_resync_required',member['agent_id']))
                return False
            elif kind == 'turn.failed':
                self._turn_failure(member,data)
            if token is not None:
                current=self._member(member['agent_id'])
                if current['cursor']:
                    previous_generation,previous_updated=token_parts(current['cursor'],member['bot_id'],member['native_session'])
                    if previous_generation==ng and previous_updated>updated:
                        token=current['cursor']
                h.db.execute('UPDATE grok_sdk_members SET cursor=?,native_generation=? WHERE agent_id=?',
                    (token,ng,member['agent_id']))
                member.update(cursor=token,native_generation=ng)
            h.db.execute('UPDATE grok_sdk_members SET last_ok=?,error=NULL WHERE agent_id=?',(time.time(),member['agent_id']))
        return True

    def _agent_state(self, member, data):
        live=[row for row in data.get('live',[]) if isinstance(row,dict) and row.get('agentId')==member['bot_id'] and row.get('sessionId')==member['native_session']]
        if not live:
            return
        row=live[-1]
        if type(row.get('isRunning')) is not bool:
            return
        running=row['isRunning'] or row.get('isComposingMessage') is True or row.get('hasRunningSubagents') is True
        waiting=isinstance(row.get('awaiting'),dict) and bool(row['awaiting'].get('reason'))
        form=self.bridge.history.db.execute("SELECT 1 FROM grok_sdk_runs WHERE agent_id=? AND generation=? AND status='working' AND warning='grok_sdk_user_action_required' LIMIT 1",
            (member['agent_id'],member['generation'])).fetchone()
        waiting=waiting or bool(form)
        h,b=self.bridge.history,self.bridge
        h.db.execute('UPDATE grok_sdk_members SET running=?,waiting=?,state_at=? WHERE agent_id=?',(int(running),int(waiting),time.time(),member['agent_id']))
        definition=b.definitions.get(member['agent_id'])
        if definition and hasattr(b,'report'):
            b.report(definition['name'],{'status':'waiting_user' if waiting else 'working' if running else 'idle',
                'step':'請在 Grok Bot 原 App 處理待確認事項' if waiting else 'Grok Bot 正在工作' if running else 'Grok Bot 待命'},'grok_bot',agent_id=member['agent_id'])
        settlement=row.get('lastTurnSettlement')
        if isinstance(settlement,dict):
            self._settlement(member,settlement)

    def _settlement(self,member,value):
        nonce=ident(value.get('clientNonce'))
        native=ident(value.get('turnId'))
        outcome=value.get('outcome')
        if not nonce or not native or outcome not in {'success','cancelled','error'}:
            return
        try:
            at=uint(value.get('settledAtMs'))
            if at<=0:return
        except SyncError:return
        h=self.bridge.history
        rows=h.db.execute('SELECT * FROM grok_sdk_runs WHERE agent_id=? AND generation=? AND message_id=?',
            (member['agent_id'],member['generation'],nonce)).fetchall()
        if len(rows)!=1:return
        row=dict(rows[0])
        if row['native_turn'] and row['native_turn']!=native:return
        if row['native_outcome'] and row['native_outcome']!=outcome:return
        h.db.execute('UPDATE grok_sdk_runs SET native_outcome=?,settled_at=?,native_turn=? WHERE agent_id=? AND generation=? AND local_run=?',
            (outcome,str(at),native,member['agent_id'],member['generation'],row['local_run']))
        self._bind_orphan_replies(member,row,native)
        row=dict(h.db.execute('SELECT * FROM grok_sdk_runs WHERE agent_id=? AND generation=? AND local_run=?',
            (member['agent_id'],member['generation'],row['local_run'])).fetchone())
        self.finish(member,row,{'success':'completed','cancelled':'cancelled','error':'failed'}[outcome])

    def _bind_orphan_replies(self,member,row,native):
        """Repair only explicit unique request or native turn/batch links."""
        h=self.bridge.history;agent=member['agent_id'];generation=member['generation']
        request=row.get('native_request')
        if request:
            matches=h.db.execute('SELECT COUNT(*) FROM grok_sdk_runs WHERE agent_id=? AND generation=? AND native_request=? AND prompt_entry IS NOT NULL',
                (agent,generation,request)).fetchone()[0]
            if matches!=1:request=None
        if not request and not native:return
        possible=h.db.execute('SELECT * FROM grok_sdk_runs WHERE agent_id=? AND generation=? AND prompt_entry IS NULL AND warning=?',
            (agent,generation,'grok_sdk_reply_unpaired')).fetchall()
        candidates=[orphan for orphan in possible if
            (request and orphan['native_request']==request)
            or (native and orphan['batch_id']==native and not (row.get('native_request') and orphan['native_request'] and row['native_request']!=orphan['native_request']))]
        for orphan in candidates:
            if orphan['local_run']==row['local_run']:continue
            h.db.execute('UPDATE messages SET run_id=? WHERE agent_id=? AND run_id=?',(row['local_run'],agent,orphan['local_run']))
            h.db.execute('UPDATE grok_sdk_entries SET local_run=? WHERE agent_id=? AND generation=? AND local_run=?',
                (row['local_run'],agent,generation,orphan['local_run']))
            h.db.execute('DELETE FROM runs WHERE agent_id=? AND run_id=? AND source_kind=?',
                (agent,orphan['local_run'],'grok_sdk_native'))
            h.db.execute('DELETE FROM grok_sdk_runs WHERE agent_id=? AND generation=? AND local_run=?',
                (agent,generation,orphan['local_run']))
        if candidates:
            delivery=self._delivery(member,row['local_run'])
            ready=any(orphan['reply_ready'] for orphan in candidates)
            h.db.execute('UPDATE grok_sdk_runs SET delivery=?,reply_ready=? WHERE agent_id=? AND generation=? AND local_run=?',
                (delivery,int(ready),agent,generation,row['local_run']))

    def _turn_failure(self, member, data):
        # Native failure IDs must match a known batch/task; never attach a
        # failure to whichever Office command happens to be outstanding.
        target=ident(data.get('turnId'))
        if not target:
            return
        rows=self.bridge.history.db.execute('SELECT * FROM grok_sdk_runs WHERE agent_id=? AND generation=? AND (batch_id=? OR native_turn=?)',
            (member['agent_id'],member['generation'],target,target)).fetchall()
        if len(rows)==1:
            self.finish(member,dict(rows[0]),'failed','grok_sdk_native_turn_failed')

    def finish(self, member, row, state, reason=None):
        """Only callers with exact native terminal evidence may call this."""
        b,h=self.bridge,self.bridge.history
        if row['status'] in {'done','error','cancelled'} or not self.valid(member):
            return
        if state=='completed' and (not row['delivery'] or not row['reply_ready'] or row['warning']):
            return
        agent,local=member['agent_id'],row['local_run']
        status={'completed':'done','failed':'error','cancelled':'cancelled'}[state]
        definition=b.definitions.get(agent)
        if not definition:return
        if row['command_id']:
            b.commands.observe_native(agent,{'type':'finished','adapter':'grok_sdk','session_id':member['local_session'],
                'run_id':local,'native_run_id':row['command_id'],'state':state,'text':row['delivery'],'detail_code':reason})
        else:
            h.record(agent,'grok_bot',definition['name'],local,status,delivery=row['delivery'],
                source_kind='grok_sdk_native',source_ref=member['bot_id'],commit=False)
        h.db.execute('UPDATE grok_sdk_runs SET status=? WHERE agent_id=? AND generation=? AND local_run=?',
            (status,agent,member['generation'],local))
        # Promote only this run's observed public replies, retaining entry IDs.
        messages=h.db.execute("SELECT * FROM messages WHERE agent_id=? AND run_id=? AND role='assistant'",(agent,local)).fetchall()
        for msg in messages:
            if msg['phase']=='final':continue
            h.db.execute('DELETE FROM messages WHERE seq=?',(msg['seq'],))
            h.message(agent,local,'assistant','final',msg['text'],at=msg['at'],event_id=msg['event_id'],commit=False)

    def bootstrap(self, member, client):
        page=client.list_transcript(member['bot_id'],member['native_session'],limit=1)
        if not isinstance(page,dict) or not isinstance(page.get('entries'),list):
            raise SyncError('grok_sdk_invalid_history')
        ng=native_generation(page.get('generation'))
        rows=page['entries'];updated=max([uint(r.get('updatedSeq',r.get('seq'))) for r in rows]+[0]);floor=max([uint(r.get('seq')) for r in rows]+[0])
        token=cursor_token(member['bot_id'],member['native_session'],ng,updated)
        with self.bridge.commands.lock,self.bridge.history.lock,self.bridge.history.db:
            if not self.valid(member):return False
            self.bridge.history.db.execute('UPDATE grok_sdk_members SET cursor=?,native_generation=?,floor_seq=? WHERE agent_id=?',
                (token,ng,str(floor),member['agent_id']))
            member.update(cursor=token,native_generation=ng,floor_seq=str(floor))
        return True

    def resync(self, member, client):
        """Bounded reload after cursor expiry; retain visible gaps on truncation."""
        before=None;entries=[];ng=None;done=False
        floor=uint(member['floor_seq'] or 0)
        for _ in range(self.history_pages):
            page=client.list_transcript(member['bot_id'],member['native_session'],limit=self.history_page_size,before_seq=before,generation=ng)
            current=native_generation(page.get('generation'))
            if ng is not None and current!=ng:raise SyncError('grok_sdk_history_generation_changed')
            ng=current;rows=page.get('entries')
            if not isinstance(rows,list):raise SyncError('grok_sdk_invalid_history')
            entries.extend(rows)
            seqs=[uint(row.get('seq')) for row in rows]
            if not rows or len(rows)<self.history_page_size or min(seqs)<=floor:
                done=True;break
            next_before=str(min(seqs))
            if next_before==before:raise SyncError('grok_sdk_history_pagination_stalled')
            before=next_before
        if not done:raise SyncError('grok_sdk_history_gap')
        if ng!=member.get('native_generation'):
            self.process_event(member,{'type':'reset','id':cursor_token(member['bot_id'],member['native_session'],ng,0),
                'data':{'botId':member['bot_id'],'sessionId':member['native_session'],'generation':ng}})
        for row in sorted(entries,key=lambda r:uint(r.get('updatedSeq',r.get('seq')))):
            updated=row.get('updatedSeq',row.get('seq'));body=row.get('body')
            data={'botId':member['bot_id'],'sessionId':member['native_session'],'generation':ng,'seq':row.get('seq'),
                  'updatedSeq':updated,'entryId':row.get('entryId'),'entry':body}
            kind='entry' if isinstance(body,dict) and not row.get('bodyOmitted') else 'entry.unavailable'
            self.process_event(member,{'type':kind,'id':cursor_token(member['bot_id'],member['native_session'],ng,updated),'data':data})
        return True

    def _enrich(self, member, client, event):
        if event.get('type')!='message':return event
        data=event['data']
        # Only a selected-session read; no account roster or unrelated history.
        page=client.list_transcript(member['bot_id'],member['native_session'],limit=50)
        if page.get('generation')!=data.get('generation'):return event
        found=next((r for r in page.get('entries',[]) if r.get('entryId')==data.get('entryId')
                    and uint(r.get('updatedSeq',r.get('seq'))) == uint(data.get('updatedSeq'))),None)
        if found and isinstance(found.get('body'),dict) and not found.get('bodyOmitted'):
            return dict(event,data=dict(data,entry=found['body']))
        return event

    def snapshot_state(self,member,client):
        """Recover a selected Bot's native state/settlement, never send work."""
        result=client.agent_statuses([member['bot_id']])
        if not isinstance(result,dict) or not isinstance(result.get('statuses'),list):
            raise SyncError('grok_sdk_invalid_native_state')
        selected=[r for r in result['statuses'] if isinstance(r,dict) and r.get('agentId')==member['bot_id']]
        if len(selected)!=1:return
        row=selected[0];live=row.get('live')
        if isinstance(live,dict):
            if live.get('agentId')!=member['bot_id'] or live.get('sessionId')!=member['native_session']:return
        elif row.get('isRunning') is False:
            # No live turn anywhere on this Bot is sufficient for idle, but
            # a nonmatching active session is never relabelled as this one.
            live={'agentId':member['bot_id'],'sessionId':member['native_session'],'isRunning':False}
        else:return
        self.process_event(member,{'type':'agent.state','data':{'botId':member['bot_id'],
            'sessionId':member['native_session'],'snapshot':True,'live':[live]}})

    def observe(self, member, client, stopped):
        key=(member['agent_id'],member['generation'])
        success=False
        try:
            if not member.get('cursor') and not self.bootstrap(member,client):return
            self.snapshot_state(member,client)
            last_snapshot=time.monotonic()
            for event in client.watch(member['bot_id'],member['native_session'],resume_from=member['cursor'],stop_event=stopped):
                if stopped.is_set() or self.stop.is_set():return
                if event.get('type')=='resync-required':
                    self.resync(member,client);break
                event=self._enrich(member,client,event)
                if not self.process_event(member,event):return
                success=True
                if event.get('type')=='heartbeat' and time.monotonic()-last_snapshot>=self.interval:
                    self.snapshot_state(member,client);last_snapshot=time.monotonic()
        except Exception:
            if not stopped.is_set() and not self.stop.is_set():self._health(member,'grok_sdk_observation_interrupted')
        finally:
            if not stopped.is_set() and not self.stop.is_set():
                with self.lock:
                    failures=0 if success else min(5,self.failures.get(key,0)+1)
                    self.failures[key]=failures
                    self.retry_at[key]=time.monotonic()+min(60,self.interval*2**failures)

    def poll_once(self):
        if not self.enabled or self.stop.is_set():return
        self.sync_members();adapter=self._adapter()
        if not adapter:return
        with self.bridge.commands.lock,self.bridge.history.lock:
            members=[dict(r) for r in self.bridge.history.db.execute('SELECT * FROM grok_sdk_members WHERE active=1')]
        with self.lock:
            active=sum(worker.is_alive() for worker in self.workers.values())
            for member in members:
                key=(member['agent_id'],member['generation']);old=self.workers.get(key)
                if old and old.is_alive() or active>=self.max_workers or time.monotonic()<self.retry_at.get(key,0):continue
                if not adapter.available(member['local_session']):continue
                stopped=threading.Event();worker=threading.Thread(target=self.observe,args=(member,adapter.client,stopped),daemon=True,name='office-grok-sdk-observer')
                self.worker_stops[key]=stopped;self.workers[key]=worker;active+=1;worker.start()

    def run(self, stop):
        while not stop.is_set() and not self.stop.is_set():
            try:self.poll_once()
            except (OSError,sqlite3.Error,ValueError):pass
            stop.wait(0.5)

    def close(self):
        self.stop.set()
        with self.lock:
            for stopped in self.worker_stops.values():stopped.set()
            workers=list(self.workers.values())
        deadline=time.monotonic()+5
        for worker in workers:
            if worker.is_alive():worker.join(timeout=max(0,deadline-time.monotonic()))
