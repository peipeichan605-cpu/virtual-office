"""Observed parent/child agents. No membership, credentials or execution authority.

The registry contract is provider neutral. CodexDiscovery is the first source
adapter; it reads only descendants of explicitly selected native sessions.
"""
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid
from history import redact, timestamp

CAPABILITY = {'enabled': False, 'can_send': False, 'same_session': False,
              'persistent': False, 'mode': 'observation', 'reason': 'observed_subagent'}
MAX_CHILDREN = 128
MAX_DEPTH = 8
RETAIN_SECONDS = 30
POLL_BYTES = 8 * 1024 * 1024
FILE_BYTES = 2 * 1024 * 1024


def seconds(value):
    if isinstance(value, (float, int)):
        return value / 1000 if value > 1e11 else float(value)
    try:
        import datetime
        return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, AttributeError, TypeError):
        return 0


def spawn_parent(source):
    try:
        data = json.loads(source) if isinstance(source, str) else source
        return data['subagent']['thread_spawn'].get('parent_thread_id')
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def clean_name(value, fallback):
    value = str(value or fallback)
    value = ''.join(c for c in value if ord(c) >= 32 and ord(c) != 127)
    return redact(value, limit=60) or fallback


class CodexDiscovery:
    """Schema-aware, bounded relationship queries; no whole-inventory scan."""
    def __init__(self, database):
        self.database = Path(database)

    def children(self, parent):
        with closing(sqlite3.connect(self.database.resolve().as_uri() + '?mode=ro', uri=True, timeout=.1)) as db:
            db.row_factory = sqlite3.Row
            cols = {r[1] for r in db.execute('PRAGMA table_info(threads)')}
            if not {'id', 'rollout_path'} <= cols:
                raise ValueError('subagent_schema_unavailable')
            selected = ['id', 'rollout_path'] + [x for x in ('source', 'created_at', 'created_at_ms', 'updated_at', 'updated_at_ms', 'agent_path', 'agent_nickname', 'agent_role') if x in cols]
            sqlcols = ','.join('t.' + x for x in selected)
            edges = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='thread_spawn_edges'").fetchone()
            if edges:
                query = 'SELECT ' + sqlcols + ' FROM threads t JOIN thread_spawn_edges e ON t.id=e.child_thread_id WHERE e.parent_thread_id=?'
            elif 'source' in cols:
                query = 'SELECT ' + sqlcols + " FROM threads t WHERE json_valid(t.source) AND json_extract(t.source,'$.subagent.thread_spawn.parent_thread_id')=?"
            else:
                raise ValueError('subagent_relationships_unavailable')
            query += (' ORDER BY t.updated_at_ms DESC' if 'updated_at_ms' in cols else ' ORDER BY t.updated_at DESC' if 'updated_at' in cols else ' ORDER BY t.created_at_ms DESC' if 'created_at_ms' in cols else ' ORDER BY t.created_at DESC' if 'created_at' in cols else '') + ' LIMIT 256'
            rows = [dict(r) for r in db.execute(query, (parent,))]
            # An edge cannot override contradictory source ownership.
            return [r for r in rows if not spawn_parent(r.get('source')) or spawn_parent(r['source']) == parent]


