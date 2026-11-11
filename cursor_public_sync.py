"""Scoped Cursor public-bubble import; native Hooks retain lifecycle authority.

This reader never starts an Agent or completes a run. Only accepted native root
or Task Starts create bindings. Public imports and their checkpoints commit together in
the OFFICE history DB, independently of Cursor's read-only storage snapshot.
"""
import hashlib
import json
import re
import time

from history import redact, timestamp

TERMINAL = {'done', 'error', 'cancelled'}
IDENT = re.compile(r'[A-Za-z0-9_.:@/-]{1,240}\Z')
LAG_SECONDS = 120
MAX_TARGETS = 16
MAX_MESSAGES = 256


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def bubble_identity(value):
    if not isinstance(value, str) or not value or '\0' in value or len(value) > 512:
        return False
    try:
        return len(value.encode('utf-8', errors='strict')) <= 512
    except UnicodeEncodeError:
        return False


class CursorPublicSync:
    def __init__(self, events, store=None):
        self.events = events
        self.bridge = events.bridge
        self.store = store
        self.last_poll = 0
        self.cursor = 0
        # Fixtures never open a user's real Cursor storage accidentally.
        self.enabled = store is not None or not getattr(self.bridge, 'dry_run', False) or bool(self.bridge.config.get('cursor_public_db'))
        with self.bridge.commands.lock, self.bridge.history.lock, self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS cursor_public_runs (
                observer_id TEXT NOT NULL,generation TEXT NOT NULL,child_id TEXT NOT NULL,
                run_id TEXT NOT NULL,native_run_id TEXT NOT NULL,parent_run_id TEXT NOT NULL,
                task_call_id TEXT NOT NULL,conversation_id TEXT NOT NULL DEFAULT '',
                boundary_id TEXT NOT NULL DEFAULT '',final_fingerprint TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(observer_id,generation,child_id,run_id))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS cursor_public_messages (
                observer_id TEXT NOT NULL,generation TEXT NOT NULL,child_id TEXT NOT NULL,
                run_id TEXT NOT NULL,bubble_id TEXT NOT NULL,event_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,phase TEXT NOT NULL,ordinal INTEGER NOT NULL,
                PRIMARY KEY(observer_id,generation,child_id,run_id,bubble_id))''')
            self.db.execute('CREATE INDEX IF NOT EXISTS cursor_public_message_run ON cursor_public_messages(child_id,run_id,phase)')
            self.db.execute('''CREATE TABLE IF NOT EXISTS cursor_public_root_runs (
                observer_id TEXT NOT NULL,generation TEXT NOT NULL,root_id TEXT NOT NULL,
                run_id TEXT NOT NULL,native_conversation TEXT NOT NULL,start_event_id TEXT NOT NULL,
                boundary_id TEXT NOT NULL DEFAULT '',final_fingerprint TEXT NOT NULL DEFAULT '',
                native_final_fingerprint TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(observer_id,generation,root_id,run_id))''')
            if 'native_final_fingerprint' not in {row[1] for row in self.db.execute('PRAGMA table_info(cursor_public_root_runs)')}:
                self.db.execute("ALTER TABLE cursor_public_root_runs ADD COLUMN native_final_fingerprint TEXT NOT NULL DEFAULT ''")
            self.db.execute('''CREATE TABLE IF NOT EXISTS cursor_public_root_callbacks (
                observer_id TEXT NOT NULL,generation TEXT NOT NULL,root_id TEXT NOT NULL,
                run_id TEXT NOT NULL,event_id TEXT NOT NULL,fingerprint TEXT NOT NULL,phase TEXT NOT NULL,
                bubble_id TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(observer_id,generation,root_id,run_id,event_id))''')
            if 'bubble_id' not in {row[1] for row in self.db.execute('PRAGMA table_info(cursor_public_root_callbacks)')}:
                self.db.execute("ALTER TABLE cursor_public_root_callbacks ADD COLUMN bubble_id TEXT NOT NULL DEFAULT ''")

    @property
    def db(self):
        return self.bridge.history.db

    def track(self, scope, child_id, data, run_id):
        """Called in the accepted Start's existing OFFICE transaction."""
        alias = data.get('native_alias')
        if (scope['provider'] != 'cursor' or scope['source_kind'] != 'cursor_hook'
                or data['type'] != 'started' or not alias or alias['kind'] != 'tool_call'):
            return
        values = (scope['scope'], scope['generation'], child_id, run_id,
                  data['child_run_id'], data['parent_run_id'], alias['id'])
        old = self.db.execute('''SELECT native_run_id,parent_run_id,task_call_id FROM cursor_public_runs
            WHERE observer_id=? AND generation=? AND child_id=? AND run_id=?''', values[:4]).fetchone()
        if old and tuple(old) != values[4:]:
            raise ValueError('cursor_public_run_conflict')
        self.db.execute('''INSERT OR IGNORE INTO cursor_public_runs
            (observer_id,generation,child_id,run_id,native_run_id,parent_run_id,task_call_id)
            VALUES (?,?,?,?,?,?,?)''', values)

    def track_root(self, scope, root_id, data):
        """Stage authenticated root callback provenance in its ingest transaction.

        Only Start creates a target. The caller stages this before accepting
        the event; any run/receipt conflict rolls it back. Duplicate receipts
        never backfill an old scope or claim another public bubble.
        """
        if (scope['provider']!='cursor' or scope['source_kind']!='cursor_hook'
                or scope['evidence']!='native_hook'
                or scope['native_parent_id'].startswith('pending:')):
            return
        identity=(scope['scope'],scope['generation'],root_id,data['run_id'])
        phase=('final' if data['type']=='turn_completed' else data.get('phase')
            if data['type']=='message' and data.get('role')=='assistant' else None)
        clean=redact(data.get('text',''),self.bridge.history.secrets) if phase in {'commentary','final'} else ''
        if clean:
            self.db.execute('''INSERT OR IGNORE INTO cursor_public_root_callbacks
                (observer_id,generation,root_id,run_id,event_id,fingerprint,phase) VALUES (?,?,?,?,?,?,?)''',
                (*identity,data['event_id'],digest(clean),phase))
        if data['type']!='turn_started':
            final=(data['type']=='turn_completed' or data['type']=='message'
                and data.get('role')=='assistant' and data.get('phase')=='final')
            clean=redact(data.get('text',''),self.bridge.history.secrets) if final else ''
            if clean:
                self.db.execute('''UPDATE cursor_public_root_runs SET native_final_fingerprint=?
                    WHERE observer_id=? AND generation=? AND root_id=? AND run_id=?
                        AND native_conversation=?''',(digest(clean),*identity,scope['native_parent_id']))
            return
        values=(scope['scope'],scope['generation'],root_id,data['run_id'],
                scope['native_parent_id'],data['event_id'])
        old=self.db.execute('''SELECT native_conversation,start_event_id FROM cursor_public_root_runs
            WHERE observer_id=? AND generation=? AND root_id=? AND run_id=?''',values[:4]).fetchone()
        if old and tuple(old)!=values[4:]:raise ValueError('cursor_public_root_run_conflict')
        self.db.execute('''INSERT OR IGNORE INTO cursor_public_root_runs
            (observer_id,generation,root_id,run_id,native_conversation,start_event_id)
            VALUES (?,?,?,?,?,?)''',values)

    def _root_targets(self, now):
        rows=self.db.execute('''SELECT p.*,p.root_id AS child_id,p.native_conversation AS root_native,
                h.status,h.delivery AS native_delivery,h.finished_at
            FROM cursor_public_root_runs p
            JOIN observed_agent_roots r ON r.root_id=p.root_id AND r.generation=p.generation
            JOIN child_observers o ON o.id=p.observer_id AND o.root_id=p.root_id AND o.generation=p.generation
            JOIN live_runs l ON l.agent_id=p.root_id AND l.run_id=p.run_id
            JOIN runs h ON h.agent_id=p.root_id AND h.run_id=p.run_id AND h.status=l.status
            JOIN event_receipts e ON e.agent_id=p.root_id AND e.event_id=p.start_event_id AND e.run_id=p.run_id
            WHERE r.active=1 AND o.active=1 AND o.provider='cursor'
                AND o.source_kind='cursor_hook' AND o.evidence='native_hook'
                AND o.native_session_id=p.native_conversation AND o.native_session_id NOT LIKE 'pending:%'
                AND (h.status='working' OR h.status IN ('done','error','cancelled') AND
                    julianday(h.finished_at)>=julianday(?))
            ORDER BY p.root_id LIMIT 128''',(timestamp(now-LAG_SECONDS),)).fetchall()
        targets=[]
        for value in rows:
            row=dict(value)
            root=self.bridge.definitions.get(row['root_id'])
            if not root or root.get('provider')!='cursor' or root.get('driver')!='push':continue
            row['target_kind']='root'
            targets.append(row)
        return targets

    def _current_root(self, target, now):
        return next((row for row in self._root_targets(now) if all(row[key]==target[key] for key in
            ('observer_id','generation','root_id','run_id','native_conversation','start_event_id'))),None)

    def suppress_root_callback_projection(self, scope, root_id, data):
        """Keep a proven bubble's visible identity when its callback arrives later.

        Called after native ingestion while the same history lock is held. The
        accepted receipt/lifecycle remain intact, but API readers never see a
        second commentary identity for the same exact root/run public body.
        """
        if (scope['provider']!='cursor' or scope['source_kind']!='cursor_hook'
                or data['type']!='message' or data.get('role')!='assistant'
                or data.get('phase')!='commentary'):return
        clean=redact(data.get('text',''),self.bridge.history.secrets)
        identity=(scope['scope'],scope['generation'],root_id,data['run_id'])
        prior=self.db.execute('''SELECT bubble_id FROM cursor_public_root_callbacks
            WHERE observer_id=? AND generation=? AND root_id=? AND run_id=? AND event_id=?''',
            (*identity,data['event_id'])).fetchone()
        if prior and prior['bubble_id']:
            claimed=" AND p.bubble_id=?"
            extra=(prior['bubble_id'],)
        else:
            claimed=''' AND NOT EXISTS (SELECT 1 FROM cursor_public_root_callbacks c
                WHERE c.observer_id=p.observer_id AND c.generation=p.generation AND c.root_id=p.child_id
                    AND c.run_id=p.run_id AND c.bubble_id=p.bubble_id AND c.fingerprint=p.fingerprint)'''
            extra=()
        canonical=self.db.execute('''SELECT p.event_id,p.bubble_id FROM cursor_public_messages p
            JOIN messages m ON m.agent_id=p.child_id AND m.run_id=p.run_id AND m.event_id=p.event_id
            WHERE p.observer_id=? AND p.generation=? AND p.child_id=? AND p.run_id=?
                AND p.fingerprint=? AND p.phase='commentary' AND m.phase='commentary'
                AND m.text=?'''+claimed+' ORDER BY p.ordinal LIMIT 1',
            (*identity,digest(clean),clean,*extra)).fetchone()
        if canonical and canonical['event_id']!=data['event_id']:
            self.db.execute('''UPDATE cursor_public_root_callbacks SET bubble_id=?
                WHERE observer_id=? AND generation=? AND root_id=? AND run_id=? AND event_id=?''',
                (canonical['bubble_id'],*identity,data['event_id']))
            self.db.execute("DELETE FROM messages WHERE agent_id=? AND run_id=? AND event_id=? "
                "AND role='assistant' AND phase='commentary' AND text=?",
                (root_id,data['run_id'],data['event_id'],clean))

    def _root_message(self, row, bubble, clean, phase, ordinal):
        """Associate each verified bubble with whichever projection appeared first."""
        key=(row['observer_id'],row['generation'],row['root_id'],row['run_id'],bubble['id'])
        signature=digest(clean)
        prior=self.db.execute('''SELECT fingerprint,phase,event_id FROM cursor_public_messages
            WHERE observer_id=? AND generation=? AND child_id=? AND run_id=? AND bubble_id=?''',key).fetchone()
        canonical=prior['event_id'] if prior else None
        if canonical is None:
            native=self.db.execute('''SELECT m.event_id FROM cursor_public_root_callbacks c
                JOIN event_receipts e ON e.agent_id=c.root_id AND e.run_id=c.run_id AND e.event_id=c.event_id
                JOIN messages m ON m.agent_id=c.root_id AND m.run_id=c.run_id AND m.event_id=c.event_id
                WHERE c.observer_id=? AND c.generation=? AND c.root_id=? AND c.run_id=?
                    AND c.fingerprint=? AND c.bubble_id='' AND m.role='assistant' AND m.phase=? AND m.text=?
                    AND NOT EXISTS (SELECT 1 FROM cursor_public_messages p WHERE
                        p.observer_id=c.observer_id AND p.generation=c.generation
                        AND p.child_id=c.root_id AND p.run_id=c.run_id AND p.event_id=m.event_id)
                ORDER BY m.seq LIMIT 1''',(*key[:4],signature,phase,clean)).fetchone()
            canonical=native['event_id'] if native else 'cursor-public:'+digest(json.dumps(key))
        self.db.execute('''UPDATE cursor_public_root_callbacks SET bubble_id=? WHERE observer_id=?
            AND generation=? AND root_id=? AND run_id=? AND event_id=? AND bubble_id='' ''',
            (bubble['id'],*key[:4],canonical))
        existing=self.db.execute('SELECT text,phase FROM messages WHERE agent_id=? AND run_id=? AND event_id=?',
            (row['root_id'],row['run_id'],canonical)).fetchone()
        if not existing or existing['text']!=clean or existing['phase']!=phase:
            self.db.execute('DELETE FROM messages WHERE agent_id=? AND event_id=?',(row['root_id'],canonical))
            self.bridge.history.message(row['root_id'],row['run_id'],'assistant',phase,clean,
                at=bubble.get('at'),event_id=canonical,commit=False)
        self.db.execute('''INSERT INTO cursor_public_messages VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(observer_id,generation,child_id,run_id,bubble_id) DO UPDATE SET
            event_id=excluded.event_id,fingerprint=excluded.fingerprint,phase=excluded.phase,ordinal=excluded.ordinal''',
            (*key,canonical,signature,phase,ordinal))

    def _targets(self, now):
        rows = self.db.execute('''SELECT p.*,a.root_id,a.parent_id,a.status,a.suppressed,
                a.terminal_at,a.updated_at AS source_at,o.native_session_id AS root_native
            FROM cursor_public_runs p JOIN observed_agents a ON a.id=p.child_id
            JOIN observed_agent_roots r ON r.root_id=a.root_id AND r.generation=p.generation
            JOIN child_observers o ON o.id=p.observer_id AND o.root_id=a.root_id AND o.generation=p.generation
            JOIN live_runs l ON l.agent_id=a.id AND l.run_id=p.run_id
            JOIN child_callback_runs c ON c.child_id=a.id AND c.native_run_id=p.native_run_id
                AND c.run_id=p.run_id AND c.parent_run_id=p.parent_run_id
            WHERE r.active=1 AND o.active=1 AND o.provider='cursor' AND o.source_kind='cursor_hook'
                AND a.provider='cursor' AND a.source_kind='cursor_hook' AND a.generation=p.generation
                AND a.suppressed=0 AND a.run_id=p.run_id AND a.parent_run_id=p.parent_run_id
                AND (a.status NOT IN ('done','error','cancelled') OR a.terminal_at>=?)
            ORDER BY p.child_id LIMIT 128''', (now - LAG_SECONDS,)).fetchall()
        result = []
        for value in rows:
            row = dict(value)
            if row['root_id'] not in self.bridge.definitions:
                continue
            if row['parent_id'] == row['root_id']:
                parent_native = row['root_native']
            else:
                parent = self.db.execute('''SELECT native_id FROM child_native_aliases
                    WHERE child_id=? AND root_id=? AND generation=? AND source_kind='cursor_hook'
                    AND kind='conversation' ''', (row['parent_id'], row['root_id'], row['generation'])).fetchone()
                if not parent:
                    continue
                parent_native = parent[0]
            known = self.db.execute('''SELECT native_id FROM child_native_aliases WHERE child_id=?
                AND root_id=? AND generation=? AND source_kind='cursor_hook' AND kind='conversation' ''',
                (row['child_id'], row['root_id'], row['generation'])).fetchone()
            row['parent_native'] = parent_native
            row['child_hint'] = known[0] if known else row['conversation_id'] or None
            result.append(row)
        return result

    def _current(self, target, now):
        return next((row for row in self._targets(now) if all(row[key] == target[key] for key in
                    ('observer_id', 'generation', 'child_id', 'run_id', 'native_run_id', 'task_call_id'))), None)

    def _suppress_independent(self, row, conversation):
        if not self.events._independent_root('cursor', row['root_id'], conversation):
            return False
        self.db.execute('UPDATE observed_agents SET suppressed=1 WHERE id=?', (row['child_id'],))
        return True

    def _message(self, row, bubble, clean, phase, ordinal):
        key = (row['observer_id'], row['generation'], row['child_id'], row['run_id'], bubble['id'])
        event_id = 'cursor-public:' + digest(json.dumps(key))
        signature = digest(clean)
        prior = self.db.execute('''SELECT fingerprint,phase,event_id FROM cursor_public_messages
            WHERE observer_id=? AND generation=? AND child_id=? AND run_id=? AND bubble_id=?''', key).fetchone()
        if prior and prior['fingerprint'] == signature and prior['phase'] == phase:
            return False
        native_final = self.db.execute('''SELECT 1 FROM messages WHERE agent_id=? AND run_id=?
            AND role='assistant' AND phase='final' AND text=? AND event_id NOT LIKE 'cursor-public:%'
            LIMIT 1''', (row['child_id'], row['run_id'], clean)).fetchone() if phase == 'final' and not prior else None
        # Stable native bubble identity, increasing seq, one visible row. This
        # also handles provider persistence lag without repeated full prefixes.
        if not native_final:
            self.db.execute('DELETE FROM messages WHERE agent_id=? AND event_id=?', (row['child_id'], event_id))
            self.bridge.history.message(row['child_id'], row['run_id'], 'assistant', phase, clean,
                at=bubble.get('at'), event_id=event_id, commit=False)
        self.db.execute('''INSERT INTO cursor_public_messages VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(observer_id,generation,child_id,run_id,bubble_id) DO UPDATE SET
            fingerprint=excluded.fingerprint,phase=excluded.phase,ordinal=excluded.ordinal''',
            (*key, event_id, signature, phase, ordinal))
        return True

    def _apply(self, target, snapshot, now):
        if not isinstance(snapshot, dict) or snapshot.get('complete') is not True:
            return False
        conversation, boundary = snapshot.get('conversation_id'), snapshot.get('boundary_id')
        if not isinstance(conversation, str) or not IDENT.fullmatch(conversation) or not bubble_identity(boundary):
            return False
        messages = snapshot.get('messages')
        if not isinstance(messages, list) or len(messages) > MAX_MESSAGES:
            return False
        with self.bridge.commands.lock, self.bridge.history.lock, self.db:
            row = self._current(target, now)
            if row is None:
                return True  # Membership/run changed while Cursor was read.
            if row['conversation_id'] and row['conversation_id'] != conversation or row['boundary_id'] and row['boundary_id'] != boundary:
                return False
            if self._suppress_independent(row, conversation):
                return True
            scope = {'scope': row['observer_id'], 'root_id': row['root_id'], 'provider': 'cursor',
                     'generation': row['generation'], 'native_parent_id': row['root_native'],
                     'source_kind': 'cursor_hook', 'evidence': 'native_hook'}
            # Reuse the existing native Task proof and alias exclusivity checks;
            # metadata registration does not create a public message/lifecycle.
            self.events._alias(scope, row['child_id'], {'type': 'message', 'native_alias': {
                'kind': 'conversation', 'id': conversation, 'parent_tool_call_id': row['task_call_id']}})
            identity = (row['observer_id'], row['generation'], row['child_id'], row['run_id'])
            self.db.execute('''UPDATE cursor_public_runs SET conversation_id=?,boundary_id=?
                WHERE observer_id=? AND generation=? AND child_id=? AND run_id=?''', (conversation, boundary, *identity))
            final_id = snapshot.get('final_id') if row['status'] in TERMINAL else None
            closed = []
            seen = set()
            for ordinal, bubble in enumerate(messages):
                if (not isinstance(bubble, dict) or bubble.get('closed') is not True
                        or not bubble_identity(bubble.get('id'))
                        or bubble['id'] in seen or not isinstance(bubble.get('text'), str)):
                    continue
                seen.add(bubble['id'])
                clean = redact(bubble['text'], self.bridge.history.secrets, limit=23500)[:24000]
                if clean:
                    closed.append((ordinal, bubble, clean))
            final = next(((bubble, clean) for _, bubble, clean in closed if bubble['id'] == final_id), None)
            for ordinal, bubble, clean in closed:
                self._message(row, bubble, clean, 'final' if final and bubble['id'] == final_id else 'commentary', ordinal)
            if row['status'] in TERMINAL and final:
                signature = digest(final[1])
                if row['final_fingerprint'] != signature:
                    # Enrichment only: preserve native finished_at, status,
                    # live_runs, updated activity, and original retirement.
                    self.db.execute('UPDATE runs SET delivery=?,updated_at=? WHERE agent_id=? AND run_id=?',
                                    (final[1], timestamp(), row['child_id'], row['run_id']))
                    self.db.execute('''UPDATE cursor_public_runs SET final_fingerprint=?
                        WHERE observer_id=? AND generation=? AND child_id=? AND run_id=?''', (signature, *identity))
            return True

    def _apply_root(self, target, snapshot, now):
        if not isinstance(snapshot,dict) or snapshot.get('complete') is not True:return False
        conversation,boundary=snapshot.get('conversation_id'),snapshot.get('boundary_id')
        messages=snapshot.get('messages')
        if (conversation!=target['native_conversation'] or not bubble_identity(boundary)
                or not isinstance(messages,list) or len(messages)>MAX_MESSAGES):return False
        with self.bridge.commands.lock,self.bridge.history.lock,self.db:
            row=self._current_root(target,now)
            if row is None:return True
            if row['boundary_id'] and row['boundary_id']!=boundary:return False
            identity=(row['observer_id'],row['generation'],row['root_id'],row['run_id'])
            self.db.execute('''UPDATE cursor_public_root_runs SET boundary_id=?
                WHERE observer_id=? AND generation=? AND root_id=? AND run_id=?''',(boundary,*identity))
            closed=[]
            seen=set()
            for ordinal,bubble in enumerate(messages):
                if (not isinstance(bubble,dict) or bubble.get('closed') is not True
                        or not bubble_identity(bubble.get('id')) or bubble['id'] in seen
                        or not isinstance(bubble.get('text'),str)):continue
                seen.add(bubble['id'])
                clean=redact(bubble['text'],self.bridge.history.secrets,limit=23500)[:24000]
                if clean:closed.append((ordinal,bubble,clean))
            # Stop authorizes terminal lifecycle, not a guess that the last
            # persisted progress bubble is the final answer. Only a matching
            # accepted native final body can authorize structured final phase.
            final=next(((bubble,clean) for _,bubble,clean in closed
                if row['status'] in TERMINAL and bubble['id']==snapshot.get('final_id')
                and clean==row['native_delivery'] and digest(clean)==row['native_final_fingerprint'] and clean),None)
            for ordinal,bubble,clean in closed:
                self._root_message(row,bubble,clean,'final' if final and bubble['id']==final[0]['id'] else 'commentary',ordinal)
            if final and row['final_fingerprint']!=digest(final[1]):
                # Keep the real native finish/status/activity; this checkpoint
                # is dialogue enrichment only and never emits another Stop.
                self.db.execute('''UPDATE cursor_public_root_runs SET final_fingerprint=?
                    WHERE observer_id=? AND generation=? AND root_id=? AND run_id=?''',(digest(final[1]),*identity))
            return True

    def poll(self, force=False, now=None):
        now = time.time() if now is None else now
        tick = time.monotonic()
        if not self.enabled or not force and tick - self.last_poll < 1:
            return
        self.last_poll = tick
        with self.bridge.commands.lock, self.bridge.history.lock, self.db:
            targets = self._targets(now)
            # Known independent roots are excluded before any storage read.
            targets = [row for row in targets if not row['child_hint'] or not self._suppress_independent(row, row['child_hint'])]
            targets += self._root_targets(now)
        if not targets:
            self.bridge.health.pop('cursor_public_error', None)
            return
        if self.store is None:
            try:
                from cursor_public_store import CursorPublicStore
                self.store = CursorPublicStore(self.bridge.config.get('cursor_public_db'))
            except Exception:
                self.bridge.health['cursor_public_error'] = 'cursor_public_source_unavailable'
                return
        total = len(targets)
        offset = self.cursor % total
        targets = (targets[offset:] + targets[:offset])[:MAX_TARGETS]
        deadline = time.monotonic() + .5
        failures = False
        processed = 0
        for target in targets:
            if time.monotonic() > deadline:
                break
            try:
                if target.get('target_kind')=='root':
                    snapshot=self.store.root_snapshot(target['native_conversation'],target['run_id'],
                        terminal=target['status'] in TERMINAL)
                    applied=self._apply_root(target,snapshot,time.time() if force is False else now)
                else:
                    snapshot = self.store.snapshot(target['root_native'], target['parent_native'],
                        target['parent_run_id'], target['task_call_id'], child_hint=target['child_hint'],
                        terminal=target['status'] in TERMINAL)
                    applied=self._apply(target,snapshot,time.time() if force is False else now)
                if not applied:
                    failures = True
            except Exception:
                # No native contents, database paths or exception text in health.
                failures = True
            finally:
                processed += 1
        self.cursor = (offset + processed) % total
        if failures:
            self.bridge.health['cursor_public_error'] = 'cursor_public_source_unavailable'
        else:
            self.bridge.health.pop('cursor_public_error', None)
