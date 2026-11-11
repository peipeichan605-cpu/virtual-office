"""Local redacted delivery history. Never published to ntfy."""
import datetime as dt
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import threading

TERMINAL = {'done', 'error', 'cancelled'}

def redact(value, secrets=(), limit=24000):
    text = str(value or '')[:256000]
    for secret in secrets:
        if secret:
            text = text.replace(secret, '[私人連結已遮蓋]')
    text = re.sub(r'<oai-mem-citation>.*?</oai-mem-citation>', '', text, flags=re.S)
    text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '[私鑰已遮蓋]', text, flags=re.S)
    text = re.sub(r'\b(?:sk-(?:proj-|or-v1-)?|gh[pousr]_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{12,}', '[憑證已遮蓋]', text)
    text = re.sub(r'(?i)(\b(?:authorization|api[_ -]?key|access[_ -]?token|password|passwd|secret|cookie)\b[\s"\x27]*[:=][\s"\x27]*)(?:Bearer\s+)?[^\s"\x27,;}]+', r'\1[已遮蓋]', text)
    text = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}', 'Bearer [已遮蓋]', text)
    text = re.sub(r'\bgbw-[A-Za-z0-9]{22}\b', '[私人頻道已遮蓋]', text)
    text = re.sub(r'(?i)([?&](?:token|key|secret|password|auth|signature)=)[^&#\s)]+', r'\1[已遮蓋]', text)
    text = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', '[電郵已遮蓋]', text)
    text = re.sub(r'/Users/[^/\s)]+/', '~/', text).strip()
    return text[:limit] + ('\n[內容較長，請到來源查看全文]' if len(text) > limit else '')

def timestamp(value=None):
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value / 1000 if value > 1e11 else value, dt.timezone.utc).isoformat().replace('+00:00', 'Z')
    if isinstance(value, str) and value:
        try:
            return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(dt.timezone.utc).isoformat().replace('+00:00', 'Z')
        except ValueError:
            pass
    return dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')