class ChildTail:
    """Consume own child records after the native inherited-history boundary.

    Child rollouts can copy parent history and rewrite its timestamps. Timestamp
    filtering alone would expose inherited prompts and fake completion events.
    We require the native ordinal boundary and a fresh child task_started.
    """
    def __init__(self, row, expected_parent, expected_parent_path):
        self.row = row
        self.path = Path(row['source_path'])
        self.expected_parent = expected_parent
        self.expected_parent_path = expected_parent_path
        self.offset = 0
        self.ordinal = -1
        self.pending = b''
        self.inode = None
        self.boundary = None
        self.metadata = None
        self.run = None
        self.run_started = 0
        self.root_run = row['parent_run_id']
        self.own_turn = False
        self.last_event = 0
        self.status = 'waiting_user'
        self.terminal_at = None
        self.final_text = ''
        self.last_size = 0

    def verify(self, payload):
        if not isinstance(payload,dict):
            raise ValueError('invalid_child_metadata')
        if payload.get('id') != self.row['native_id']:
            raise ValueError('child_identity_mismatch')
        parents = [p for p in (payload.get('parent_thread_id'), spawn_parent(payload.get('source'))) if p is not None]
        if not parents or any(p != self.expected_parent for p in parents):
            raise ValueError('child_parent_mismatch')
        boundary = payload.get('subagent_history_start_ordinal')
        if type(boundary) is not int or not 0 <= boundary <= 1000000:
            raise ValueError('child_history_boundary_unavailable')
        born = seconds(payload.get('timestamp'))
        if not born or abs(born - self.row['born']) > 2:
            raise ValueError('child_birth_mismatch')
        path = payload.get('agent_path')
        if not isinstance(path, str) or not path.startswith('/'):
            raise ValueError('child_path_unavailable')
        self.boundary = boundary
        self.metadata = {'agent_path': path}

    def read(self, max_bytes=FILE_BYTES):
        if not self.path.is_absolute() or self.path.resolve() != self.path:
            raise ValueError('invalid_child_path')
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or (self.inode is not None and (self.inode != info.st_ino or info.st_size < self.offset)):
                raise ValueError('child_rollout_rotated')
            self.inode = info.st_ino
            self.last_size = info.st_size
            file.seek(self.offset)
            raw = file.read(max_bytes)
            self.offset += len(raw)
        self.pending += raw
        if len(self.pending) > 16 * 1024 * 1024:
            raise ValueError('child_line_too_large')
        lines = self.pending.split(b'\n'); self.pending = lines.pop()
        events = []
        for rawline in lines:
            self.ordinal += 1
            if not rawline:
                continue
            if self.metadata is not None and self.ordinal < self.boundary:
                continue
            try:
                record = json.loads(rawline)
            except (ValueError, UnicodeDecodeError):
                if self.metadata is None:
                    raise ValueError('invalid_child_metadata')
                continue
            payload = record.get('payload', {})
            if self.metadata is None:
                if self.ordinal != 0 or record.get('type') != 'session_meta':
                    raise ValueError('invalid_child_metadata')
                self.verify(payload)
                continue
            if self.ordinal < self.boundary:
                continue
            if not isinstance(payload, dict):
                continue
            stamp = seconds(record.get('timestamp'))
            if record.get('type') == 'event_msg' and payload.get('type') == 'task_started':
                # Native started_at may be whole seconds while birth has ms.
                start = seconds(payload.get('started_at'))
                run = payload.get('turn_id')
                root_run = payload.get('root_turn_id')
                if start < math.floor(self.row['born']) or not isinstance(run, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', run):
                    continue
                if not isinstance(root_run, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', root_run):
                    continue
                if run==self.run or start<self.run_started:
                    continue
                self.run, self.root_run = run, root_run
                self.run_started=start
                self.own_turn = True
                self.status = 'working'; self.terminal_at = None; self.final_text = ''
                self.last_event = stamp
                events.append({'kind': 'run', 'status': 'working', 'run_id': run, 'at': record.get('timestamp'), 'started_at': payload.get('started_at'), 'parent_run_id': root_run})
                continue
            if not self.own_turn:
                continue
            if record.get('type') == 'event_msg':
                kind = payload.get('type')
                if kind in {'task_complete', 'task_failed', 'turn_failed', 'task_aborted', 'turn_aborted'}:
                    if payload.get('turn_id') != self.run:
                        continue
                    self.status = 'error' if payload.get('error') or kind in {'task_failed', 'turn_failed'} else 'cancelled' if kind in {'task_aborted', 'turn_aborted'} else 'done'
                    self.terminal_at = stamp; self.last_event = stamp
                    text = payload.get('last_agent_message') or self.final_text
                    events.append({'kind': 'run', 'status': self.status, 'run_id': self.run, 'at': record.get('timestamp'), 'delivery': text if isinstance(text, str) else '', 'parent_run_id': self.root_run})
                    if isinstance(text, str) and text:
                        events.append({'kind': 'message', 'role': 'assistant', 'phase': 'final', 'text': text, 'run_id': self.run, 'at': record.get('timestamp'), 'parent_run_id': self.root_run})
                elif self.status == 'working':
                    self.last_event = max(self.last_event, stamp)
            elif record.get('type') == 'response_item':
                # Only explicit public text is stored. Never encrypted content,
                # reasoning, tool calls, tool results or arbitrary string fields.
                self.last_event = max(self.last_event, stamp)
                texts = [x['text'] for x in payload.get('content', []) if isinstance(x, dict) and x.get('type') in {'input_text', 'output_text', 'text'} and isinstance(x.get('text'), str)]
                role = phase = None
                if payload.get('type') == 'agent_message' and payload.get('author') == self.expected_parent_path and payload.get('recipient') == self.metadata['agent_path']:
                    role, phase = 'user', 'prompt'
                elif payload.get('type') == 'message' and payload.get('role') == 'assistant':
                    channel = payload.get('phase') or payload.get('channel')
                    if channel == 'commentary': role, phase = 'assistant', 'commentary'
                    elif channel in {'final_answer', 'final'}: role, phase = 'assistant', 'final'
                if role and texts:
                    text = '\n\n'.join(texts)
                    if phase == 'prompt':
                        # Native multi-agent messages can expose only routing
                        # headers while the task body remains encrypted. Do
                        # not present that empty envelope as a readable task.
                        encrypted=any(isinstance(item,dict) and 'encrypted_content' in item for item in payload.get('content',[]))
                        lines=[line.strip() for line in text.splitlines() if line.strip()]
                        header_only=bool(lines) and all(line.startswith(('Message Type:','Task name:','Sender:','Payload:')) for line in lines)
                        if encrypted and header_only and 'Payload:' in text and not text.partition('Payload:')[2].strip():
                            text='原平台未提供可讀任務內容；請查看子 Agent 的公開進度與交付。'
                    if phase == 'final': self.final_text = text
                    events.append({'kind': 'message', 'role': role, 'phase': phase, 'text': text, 'run_id': self.run, 'at': record.get('timestamp'), 'parent_run_id': self.root_run})
        return events


class ObservedChildren:
    """Registry and lifetime, guarded by Bridge's commands -> history locks."""
    def __init__(self, bridge):
        self.bridge = bridge
        self.tails = {}
        self.pending_tails = {}
        self.watch_ids = set()
        self.revisions = {}
        self.last_poll = 0
        self.remaining_bytes = POLL_BYTES
        self.read_ids = set()
        self.deadline = 0
        with bridge.history.lock, bridge.history.db:
            bridge.history.db.execute('''CREATE TABLE IF NOT EXISTS observed_agent_roots (
                root_id TEXT PRIMARY KEY,native_id TEXT NOT NULL,fingerprint TEXT NOT NULL,
                generation TEXT NOT NULL,active INTEGER NOT NULL)''')
            bridge.history.db.execute('''CREATE TABLE IF NOT EXISTS observed_agents (
                id TEXT PRIMARY KEY,provider TEXT NOT NULL,native_id TEXT NOT NULL,
                root_id TEXT NOT NULL,parent_id TEXT NOT NULL,generation TEXT NOT NULL,
                depth INTEGER NOT NULL,name TEXT NOT NULL,agent_path TEXT NOT NULL,
                born REAL NOT NULL,parent_run_id TEXT NOT NULL,root_started REAL NOT NULL,
                status TEXT NOT NULL,step TEXT NOT NULL,run_id TEXT,updated_at REAL NOT NULL,
                terminal_at REAL,source_path TEXT NOT NULL,parent_context_run TEXT NOT NULL DEFAULT '',native_revision TEXT NOT NULL DEFAULT '',
                source_kind TEXT NOT NULL DEFAULT 'codex_native',evidence TEXT NOT NULL DEFAULT 'native_metadata',
                suppressed INTEGER NOT NULL DEFAULT 0)''')
            if 'parent_context_run' not in {r[1] for r in bridge.history.db.execute('PRAGMA table_info(observed_agents)')}:
                bridge.history.db.execute("ALTER TABLE observed_agents ADD COLUMN parent_context_run TEXT NOT NULL DEFAULT ''")
            if 'native_revision' not in {r[1] for r in bridge.history.db.execute('PRAGMA table_info(observed_agents)')}:
                bridge.history.db.execute("ALTER TABLE observed_agents ADD COLUMN native_revision TEXT NOT NULL DEFAULT ''")
            columns={r[1] for r in bridge.history.db.execute('PRAGMA table_info(observed_agents)')}
            for name,default in (('source_kind','codex_native'),('evidence','native_metadata')):
                if name not in columns:
                    bridge.history.db.execute("ALTER TABLE observed_agents ADD COLUMN "+name+" TEXT NOT NULL DEFAULT '"+default+"'")
            if 'suppressed' not in columns:
                bridge.history.db.execute('ALTER TABLE observed_agents ADD COLUMN suppressed INTEGER NOT NULL DEFAULT 0')
            bridge.history.db.execute('CREATE INDEX IF NOT EXISTS observed_root ON observed_agents(root_id,generation)')
            bridge.history.db.execute('''CREATE TABLE IF NOT EXISTS observed_agent_runs (
                agent_id TEXT NOT NULL,run_id TEXT NOT NULL,parent_run_id TEXT NOT NULL,
                PRIMARY KEY(agent_id,run_id))''')
        self.sync()

    @property
    def db(self): return self.bridge.history.db

    def sync(self):
        b = self.bridge
        with b.commands.lock, b.history.lock, b.history.db:
            selected = {a['id']: (a['source_id'], a) for a in b.definitions.values()}
            # Preserve the native identity and fingerprint of existing Codex
            # aliases. Other runtimes keep a separate observer binding.
            codex_roots={a['id'] for a in b.codex_targets.values()}
            selected.update({a['id']: (native, a) for native, a in b.codex_targets.items()})
            for row in self.db.execute('SELECT * FROM observed_agent_roots').fetchall():
                if row['root_id'] not in selected:
                    self.db.execute('UPDATE observed_agent_roots SET active=0 WHERE root_id=?', (row['root_id'],))
            for root, (native, definition) in selected.items():
                values=[native, definition.get('token_sha256'), definition.get('native_observe_after')]
                if root not in codex_roots:
                    binding=b.config.get('command_bindings',{}).get(root,{})
                    values=[definition['provider'],definition['driver'],*values,binding.get('adapter'),binding.get('session_id')]
                fingerprint = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
                previous = self.db.execute('SELECT * FROM observed_agent_roots WHERE root_id=?', (root,)).fetchone()
                generation = previous['generation'] if previous and previous['active'] and previous['fingerprint'] == fingerprint else uuid.uuid4().hex
                self.db.execute('INSERT INTO observed_agent_roots VALUES (?,?,?,?,1) ON CONFLICT(root_id) DO UPDATE SET native_id=excluded.native_id,fingerprint=excluded.fingerprint,generation=excluded.generation,active=1', (root, native, fingerprint, generation))
            allowed = self.allowed()
            self.tails = {k: v for k, v in self.tails.items() if k in allowed}
            active_generations={r['generation'] for r in self.db.execute('SELECT generation FROM observed_agent_roots WHERE active=1')}
            self.pending_tails={k:v for k,v in self.pending_tails.items() if v.row['generation'] in active_generations}

    def rows(self):
        return [dict(r) for r in self.db.execute('''SELECT a.* FROM observed_agents a JOIN observed_agent_roots r
            ON a.root_id=r.root_id AND a.generation=r.generation WHERE r.active=1 AND a.suppressed=0 ORDER BY a.depth,a.born,a.id''')
                if r['root_id'] in self.bridge.agents]

    def allowed(self):
        return {r['id']: self.descriptor(r) for r in self.rows()}

    def descriptor(self, row):
        return {'id': row['id'], 'name': row['name'], 'provider': row['provider'], 'native_url': None,
                'is_subagent': True, 'parent_agent_id': row['parent_id'], 'root_agent_id': row['root_id'],
                'depth': row['depth'], 'parent_run_id': row['parent_run_id'],
                'source_kind':row.get('source_kind','codex_native'),
                'evidence':row.get('evidence','native_metadata')}

    def decorate_runs(self, rows):
        children = {r['id']: r for r in self.rows()}
        result = []
        for row in rows:
            item = dict(row)
            child = children.get(row['agent_id'])
            if child:
                relation = self.descriptor(child)
                mapping = self.db.execute('SELECT parent_run_id FROM observed_agent_runs WHERE agent_id=? AND run_id=(SELECT run_id FROM runs WHERE id=?)', (child['id'], row['id'])).fetchone()
                item.update({k: v for k, v in relation.items() if k not in {'id', 'name', 'provider', 'native_url'}})
                if mapping: item['parent_run_id'] = mapping[0]
            result.append(item)
        return result

    def discover(self):
        b = self.bridge
        source = CodexDiscovery(b.config['codex_db'])
        known = [r for r in self.rows() if r.get('source_kind','codex_native')=='codex_native']
        self.watch_ids = set()
        self.revisions = {}
        selected_native = set(b.codex_targets)
        for root in self.db.execute('SELECT * FROM observed_agent_roots WHERE active=1').fetchall():
            definition = b.codex_targets.get(root['native_id'])
            tail = b.tails.get(root['native_id'])
            if not definition or not tail or not tail.run or not getattr(tail, 'run_started', 0):
                continue
            lower = max(tail.run_started, seconds(definition.get('native_observe_after')))
            current = {r['native_id']: r for r in known if r['root_id'] == root['root_id']}
            active_count=sum(r['status'] not in {'done','error','cancelled'} for r in current.values())
            queue = [(root['native_id'], root['root_id'], 0, getattr(tail,'agent_path','/root'))]
            queue.extend((r['native_id'], r['id'], r['depth'], r['agent_path']) for r in current.values() if r['depth'] < MAX_DEPTH and r['status'] not in {'done','error','cancelled'})
            visited = set()
            while queue and len(visited) <= MAX_CHILDREN and time.monotonic()<self.deadline and self.remaining_bytes>0:
                native, parent, depth, parent_path = queue.pop(0)
                if native in visited or depth >= MAX_DEPTH:
                    continue
                visited.add(native)
                for child in source.children(native):
                    if time.monotonic()>=self.deadline or self.remaining_bytes<=0:
                        break
                    cid = child['id']
                    if cid in selected_native or cid == root['native_id']:
                        continue
                    if cid in current:
                        old=current[cid]
                        try:
                            info=Path(child['rollout_path']).lstat()
                            revision=json.dumps([child.get('updated_at_ms') or child.get('updated_at'),info.st_mtime_ns,info.st_size,info.st_ino])
                        except OSError:
                            revision='missing'
                        self.revisions[old['id']]=revision
                        if old['status'] not in {'done','error','cancelled'} or revision!=old['native_revision']:
                            self.watch_ids.add(old['id'])
                            queue.append((cid,old['id'],old['depth'],old['agent_path']))
                        continue
                    born = seconds(child.get('created_at_ms') or child.get('created_at'))
                    if not born or born < lower or born > time.time() + 60 or active_count >= MAX_CHILDREN:
                        continue
                    ident = 'child:codex:' + hashlib.sha256((root['root_id'] + ':' + root['generation'] + ':' + cid).encode()).hexdigest()[:32]
                    row = {'id': ident, 'provider': 'codex', 'native_id': cid, 'root_id': root['root_id'], 'parent_id': parent, 'generation': root['generation'], 'depth': depth + 1,
                           'name': clean_name(child.get('agent_nickname') or child.get('agent_role'), 'Codex 子 Agent') + ' · ' + ident[-6:],
                           'agent_path': child.get('agent_path') or '', 'born': born, 'parent_run_id': tail.run, 'root_started': lower, 'status': 'waiting_user',
                           'step': '子 Agent 已建立；等待工作事件', 'run_id': None, 'updated_at': time.time(), 'terminal_at': None, 'source_path': child['rollout_path'],
                           'parent_context_run':getattr(tail,'native_root_run',None) or tail.run,'native_revision':''}
                    reader = self.pending_tails.get(ident) or ChildTail(row, native, parent_path)
                    # Verify before admitting any identity or reading its text.
                    try:
                        events = self.read(reader)
                    except (OSError,ValueError,TypeError):
                        self.pending_tails.pop(ident,None)
                        b.health['subagent_lookup_error']='subagent_source_unavailable'
                        continue
                    if reader.metadata is None:
                        if len(self.pending_tails)<MAX_CHILDREN:self.pending_tails[ident]=reader
                        continue
                    self.pending_tails.pop(ident,None)
                    row['agent_path'] = reader.metadata['agent_path']
                    self.db.execute('INSERT OR IGNORE INTO observed_agents ('+','.join(row)+') VALUES (' + ','.join('?' for _ in row) + ')', tuple(row.values()))
                    self.tails[ident] = reader
                    self.watch_ids.add(ident)
                    info=Path(row['source_path']).lstat()
                    self.revisions[ident]=json.dumps([child.get('updated_at_ms') or child.get('updated_at'),info.st_mtime_ns,info.st_size,info.st_ino])
                    current[cid] = row
                    active_count+=1
                    self.apply(row, reader, events)
                    queue.append((cid, ident, depth + 1, row['agent_path']))

    def read(self, reader):
        if reader.row['id'] in self.read_ids:
            return []
        self.read_ids.add(reader.row['id'])
        before=reader.offset
        events=reader.read(min(FILE_BYTES,self.remaining_bytes))
        self.remaining_bytes-=reader.offset-before
        return events

    def apply(self, row, reader, events):
        b = self.bridge
        root = self.db.execute('SELECT native_id FROM observed_agent_roots WHERE root_id=? AND generation=? AND active=1', (row['root_id'], row['generation'])).fetchone()
        if not root or row['root_id'] not in b.agents:
            return
        current_tail = b.tails.get(root['native_id'])
        for event in events:
            # First admission is tied to a real current parent run. A reused
            # child may get a later run only when native root_turn_id proves it.
            root = self.db.execute('SELECT native_id FROM observed_agent_roots WHERE root_id=? AND generation=? AND active=1', (row['root_id'], row['generation'])).fetchone()
            if not root or row['root_id'] not in b.agents:
                return
            current_tail = b.tails.get(root['native_id'])
            context_run = event.get('parent_run_id')
            permitted = {row['parent_context_run'] or row['parent_run_id']}
            if current_tail and current_tail.run:
                permitted.add(getattr(current_tail,'native_root_run',None) or current_tail.run)
            if context_run not in permitted:
                continue
            parent_run=(current_tail.run if current_tail and context_run==(getattr(current_tail,'native_root_run',None) or current_tail.run) else row['parent_run_id'])
            if event['kind'] == 'run':
                self.db.execute('INSERT OR IGNORE INTO observed_agent_runs VALUES (?,?,?)', (row['id'], event['run_id'], parent_run))
                b.history.record(row['id'], row['provider'], row['name'], source_kind='observed_subagent', **{k: v for k, v in event.items() if k not in {'kind', 'parent_run_id'}})
            else:
                b.history.message(row['id'], **{k: v for k, v in event.items() if k not in {'kind', 'parent_run_id'}})
                if event['phase'] == 'prompt':
                    b.history.record(row['id'], row['provider'], row['name'], event['run_id'], 'working', prompt=event['text'], at=event['at'], source_kind='observed_subagent')
        if reader.run and reader.root_run in ({row['parent_context_run'] or row['parent_run_id']} | ({getattr(current_tail,'native_root_run',None) or current_tail.run} if current_tail else set())):
            state = reader.status
            if state == 'working' and time.time() - reader.last_event > 1800:
                state = 'waiting_user'
            step = {'working': '子 Agent 正在工作', 'done': '子 Agent 已完成', 'error': '子 Agent 執行失敗', 'cancelled': '子 Agent 已中止', 'waiting_user': '來源暫無更新；未確認完成'}[state]
            parent_run=(current_tail.run if current_tail and reader.root_run==(getattr(current_tail,'native_root_run',None) or current_tail.run) else row['parent_run_id'])
            self.db.execute('UPDATE observed_agents SET status=?,step=?,run_id=?,parent_run_id=?,parent_context_run=?,updated_at=?,terminal_at=? WHERE id=?',
                            (state, step, reader.run, parent_run, reader.root_run, reader.last_event, reader.terminal_at, row['id']))
        revision=self.revisions.get(row['id'])
        if revision and reader.offset==reader.last_size and not reader.pending:
            self.db.execute('UPDATE observed_agents SET native_revision=? WHERE id=?',(revision,row['id']))

    def poll(self, force=False):
        if not force and time.monotonic() - self.last_poll < 2:
            return
        self.last_poll = time.monotonic()
        self.deadline=time.monotonic()+.5
        self.remaining_bytes=POLL_BYTES
        self.read_ids=set()
        b = self.bridge
        with b.commands.lock, b.history.lock:
            self.sync()
            try:
                b.health.pop('subagent_lookup_error', None)
                self.discover()
            except (OSError, ValueError, TypeError, sqlite3.Error):
                b.health['subagent_lookup_error'] = 'subagent_source_unavailable'
            all_rows=self.rows()
            selected_rows=[r for r in all_rows if r.get('source_kind','codex_native')=='codex_native' and (r['id'] in self.watch_ids or r['status'] not in {'done','error','cancelled'} or (r['terminal_at'] and time.time()-r['terminal_at']<=RETAIN_SECONDS))]
            for row in selected_rows[:MAX_CHILDREN]:
                if time.monotonic()>=self.deadline or self.remaining_bytes<=0:
                    break
                try:
                    reader = self.tails.get(row['id'])
                    if reader is None:
                        parent = next((r for r in all_rows if r['id'] == row['parent_id']), None)
                        root = self.db.execute('SELECT native_id FROM observed_agent_roots WHERE root_id=?', (row['root_id'],)).fetchone()
                        root_tail=b.tails.get(root['native_id'])
                        reader = ChildTail(row, parent['native_id'] if parent else root['native_id'], parent['agent_path'] if parent else getattr(root_tail,'agent_path','/root'))
                        self.tails[row['id']] = reader
                    self.apply(row, reader, self.read(reader))
                except (OSError, ValueError, TypeError, sqlite3.Error):
                    b.health['subagent_lookup_error']='subagent_source_unavailable'
                    self.db.execute("UPDATE observed_agents SET status='waiting_user',step='未能讀取子 Agent 來源；狀態未知' WHERE id=? AND status NOT IN ('done','error','cancelled')", (row['id'],))
            self.db.commit()
            retained={r['id'] for r in selected_rows[:MAX_CHILDREN]}
            self.tails={k:v for k,v in self.tails.items() if k in retained}

    def snapshot(self, roots):
        now = time.time()
        visible=[r for r in self.rows() if r['status'] not in {'done','error','cancelled'} or r['terminal_at'] is None or now-r['terminal_at']<=RETAIN_SECONDS]
        latest = self.bridge.history.latest_messages({r['id'] for r in visible})
        result = []
        for row in visible:
            if row['status'] in {'done', 'error', 'cancelled'} and row['terminal_at'] is not None and now - row['terminal_at'] > RETAIN_SECONDS:
                continue
            root = roots.get(row['root_id'])
            if not root:
                continue
            msg = latest.get(row['id'])
            if msg and msg['run_id'] != row['run_id']:
                msg = None
            result.append(dict(self.descriptor(row), state='idle' if row['status'] == 'cancelled' else row['status'],
                               step=row['step'], state_changed_at=timestamp(row['updated_at']),
                               latest_message=dict(msg, text=msg['text'][:240]) if msg else None,
                               workspace=root['workspace'], ground_desk_id=None, command_capability=dict(CAPABILITY)))
        return result
