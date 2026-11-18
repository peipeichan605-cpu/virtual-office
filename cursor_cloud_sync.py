"""Read-only, selected-Cloud-Agent public conversation observation.

Discovery never lists an account or launches work. Membership generations fence
in-flight reads; durable pagination and native run IDs separate App work from
Office commands. SSE replay uses stable segment identities, not append-only text.
"""
import datetime as dt
import hashlib
import http.client
import sqlite3
import threading
import time
import urllib.parse
import uuid

from history import redact, timestamp
from platform_adapters import CURSOR_RUN, RemoteError, cursor_agent_id, sse_events

ACTIVE = {'CREATING', 'QUEUED', 'RUNNING', 'WAITING_FOR_BACKGROUND_WORK'}
TERMINAL = {'FINISHED': 'completed', 'ERROR': 'failed', 'CANCELLED': 'cancelled', 'EXPIRED': 'failed'}


def source_time(value):
    if not isinstance(value, str):
        raise RemoteError('cursor_cloud_invalid_timestamp')
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(dt.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    except ValueError:
        raise RemoteError('cursor_cloud_invalid_timestamp')


def public_prompt(value, native):
    """The native stream user-message schema; no arbitrary nested text search."""
    if value.get('type') != 'user-message-appended':
        return None
    message = value.get('userMessage')
    if (isinstance(message, dict) and message.get('type') == 'user_message'
            and message.get('session_id') == native and isinstance(message.get('text'), str)):
        return message['text']
    return None


class CursorCloudSync:
    interval = 3.0
    page_size = 50
    pages_per_tick = 2
    max_workers = 4

    def __init__(self, bridge, enabled=True):
        self.bridge, self.enabled = bridge, enabled
        self.stop = threading.Event()
        self.workers, self.connections, self.worker_stops = {}, {}, {}
        self.lock = threading.RLock()
        self.next_poll, self.failures = {}, {}
        self.retry_after, self.run_failures = {}, {}
        self.terminal_seen = {}
        h = bridge.history
        with bridge.commands.lock, h.lock, h.db:
            h.db.execute('''CREATE TABLE IF NOT EXISTS cursor_cloud_members (
                agent_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, generation TEXT NOT NULL,
                active INTEGER NOT NULL, joined_at TEXT NOT NULL, watermark TEXT NOT NULL,
                scan_cursor TEXT, scan_head TEXT, last_ok REAL, error TEXT)''')
            h.db.execute('''CREATE TABLE IF NOT EXISTS cursor_cloud_runs (
                agent_id TEXT NOT NULL, generation TEXT NOT NULL, native_run TEXT NOT NULL,
                local_run TEXT NOT NULL, created_at TEXT NOT NULL, native_status TEXT NOT NULL,
                origin TEXT NOT NULL, complete INTEGER NOT NULL DEFAULT 0, error TEXT,
                PRIMARY KEY(agent_id,generation,native_run))''')
        self.sync_members()

    def selected(self):
        b = self.bridge
        result = {}
        for definition in b.config.get('agents', []):
            binding = b.commands.binding(definition['id']) or {}
            if binding.get('adapter') != 'cursor_cloud' or definition.get('provider') != 'cursor':
                continue
            try:
                sid = cursor_agent_id(binding['session_id'])
            except (KeyError, ValueError):
                continue
            result[definition['id']] = sid
        return result

    def sync_members(self):
        """Called at every membership mutation, including logout then rejoin."""
        b, h = self.bridge, self.bridge.history
        with b.commands.lock, h.lock, h.db:
            selected = self.selected()
            rows = {r['agent_id']: r for r in h.db.execute('SELECT * FROM cursor_cloud_members')}
            for agent, row in rows.items():
                if selected.get(agent) != row['session_id']:
                    h.db.execute('UPDATE cursor_cloud_members SET active=0 WHERE agent_id=?', (agent,))
            for agent, sid in selected.items():
                old = rows.get(agent)
                if old and old['active'] and old['session_id'] == sid:
                    continue
                at = source_time(timestamp())
                h.db.execute('INSERT OR REPLACE INTO cursor_cloud_members VALUES (?,?,?,?,?,?,?,?,?,?)',
                             (agent, sid, uuid.uuid4().hex, 1, at, at, None, None, None, None))
            current = {(r['agent_id'], r['generation']) for r in h.db.execute('SELECT * FROM cursor_cloud_members WHERE active=1')}
        with self.lock:
            self.next_poll = {k:v for k,v in self.next_poll.items() if k in selected}
            self.failures = {k:v for k,v in self.failures.items() if k in selected}
            for key, event in self.worker_stops.items():
                if (key[0], key[1]) not in current:
                    event.set()
                    connection = self.connections.get(key)
                    if connection:
                        try:
                            connection.close()
                        except OSError:
                            pass

    def valid(self, agent, generation):
        row = self.bridge.history.db.execute('SELECT * FROM cursor_cloud_members WHERE agent_id=?', (agent,)).fetchone()
        return bool(not self.stop.is_set() and row and row['active'] and row['generation'] == generation
                    and self.selected().get(agent) == row['session_id'])

    def describe(self, agent):
        if not self.enabled or self.stop.is_set():
            return None
        h = self.bridge.history
        with self.bridge.commands.lock, h.lock:
            row = h.db.execute('SELECT * FROM cursor_cloud_members WHERE agent_id=? AND active=1', (agent,)).fetchone()
            if not row or not self.enabled or self.stop.is_set():
                return None
            fresh = row['last_ok'] is not None and time.time() - row['last_ok'] < 30 and not row['error']
            return {'state': 'configured' if fresh else 'recovering' if row['last_ok'] else 'connecting',
                    'active': fresh, 'reason': None if fresh else row['error'] or 'cursor_cloud_sync_connecting'}

    def decorate_run(self, item):
        if not item:
            return item
        h = self.bridge.history
        with self.bridge.commands.lock, h.lock:
            row = h.db.execute('SELECT error FROM cursor_cloud_runs WHERE agent_id=? AND local_run=? ORDER BY rowid DESC LIMIT 1',
                               (item['agent_id'],item['run_id'])).fetchone()
        if row and row['error'] in {'cursor_cloud_prompt_unavailable','cursor_cloud_transcript_gap'}:
            return dict(item, sync_warning=row['error'])
        return item

    def busy(self, agent):
        h = self.bridge.history
        with self.bridge.commands.lock, h.lock:
            return bool(h.db.execute('''SELECT 1 FROM cursor_cloud_runs r JOIN cursor_cloud_members m
                ON m.agent_id=r.agent_id AND m.generation=r.generation WHERE m.active=1
                AND r.agent_id=? AND r.origin='native' AND r.complete=0
                AND r.native_status IN ('CREATING','QUEUED','RUNNING','WAITING_FOR_BACKGROUND_WORK') LIMIT 1''', (agent,)).fetchone())

    def classify(self, agent, native, created_at=None):
        commands = self.bridge.commands
        row = commands.db.execute("SELECT id FROM commands WHERE agent_id=? AND adapter='cursor_cloud' AND native_message_id=?", (agent, native)).fetchone()
        if row:
            return 'office'
        sending = commands.db.execute("SELECT 1 FROM commands WHERE agent_id=? AND adapter='cursor_cloud' AND state='sending' AND native_message_id IS NULL LIMIT 1", (agent,)).fetchone()
        # During POST the accepted callback can still assign the native ID.
        # Older uncertain attempts are not proof of ownership: show unmatched
        # native work with neutral provenance, never reconcile the command.
        return 'deferred' if sending else 'native'

    def discover(self, member, adapter):
        b, h = self.bridge, self.bridge.history
        agent, generation, sid = member['agent_id'], member['generation'], member['session_id']
        cursor, head, floor = member['scan_cursor'], member['scan_head'], member['watermark']
        for _ in range(self.pages_per_tick):
            if self.stop.is_set():return
            query = {'limit': self.page_size}
            if cursor:
                query['cursor'] = cursor
            page = adapter.transport.json('GET', adapter.base + sid + '/runs?' + urllib.parse.urlencode(query), adapter.headers(sid))
            items = page.get('items')
            if not isinstance(items, list) or len(items) > 100:
                raise RemoteError('cursor_cloud_invalid_run_page')
            checked = []
            for row in items:
                if (not isinstance(row, dict) or row.get('agentId') != sid or not isinstance(row.get('id'), str)
                        or not CURSOR_RUN.fullmatch(row['id']) or row.get('status') not in ACTIVE | set(TERMINAL)):
                    raise RemoteError('cursor_cloud_invalid_run_page')
                checked.append((row, source_time(row.get('createdAt'))))
            next_cursor = page.get('nextCursor')
            if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor or len(next_cursor) > 4096 or next_cursor == cursor):
                raise RemoteError('cursor_cloud_invalid_page_cursor')
            if checked:
                head = max(head or floor, *(at for _, at in checked))
            done = not next_cursor or any(at < floor for _, at in checked)
            with b.commands.lock, h.lock, h.db:
                if not self.valid(agent, generation):
                    return
                for row, at in checked:
                    if at < floor and row['status'] not in ACTIVE:
                        continue
                    native = row['id']
                    origin = self.classify(agent, native, at)
                    local = 'cloud-' + hashlib.sha256((sid + '\0' + native).encode()).hexdigest()[:40]
                    h.db.execute('''INSERT INTO cursor_cloud_runs
                        (agent_id,generation,native_run,local_run,created_at,native_status,origin)
                        VALUES (?,?,?,?,?,?,?) ON CONFLICT(agent_id,generation,native_run)
                        DO UPDATE SET native_status=excluded.native_status''',
                        (agent, generation, native, local, at, row['status'], origin))
                h.db.execute('''UPDATE cursor_cloud_members SET scan_cursor=?,scan_head=?,watermark=?,last_ok=?,error=NULL
                    WHERE agent_id=? AND generation=?''',
                    (None if done else next_cursor, None if done else head, (head or floor) if done else floor, time.time(), agent, generation))
            if done:
                break
            cursor = next_cursor

    def poll_once(self):
        if not self.enabled or self.stop.is_set():
            return
        self.sync_members()
        b, h = self.bridge, self.bridge.history
        adapter = b.commands.adapters.get('cursor_cloud')
        if not adapter:
            return
        with b.commands.lock, h.lock:
            members = [dict(r) for r in h.db.execute('SELECT * FROM cursor_cloud_members WHERE active=1')]
        for member in members:
            if self.stop.is_set():return
            agent = member['agent_id']
            if time.monotonic() < self.next_poll.get(agent, 0):
                continue
            try:
                if not adapter.available(member['session_id']):
                    raise RemoteError('cursor_cloud_credentials_unavailable')
                self.discover(member, adapter)
                self.reconcile_terminal_streams(member, adapter)
                self.failures[agent] = 0
                self.next_poll[agent] = time.monotonic() + self.interval
            except (RemoteError, OSError, KeyError, ValueError, sqlite3.Error):
                self.failures[agent] = min(self.failures.get(agent, 0) + 1, 5)
                self.next_poll[agent] = time.monotonic() + min(60, self.interval * 2 ** self.failures[agent])
                with b.commands.lock, h.lock, h.db:
                    if self.valid(agent, member['generation']):
                        h.db.execute('UPDATE cursor_cloud_members SET error=? WHERE agent_id=?', ('cursor_cloud_sync_unavailable', agent))
        self.launch_pending(adapter)

    def launch_pending(self, adapter):
        b, h = self.bridge, self.bridge.history
        with b.commands.lock, h.lock:
            rows = [dict(r) for r in h.db.execute('''SELECT r.*,m.session_id FROM cursor_cloud_runs r
                JOIN cursor_cloud_members m ON m.agent_id=r.agent_id AND m.generation=r.generation
                WHERE m.active=1 AND r.complete=0 AND r.origin!='office' ORDER BY
                CASE WHEN r.native_status IN ('CREATING','QUEUED','RUNNING','WAITING_FOR_BACKGROUND_WORK') THEN 0 ELSE 1 END,
                r.created_at DESC,r.native_run''')]
            for row in rows:
                origin = self.classify(row['agent_id'], row['native_run'], row['created_at'])
                h.db.execute('UPDATE cursor_cloud_runs SET origin=? WHERE agent_id=? AND generation=? AND native_run=?',
                             (origin, row['agent_id'], row['generation'], row['native_run']))
                row['origin'] = origin
            h.db.commit()
        with self.lock:
            pending = {(r['agent_id'], r['generation'], r['native_run']) for r in rows if r['origin'] == 'native'}
            for key, worker in list(self.workers.items()):
                if not worker.is_alive():
                    self.workers.pop(key, None)
                    self.worker_stops.pop(key, None)
                    if key not in pending:
                        self.retry_after.pop(key, None)
                        self.run_failures.pop(key, None)
                        self.terminal_seen.pop(key, None)
            for row in rows:
                if row['origin'] != 'native':
                    continue
                key = (row['agent_id'], row['generation'], row['native_run'])
                live = [key for key, worker in self.workers.items() if worker.is_alive()]
                if len(live) >= self.max_workers:
                    break
                if any(other[0] == row['agent_id'] for other in live):
                    continue
                # Failed reads back off with discovery; no hot retry stream loop.
                if time.monotonic() < self.retry_after.get(key, 0) or self.failures.get(row['agent_id'], 0):
                    continue
                event = threading.Event()
                worker = threading.Thread(target=self.observe, args=(row, adapter, event), daemon=True)
                self.workers[key], self.worker_stops[key] = worker, event
                worker.start()

    def project(self, row, kind, **fields):
        b, h = self.bridge, self.bridge.history
        agent, generation, native, local = (row[k] for k in ('agent_id','generation','native_run','local_run'))
        event = {'type': kind, 'run_id': local, 'native_run_id': native, 'adapter': 'cursor_cloud', **fields}
        with b.commands.lock, h.lock:
            if not self.valid(agent, generation) or self.classify(agent, native, row['created_at']) != 'native':
                return False
            ledger = h.db.execute('SELECT complete FROM cursor_cloud_runs WHERE agent_id=? AND generation=? AND native_run=?',
                                  (agent,generation,native)).fetchone()
            if not ledger or (ledger['complete'] and kind != 'prompt'):
                return False
            definition = b.definitions.get(agent)
            if not definition:
                return False
            state = fields.get('state')
            status = {'completed':'done','failed':'error','cancelled':'cancelled','delivery_unknown':'unknown'}.get(state, 'working')
            # History methods use their own RLock and stable identities. Any
            # interrupted projection safely replays before ledger completion.
            if kind == 'prompt':
                h.record(agent, 'cursor', definition['name'], local, 'working', prompt=fields['text'],
                         source_kind='cursor_cloud_native', source_ref=native, started_at=row['created_at'])
                h.message(agent, local, 'user', 'prompt', fields['text'], at=row['created_at'], event_id='cursor-cloud:'+native+':prompt:'+str(fields.get('index',0)))
            elif kind == 'text':
                final = fields.get('final', False)
                if not final:
                    # Reopening a run stream replays from its beginning. Keep
                    # the already-visible revision until replay catches up;
                    # otherwise every reconnect shrinks/retypes old bubbles.
                    event_id = 'hermes:%s:text:%s' % (local, fields['segment'])
                    previous = h.db.execute('SELECT text,phase FROM messages WHERE agent_id=? AND event_id=?',
                                            (agent,event_id)).fetchone()
                    clean = redact(fields['text'], h.secrets)
                    if previous and (previous['phase'] == 'final' or previous['text'].startswith(clean)):
                        return True
                h.stream_message(agent, local, fields['segment'], fields['text'], final)
            elif kind in {'started','finished','interrupted'}:
                h.record(agent, 'cursor', definition['name'], local, status,
                         delivery=fields.get('text') if kind == 'finished' else None,
                         source_kind='cursor_cloud_native', source_ref=native, started_at=row['created_at'])
                with h.db:
                    h.db.execute('UPDATE cursor_cloud_runs SET complete=?,error=? WHERE agent_id=? AND generation=? AND native_run=?',
                                 (int(kind == 'finished'), fields.get('detail_code'), agent, generation, native))
            # Older recovery remains in history but cannot hide a newer turn.
            newest = h.db.execute('SELECT run_id FROM runs WHERE agent_id=? ORDER BY started_at DESC,id DESC LIMIT 1', (agent,)).fetchone()
            newer_discovered = h.db.execute('''SELECT 1 FROM cursor_cloud_runs WHERE agent_id=?
                AND generation=? AND created_at>? LIMIT 1''', (agent,generation,row['created_at'])).fetchone()
            if newest and newest['run_id'] == local and not newer_discovered and kind in {'started','status','finished','interrupted'}:
                b.native_status(agent, event)
        return True

    def finish_snapshot(self, row, value, text_key='result', segment=0, stream_gap=False):
        """Finish only from an exact-run authoritative result, once per ledger."""
        native, sid = row['native_run'], row['session_id']
        if value.get('runId', value.get('id')) != native or value.get('agentId', sid) != sid:
            raise RemoteError('cursor_run_mismatch')
        state = TERMINAL.get(value.get('status'))
        if not state:
            return False
        b, h = self.bridge, self.bridge.history
        with b.commands.lock, h.lock:
            if not self.valid(row['agent_id'],row['generation']):
                return False
            ledger = h.db.execute('SELECT complete FROM cursor_cloud_runs WHERE agent_id=? AND generation=? AND native_run=?',
                                  (row['agent_id'],row['generation'],native)).fetchone()
            if not ledger:
                return False
            if ledger['complete']:
                return True
            saved = h.db.execute("SELECT event_id FROM messages WHERE agent_id=? AND run_id=? AND role='assistant'",
                                 (row['agent_id'],row['local_run'])).fetchall()
            old_segments = [int(r[0].rsplit(':',1)[-1]) for r in saved
                            if r[0].startswith('hermes:'+row['local_run']+':text:') and r[0].rsplit(':',1)[-1].isdigit()]
            segment = max([segment] + old_segments)
            had_prompt = h.db.execute("SELECT 1 FROM messages WHERE agent_id=? AND run_id=? AND role='user' LIMIT 1",
                                      (row['agent_id'],row['local_run'])).fetchone()
            text = value.get(text_key) if isinstance(value.get(text_key), str) else ''
            if text:
                self.project(row,'text',text=text,segment=segment,final=True)
            return self.project(row,'finished',state=state,text=text,
                detail_code='cursor_cloud_prompt_unavailable' if not had_prompt else
                'cursor_cloud_transcript_gap' if stream_gap else
                None if text or state != 'completed' else 'completed_without_reply')

    def reconcile_terminal_streams(self, member, adapter):
        """An open SSE connection must not hold a confirmed terminal run busy.

        Give normal final frames one poll interval to arrive. Then verify the
        exact run by GET; the discovery page alone never completes delivery.
        """
        b, h = self.bridge, self.bridge.history
        with b.commands.lock, h.lock:
            if not self.valid(member['agent_id'],member['generation']):
                return
            rows = [dict(r) for r in h.db.execute("SELECT * FROM cursor_cloud_runs "
                "WHERE agent_id=? AND generation=? AND origin='native' AND complete=0 "
                "AND native_status IN ('FINISHED','ERROR','CANCELLED','EXPIRED')",
                (member['agent_id'],member['generation']))]
        for row in rows:
            key = (row['agent_id'],row['generation'],row['native_run'])
            with self.lock:
                worker = self.workers.get(key)
                if not worker or not worker.is_alive() or key not in self.connections:
                    continue
                first = self.terminal_seen.setdefault(key,time.monotonic())
                elapsed = time.monotonic() - first
            if elapsed < self.interval:
                continue
            with b.commands.lock, h.lock:
                had_prompt = h.db.execute("SELECT 1 FROM messages WHERE agent_id=? AND run_id=? AND role='user' LIMIT 1",
                                          (row['agent_id'],row['local_run'])).fetchone()
            if not had_prompt and elapsed < 15:
                continue
            row['session_id'] = member['session_id']
            value = adapter.transport.json('GET',adapter.base+row['session_id']+'/runs/'+row['native_run'],
                                           adapter.headers(row['session_id']))
            if self.finish_snapshot(row,value,stream_gap=True):
                with self.lock:
                    event = self.worker_stops.get(key)
                    if event:
                        event.set()
                    connection = self.connections.get(key)
                    if connection:
                        try:
                            connection.close()
                        except OSError:
                            pass

    def observe(self, row, adapter, stop):
        key = (row['agent_id'], row['generation'], row['native_run'])
        sid, native = row['session_id'], row['native_run']
        url = adapter.base + sid + '/runs/' + native
        text, segment, connection, started = '', 0, None, False
        prompt_index, succeeded, stream_gap = 0, False, False

        def begin():
            nonlocal started
            if not started:
                started = self.project(row, 'started')
            return started

        def finish(value, text_key):
            nonlocal succeeded
            succeeded = self.finish_snapshot(row, value, text_key, segment, stream_gap)
            return succeeded

        try:
            try:
                connection, response = adapter.transport.open('GET', url + '/stream',
                    {**adapter.headers(sid), 'Accept': 'text/event-stream'})
                with self.lock:
                    self.connections[key] = connection
                for kind, value in sse_events(response, stop, duration=120):
                    if self.stop.is_set() or stop.is_set():
                        return
                    if (('runId' in value and value['runId'] != native)
                            or ('agentId' in value and value['agentId'] != sid)):
                        raise RemoteError('cursor_run_mismatch')
                    if kind == 'interaction_update':
                        prompt = public_prompt(value, native)
                        if prompt is not None:
                            self.project(row, 'prompt', text=prompt, index=prompt_index)
                            prompt_index += 1
                    elif kind == 'status':
                        if value.get('runId') != native:
                            raise RemoteError('cursor_run_mismatch')
                        if value.get('status') in ACTIVE:
                            begin()
                    elif kind == 'assistant' and isinstance(value.get('text'), str):
                        begin()
                        text = (text + value['text'])[:256000]
                        self.project(row, 'text', text=text, segment=segment)
                    elif kind == 'tool_call':
                        begin()
                        if value.get('status') == 'running':
                            if text:
                                segment += 1
                                text = ''
                            self.project(row, 'status', step='Cursor 正在執行工具')
                    elif kind == 'result' and finish(value, 'text'):
                        return
                    elif kind in {'done','error'}:
                        break
                stream_gap = True
            except (RemoteError, OSError, http.client.HTTPException) as error:
                if isinstance(error, RemoteError) and error.code == 'cursor_run_mismatch':
                    raise
                stream_gap = True
            if not self.stop.is_set() and not stop.is_set():
                value = adapter.transport.json('GET', url, adapter.headers(sid))
                if finish(value, 'result'):
                    return
                begin()
                raise RemoteError('cursor_cloud_observation_interrupted')
        except Exception:
            if not self.stop.is_set() and not stop.is_set():
                self.project(row, 'interrupted', state='delivery_unknown', detail_code='cursor_cloud_observation_interrupted')
        finally:
            if connection:
                try:
                    connection.close()
                except OSError:
                    pass
            with self.lock:
                self.connections.pop(key, None)
                if succeeded:
                    self.retry_after.pop(key, None)
                    self.run_failures.pop(key, None)
                elif not stop.is_set() and not self.stop.is_set():
                    failures = min(5, self.run_failures.get(key, 0) + 1)
                    self.run_failures[key] = failures
                    self.retry_after[key] = time.monotonic() + min(60, self.interval * 2 ** failures)

    def run(self, stop):
        while not stop.is_set() and not self.stop.is_set():
            try:
                self.poll_once()
            except (OSError, sqlite3.Error):
                pass
            stop.wait(0.5)

    def close(self):
        self.stop.set()
        with self.lock:
            for event in self.worker_stops.values():
                event.set()
            for connection in list(self.connections.values()):
                try:
                    connection.close()
                except OSError:
                    pass
            workers = list(self.workers.values())
        for worker in workers:
            if worker.is_alive():
                worker.join(timeout=16)