class HistoryStore:
    def __init__(self, path, secrets=()):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.secrets = tuple(secrets)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=3)
        os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, run_id TEXT NOT NULL,
            provider TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT, updated_at TEXT NOT NULL,
            prompt TEXT NOT NULL DEFAULT '', delivery TEXT NOT NULL DEFAULT '',
            source_ref TEXT NOT NULL DEFAULT '', source_kind TEXT NOT NULL DEFAULT '',
            UNIQUE(agent_id,run_id))""")
        self.db.execute('CREATE INDEX IF NOT EXISTS runs_agent_time ON runs(agent_id,started_at DESC)')
        self.db.execute("""CREATE TABLE IF NOT EXISTS messages (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,
            agent_id TEXT NOT NULL, run_id TEXT NOT NULL, role TEXT NOT NULL,
            phase TEXT NOT NULL, text TEXT NOT NULL, at TEXT NOT NULL,
            UNIQUE(agent_id,event_id))""")
        self.db.execute('CREATE INDEX IF NOT EXISTS messages_agent_seq ON messages(agent_id,seq)')
        self.db.execute('CREATE INDEX IF NOT EXISTS messages_run_phase_seq ON messages(agent_id,run_id,role,phase,seq)')
        self.db.execute("""CREATE TABLE IF NOT EXISTS event_receipts (
            agent_id TEXT NOT NULL, event_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
            run_id TEXT NOT NULL, PRIMARY KEY(agent_id,event_id))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS live_runs (
            agent_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, status TEXT NOT NULL,
            updated_at TEXT NOT NULL)""")
        self.db.commit()

    def message(self, agent_id, run_id, role, phase, text, at=None, event_id=None, commit=True):
        if (role, phase) not in {('user','prompt'), ('assistant','commentary'), ('assistant','final')}:
            raise ValueError('Only public user and assistant messages are supported')
        if not isinstance(text, str) or not text.strip():
            return None
        clean = redact(text, self.secrets)
        at = timestamp(at)
        # Final text appears both as a message and in the terminal envelope.
        identity = '|'.join((run_id, role, phase, '' if phase == 'final' else at, clean))
        event_id = event_id or hashlib.sha256(identity.encode()).hexdigest()
        with self.lock:
            self.db.execute('INSERT OR IGNORE INTO messages(event_id,agent_id,run_id,role,phase,text,at) VALUES (?,?,?,?,?,?,?)',
                            (event_id,agent_id,run_id,role,phase,clean,at))
            if commit:
                self.db.commit()
            return self.db.execute('SELECT seq FROM messages WHERE agent_id=? AND event_id=?',(agent_id,event_id)).fetchone()[0]

    def messages(self, allowed, agent, run=None, after=0, before=None, limit=100):
        if agent not in allowed:
            return []
        clauses, args = ['agent_id=?'], [agent]
        # Keep exact source event IDs for authenticated reconciliation, but show
        # a final body once when both a public reply and terminal envelope carry
        # it. Filter before paging so hidden duplicates cannot create empty
        # pages or repeat an earlier final after a cursor advances.
        if run:
            clauses.append('run_id=?');args.append(run)
        if after:
            clauses.append('seq>?');args.append(int(after))
        if before:
            clauses.append('seq<?');args.append(int(before))
        direction = 'ASC' if after else 'DESC'
        args.append(min(max(int(limit),1),200))
        with self.lock:
            # A structured Cursor root bubble may revise its already-visible
            # identity after a native final envelope was accepted. Prefer that
            # authenticated canonical identity so after-cursor clients receive
            # the revision; keep all native event receipts and raw envelopes.
            # Bare HistoryStore users and older databases retain the original
            # earliest-final rule when these optional producer tables lack the
            # required schema. Child and unrelated provider histories do too.
            tables={row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('cursor_public_messages','cursor_public_root_runs')")}
            canonical=None
            if len(tables)==2:
                root_columns={row[1] for row in self.db.execute('PRAGMA table_info(cursor_public_root_runs)')}
                public_columns={row[1] for row in self.db.execute('PRAGMA table_info(cursor_public_messages)')}
                if ({'observer_id','generation','root_id','run_id','start_event_id','native_final_fingerprint'}<=root_columns
                        and {'observer_id','generation','child_id','run_id','event_id','fingerprint','phase'}<=public_columns):
                    canonical="""SELECT projected.event_id FROM cursor_public_messages p
                        JOIN cursor_public_root_runs r ON r.observer_id=p.observer_id AND r.generation=p.generation
                            AND r.root_id=p.child_id AND r.run_id=p.run_id AND r.native_final_fingerprint=p.fingerprint
                        JOIN event_receipts started ON started.agent_id=r.root_id
                            AND started.run_id=r.run_id AND started.event_id=r.start_event_id
                        JOIN messages projected ON projected.agent_id=p.child_id
                            AND projected.run_id=p.run_id AND projected.event_id=p.event_id
                        WHERE p.child_id=messages.agent_id AND p.run_id=messages.run_id
                            AND p.phase='final' AND projected.role='assistant' AND projected.phase='final'
                            AND projected.text=messages.text ORDER BY projected.seq LIMIT 1"""
            prior="""EXISTS (SELECT 1 FROM messages prior WHERE prior.agent_id=messages.agent_id
                AND prior.run_id=messages.run_id AND prior.role='assistant' AND prior.phase='final'
                AND prior.text=messages.text AND prior.seq<messages.seq)"""
            if canonical:
                clauses.append("NOT (role='assistant' AND phase='final' AND CASE WHEN ("+canonical+
                    ") IS NOT NULL THEN event_id != ("+canonical+") ELSE "+prior+" END)")
            else:
                clauses.append("NOT (role='assistant' AND phase='final' AND "+prior+")")
            rows = self.db.execute('SELECT seq,event_id,agent_id,run_id,role,phase,text,at FROM messages WHERE '+
                                   ' AND '.join(clauses)+' ORDER BY seq '+direction+' LIMIT ?',args).fetchall()
        result = [dict(row) for row in rows]
        return result if after else list(reversed(result))

    def stream_message(self, agent, run, segment, text, final=False):
        """One visible native message, with increasing revisions for polling.

        Replace the old row atomically. event_id stays stable for client merging;
        seq advances so an already-open bubble receives edits and finalization.
        """
        if not isinstance(text, str) or not text.strip():
            return
        event_id = 'hermes:%s:text:%s' % (run, segment)
        phase, clean = ('final' if final else 'commentary'), redact(text, self.secrets)
        with self.lock, self.db:
            old = self.db.execute('SELECT text,phase FROM messages WHERE agent_id=? AND event_id=?', (agent, event_id)).fetchone()
            if old and old['text'] == clean and old['phase'] == phase:
                return
            self.db.execute('DELETE FROM messages WHERE agent_id=? AND event_id=?', (agent, event_id))
            self.db.execute('INSERT INTO messages(event_id,agent_id,run_id,role,phase,text,at) VALUES (?,?,?,?,?,?,?)',
                (event_id, agent, run, 'assistant', phase, clean, timestamp()))

    def latest_messages(self, allowed):
        return {agent: rows[-1] for agent in allowed if (rows := self.messages(allowed,agent,limit=1))}

    def record(self, agent_id, provider, name, run_id, status, at=None, prompt=None,
               delivery=None, source_ref='', source_kind='', started_at=None, commit=True):
        if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,180}', run_id):
            return None
        if status not in {'working', 'done', 'error', 'cancelled', 'unknown'}:
            return None
        ident = hashlib.sha256((agent_id + ':' + run_id).encode()).hexdigest()[:32]
        at = timestamp(at)
        with self.lock:
            old = self.db.execute('SELECT * FROM runs WHERE id=?', (ident,)).fetchone()
            effective = old['status'] if old and old['status'] in TERMINAL else status
            start = timestamp(started_at) if started_at is not None else old['started_at'] if old else at
            # Late public text enriches the delivery; it is not another finish
            # event and cannot restart or change an already terminal outcome.
            finish = old['finished_at'] if old and old['status'] in TERMINAL else at if status in TERMINAL else None
            clean_prompt = redact(prompt, self.secrets, 6000) if prompt else old['prompt'] if old else ''
            clean_delivery = redact(delivery, self.secrets) if delivery else old['delivery'] if old else ''
            ref = source_ref or (old['source_ref'] if old else '')
            kind = source_kind or (old['source_kind'] if old else '')
            self.db.execute("""INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET status=excluded.status,started_at=excluded.started_at,
                finished_at=excluded.finished_at,updated_at=excluded.updated_at,prompt=excluded.prompt,
                delivery=excluded.delivery,source_ref=excluded.source_ref,source_kind=excluded.source_kind""",
                (ident,agent_id,run_id,provider,name,effective,start,finish,timestamp(),clean_prompt,clean_delivery,ref,kind))
            if commit:
                self.db.commit()
        return ident

    def list(self, allowed, agent=None, limit=60, before=None, terminal=False):
        ids = [agent] if agent in allowed else list(allowed) if agent is None else []
        if not ids:
            return []
        clauses = ['agent_id IN (%s)' % ','.join('?' for _ in ids)]
        args = list(ids)
        if terminal:
            clauses.append("status IN ('done','error','cancelled')")
        if before:
            clauses.append("started_at || '|' || id < ?")
            args.append(before)
        args.append(min(max(int(limit),1),100))
        with self.lock:
            rows = self.db.execute('SELECT id,agent_id,provider,name,status,started_at,finished_at,updated_at,substr(prompt,1,180) AS prompt_preview,substr(delivery,1,220) AS delivery_preview FROM runs WHERE ' + ' AND '.join(clauses) + ' ORDER BY started_at DESC,id DESC LIMIT ?',args).fetchall()
        return [dict(row) for row in rows]

    def detail(self, ident, allowed):
        with self.lock:
            row = self.db.execute('SELECT * FROM runs WHERE id=?',(ident,)).fetchone()
        if not row or row['agent_id'] not in allowed:
            return None
        return {k:row[k] for k in row.keys() if k not in {'source_ref','source_kind'}}

    def references(self, allowed):
        if not allowed:
            return []
        with self.lock:
            rows = self.db.execute("SELECT agent_id,run_id,source_ref,source_kind FROM runs WHERE agent_id IN (%s) AND source_ref != ? AND (status IN ('working','unknown') OR delivery='') ORDER BY started_at DESC LIMIT 100" % ','.join('?' for _ in allowed),list(allowed)+['']).fetchall()
        return [dict(row) for row in rows]

    def close(self):
        with self.lock:
            self.db.close()
