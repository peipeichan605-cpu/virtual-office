"""Scoped observed-child callbacks; never grants membership or execution rights."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
import uuid

from event_ingest import EventConflict, ingest, validate_event
from history import redact, timestamp
from subagents import MAX_CHILDREN, MAX_DEPTH, clean_name, seconds
from cursor_public_sync import CursorPublicSync

FIELDS = {'event_id','native_child_id','native_parent_id','parent_run_id','child_run_id',
          'type','role','phase','text','at','name','native_alias'}
KINDS = {'started':'turn_started','message':'message','completed':'turn_completed',
         'failed':'turn_failed','cancelled':'turn_cancelled'}
TERMINAL = {'done','error','cancelled'}
IDENT = re.compile(r'[A-Za-z0-9_.:@/-]{1,240}\Z')


class ChildEventError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def identity(value):
    if not isinstance(value,str) or not IDENT.fullmatch(value):
        raise ChildEventError('invalid_child_identity',400)
    return value


def validate(data):
    if not isinstance(data,dict) or set(data)-FIELDS:
        raise ChildEventError('invalid_child_event',400)
    for field in ('event_id','native_child_id','native_parent_id','parent_run_id','child_run_id'):
        identity(data.get(field))
    if data.get('type') not in KINDS:
        raise ChildEventError('invalid_child_event',400)
    if 'text' in data and (not isinstance(data['text'],str) or len(data['text'])>24000):
        raise ChildEventError('invalid_child_text',400)
    if 'name' in data and (not isinstance(data['name'],str) or len(data['name'])>200):
        raise ChildEventError('invalid_child_name',400)
    if 'at' in data and (not isinstance(data['at'],str) or len(data['at'])>50 or not seconds(data['at']) or seconds(data['at'])>time.time()+60):
        raise ChildEventError('invalid_child_timestamp',400)
    if data['type']=='message' and (data.get('role'),data.get('phase')) not in {
            ('user','prompt'),('assistant','commentary'),('assistant','final')}:
        raise ChildEventError('invalid_public_child_message',400)
    if any(k in data for k in ('role','phase')) and (data.get('role'),data.get('phase')) not in {
            ('user','prompt'),('assistant','commentary'),('assistant','final')}:
        raise ChildEventError('invalid_public_child_message',400)
    alias=data.get('native_alias')
    if alias is not None:
        if not isinstance(alias,dict) or set(alias)-{'kind','id','parent_tool_call_id'} or alias.get('kind') not in {'tool_call','conversation'}:
            raise ChildEventError('invalid_child_alias',400)
        identity(alias.get('id'))
        if 'parent_tool_call_id' in alias:identity(alias['parent_tool_call_id'])
    return data


class ChildEvents:
    """Transport identity comes from a server-issued observer or a push root.

    Native hook credentials identify an installed observer. Model-cooperative
    reports always retain parent_reported evidence, including Grok Bot reports.
    All operations share the existing commands -> history lifecycle lock order.
    """
    def __init__(self, bridge):
        self.bridge=bridge
        self.started=time.time()
        self.directory=Path(bridge.config['data_dir'])/'child-observers'
        with bridge.commands.lock,bridge.history.lock,bridge.history.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS child_observers (
                id TEXT PRIMARY KEY,root_id TEXT NOT NULL,provider TEXT NOT NULL,
                native_session_id TEXT NOT NULL,generation TEXT NOT NULL,
                token_sha256 TEXT NOT NULL,credential_path TEXT NOT NULL,
                source_kind TEXT NOT NULL,evidence TEXT NOT NULL,active INTEGER NOT NULL,
                activation_sha256 TEXT NOT NULL DEFAULT '')''')
            if 'activation_sha256' not in {r[1] for r in self.db.execute('PRAGMA table_info(child_observers)')}:
                self.db.execute("ALTER TABLE child_observers ADD COLUMN activation_sha256 TEXT NOT NULL DEFAULT ''")
            self.db.execute('CREATE INDEX IF NOT EXISTS child_observers_root ON child_observers(root_id,active)')
            self.db.execute('''CREATE TABLE IF NOT EXISTS child_event_receipts (
                scope TEXT NOT NULL,event_id TEXT NOT NULL,fingerprint TEXT NOT NULL,
                child_id TEXT NOT NULL,run_id TEXT NOT NULL,status TEXT NOT NULL,
                PRIMARY KEY(scope,event_id))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS child_native_aliases (
                root_id TEXT NOT NULL,generation TEXT NOT NULL,source_kind TEXT NOT NULL,
                kind TEXT NOT NULL,native_id TEXT NOT NULL,child_id TEXT NOT NULL,
                PRIMARY KEY(root_id,generation,source_kind,kind,native_id))''')
            schema=self.db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='child_native_aliases'").fetchone()[0]
            if 'UNIQUE(child_id,kind)' in schema.replace(' ',''):
                self.db.execute('ALTER TABLE child_native_aliases RENAME TO child_native_aliases_old')
                self.db.execute('''CREATE TABLE child_native_aliases (
                    root_id TEXT NOT NULL,generation TEXT NOT NULL,source_kind TEXT NOT NULL,
                    kind TEXT NOT NULL,native_id TEXT NOT NULL,child_id TEXT NOT NULL,
                    PRIMARY KEY(root_id,generation,source_kind,kind,native_id))''')
                self.db.execute('INSERT INTO child_native_aliases SELECT * FROM child_native_aliases_old')
                self.db.execute('DROP TABLE child_native_aliases_old')
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS child_conversation_identity ON child_native_aliases(child_id) WHERE kind='conversation'")
            self.db.execute('''CREATE TABLE IF NOT EXISTS child_callback_runs (
                child_id TEXT NOT NULL,native_run_id TEXT NOT NULL,run_id TEXT NOT NULL,
                parent_run_id TEXT NOT NULL,PRIMARY KEY(child_id,native_run_id))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS child_observer_rotations (
                observer_id TEXT PRIMARY KEY,credential_path TEXT NOT NULL,
                prepared_path TEXT NOT NULL,generation TEXT NOT NULL,token_sha256 TEXT NOT NULL)''')
            self.restored_ids={row[0] for row in self.db.execute("SELECT id FROM observed_agents WHERE source_kind!='codex_native' AND status NOT IN ('done','error','cancelled')")}
            self.db.execute("UPDATE observed_agents SET status='waiting_user',step='重啟後等待子 Agent 來源更新；未確認完成' WHERE source_kind!='codex_native' AND status='working'")
            self._recover_rotations()
            self.refresh_index()
        self.public_sync=CursorPublicSync(self)

    @property
    def db(self):return self.bridge.history.db

    def _root(self, root_id):
        root=self.bridge.definitions.get(root_id)
        row=self.db.execute('SELECT * FROM observed_agent_roots WHERE root_id=? AND active=1',(root_id,)).fetchone()
        if not root or not row:raise ChildEventError('root_not_selected',404)
        return root,dict(row)

    def _independent_root(self,provider,root_id,native):
        for root in self.bridge.definitions.values():
            if root['id']!=root_id and root['provider']==provider and root['source_id']==native:
                return root['id']
        for row in self.db.execute('''SELECT o.root_id FROM child_observers o JOIN observed_agent_roots r
                ON o.root_id=r.root_id AND o.generation=r.generation WHERE o.active=1 AND r.active=1
                AND o.provider=? AND o.native_session_id=? AND o.root_id!=?''',(provider,native,root_id)):
            if row['root_id'] in self.bridge.definitions:return row['root_id']
        return None

    def create_observer(self, data):
        if not isinstance(data,dict) or set(data)!={'agent_id','provider','native_session_id'}:
            raise ChildEventError('invalid_child_observer',400)
        native=identity(data['native_session_id'])
        b=self.bridge
        with b.commands.lock,b.history.lock,b.history.db:
            b.children.sync()
            root,state=self._root(data['agent_id'])
            if root['provider']!=data['provider'] or root['provider'] not in {'cursor','grok_bot'}:
                raise ChildEventError('child_provider_mismatch',400)
            if self._independent_root(root['provider'],root['id'],native):
                raise ChildEventError('native_session_already_observed')
            old=self.db.execute('SELECT * FROM child_observers WHERE root_id=? AND active=1 ORDER BY rowid DESC LIMIT 1',(root['id'],)).fetchone()
            if old and old['native_session_id']==native and old['generation']==state['generation']:
                p=Path(old['credential_path'])
                try:
                    info=p.lstat()
                    if stat.S_ISREG(info.st_mode) and not info.st_mode&0o077 and self._file_matches(old,self._read_private(p)):
                        self.refresh_index()
                        return self._binding(old)
                except (OSError,ValueError,TypeError):pass
            # Rebinding revokes all previous observation descendants immediately;
            # membership, root commands and root history remain untouched.
            if old:
                generation=uuid.uuid4().hex
                self.db.execute('UPDATE observed_agent_roots SET generation=? WHERE root_id=?',(generation,root['id']))
                state['generation']=generation
            ident=uuid.uuid4().hex;token=secrets.token_urlsafe(32)
            kind='cursor_hook' if root['provider']=='cursor' else 'parent_reported'
            evidence='native_hook' if kind=='cursor_hook' else 'parent_reported'
            self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
            path=self.directory/(ident+'.json')
            base='http://127.0.0.1:'+str(b.config['port'])
            credential={'endpoint':base+'/api/agents/children/events',
                        'root_event_endpoint':base+'/api/agents/observers/root-events',
                        'observer_id':ident,'agent_id':root['id'],'provider':root['provider'],
                        'native_session_id':native,'generation':state['generation'],'token':token,
                        'source_kind':kind,'evidence':evidence}
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'w') as out:json.dump(credential,out,ensure_ascii=False,indent=2)
            with self.db:
                self.db.execute('UPDATE child_observers SET active=0 WHERE root_id=?',(root['id'],))
                self.db.execute('INSERT INTO child_observers (id,root_id,provider,native_session_id,generation,token_sha256,credential_path,source_kind,evidence,active) VALUES (?,?,?,?,?,?,?,?,?,1)',
                    (ident,root['id'],root['provider'],native,state['generation'],hashlib.sha256(token.encode()).hexdigest(),str(path),kind,evidence))
            self.refresh_index()
            return self._binding(self.db.execute('SELECT * FROM child_observers WHERE id=?',(ident,)).fetchone())

    def _binding(self,row):
        return {'observer_id':row['id'],'agent_id':row['root_id'],'provider':row['provider'],
                'native_session_id':row['native_session_id'],'credential_path':row['credential_path'],
                'generation':row['generation'],'source_kind':row['source_kind'],'evidence':row['evidence']}

    @staticmethod
    def _write_private(path,data):
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as out:
            json.dump(data,out,ensure_ascii=False,indent=2)
            out.flush();os.fsync(out.fileno())

    def _read_private(self,path):
        path=Path(path)
        if path.parent.resolve()!=self.directory.resolve():raise ValueError('invalid_observer_path')
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd) as source:
            info=os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size>16384 or info.st_mode&0o077:
                raise ValueError('invalid_observer_file')
            return json.load(source)

    @staticmethod
    def _file_matches(row,data):
        return (isinstance(data,dict) and data.get('observer_id')==row['id'] and
            data.get('agent_id')==row['root_id'] and data.get('provider')==row['provider'] and
            data.get('native_session_id')==row['native_session_id'] and data.get('generation')==row['generation'] and
            isinstance(data.get('token'),str) and hmac.compare_digest(hashlib.sha256(data['token'].encode()).hexdigest(),row['token_sha256']))

    def refresh_index(self):
        try:self._refresh_index()
        except (OSError,ValueError,sqlite3.Error):
            self.bridge.health['child_observer_error']='observer_index_unavailable'
            return False
        self.bridge.health.pop('child_observer_error',None)
        return True

    def _refresh_index(self):
        if not self.directory.exists():return
        paths=[]
        for row in self.db.execute('''SELECT o.* FROM child_observers o JOIN observed_agent_roots r
                ON o.root_id=r.root_id AND o.generation=r.generation WHERE o.active=1 AND r.active=1
                AND o.provider='cursor' ORDER BY o.rowid DESC LIMIT 128'''):
            if row['root_id'] not in self.bridge.definitions:continue
            path=Path(row['credential_path'])
            if path.parent==self.directory and re.fullmatch(r'[0-9a-f]{32}\.json',path.name):paths.append(path.name)
        data={'version':1,'credentials':paths};target=self.directory/'.cursor-active.json'
        try:
            if self._read_private(target)==data:return
        except (OSError,ValueError,TypeError):pass
        prepared=target.with_name('.cursor-active-'+uuid.uuid4().hex+'.tmp')
        self._write_private(prepared,data);os.replace(prepared,target)

    def _set_activation_hash(self,ident,marker):
        with self.db:self.db.execute('UPDATE child_observers SET activation_sha256=? WHERE id=?',
            (hashlib.sha256(marker.encode()).hexdigest(),ident))

    def _pending_binding(self,row):
        path=Path(row['credential_path']);data=self._read_private(path)
        if not self._file_matches(row,data):raise ValueError('invalid_pending_file')
        marker=data.get('activation_marker')
        valid=isinstance(marker,str) and bool(re.fullmatch(r'OFFICE_CURSOR_BIND_[0-9a-f]{32}',marker))
        digest=hashlib.sha256(marker.encode()).hexdigest() if valid else ''
        if not digest or not hmac.compare_digest(digest,row['activation_sha256']):
            # Recover both crash windows: row created before marker staging, or
            # marker file renamed before its DB hash was committed.
            if not valid or row['activation_sha256']:
                marker='OFFICE_CURSOR_BIND_'+uuid.uuid4().hex;data['activation_marker']=marker
                prepared=path.with_name(path.stem+'-'+uuid.uuid4().hex+'.tmp')
                self._write_private(prepared,data);os.replace(prepared,path)
            self._set_activation_hash(row['id'],marker)
        self.refresh_index()
        return dict(self._binding(row),pending=True,activation_marker=marker)

    def _recover_rotations(self):
        # The journal is committed with the new token hash and generation.
        # A crash between that commit and rename resumes the prepared 0600 file;
        # it never restores the old token or the old observation generation.
        for row in self.db.execute('SELECT * FROM child_observer_rotations').fetchall():
            final=Path(row['credential_path']);prepared=Path(row['prepared_path'])
            try:
                source=prepared if prepared.exists() else final
                data=self._read_private(source)
                if (data.get('generation')!=row['generation'] or not isinstance(data.get('token'),str) or
                    not hmac.compare_digest(hashlib.sha256(data['token'].encode()).hexdigest(),row['token_sha256'])):
                    raise ValueError('invalid_observer_rotation')
                if source==prepared:os.replace(prepared,final)
            except (OSError,ValueError,TypeError):
                self.db.execute('UPDATE child_observers SET active=0 WHERE id=?',(row['observer_id'],))
            self.db.execute('DELETE FROM child_observer_rotations WHERE observer_id=?',(row['observer_id'],))
        self.db.commit()

    def register_pending(self,ident,token,expected_generation=None):
        b=self.bridge
        with b.commands.lock,b.history.lock:
            scope=self.report_scope(ident,token)
            if expected_generation is not None and expected_generation!=scope['generation']:
                raise ChildEventError('observer_generation_expired',403)
            if scope['provider']!='cursor':raise ChildEventError('pending_observer_not_supported',400)
            old=self.db.execute('SELECT * FROM child_observers WHERE root_id=? AND active=1 ORDER BY rowid DESC LIMIT 1',(ident,)).fetchone()
            if old and old['generation']==scope['generation']:
                try:
                    if old['native_session_id'].startswith('pending:'):return self._pending_binding(old)
                    if self._file_matches(old,self._read_private(old['credential_path'])):
                        self.refresh_index()
                        return dict(self._binding(old),pending=False)
                except (OSError,ValueError,TypeError):pass
                binding=self.create_observer({'agent_id':ident,'provider':'cursor','native_session_id':old['native_session_id']})
                row=self.db.execute('SELECT * FROM child_observers WHERE id=?',(binding['observer_id'],)).fetchone()
                return self._pending_binding(row) if row['native_session_id'].startswith('pending:') else dict(binding,pending=False)
            binding=self.create_observer({'agent_id':ident,'provider':'cursor','native_session_id':'pending:'+uuid.uuid4().hex})
            return self._pending_binding(self.db.execute('SELECT * FROM child_observers WHERE id=?',(binding['observer_id'],)).fetchone())

    def bind(self,ident,token,data,expected_generation=None):
        if not isinstance(data,dict) or set(data)!={'native_session_id','activation_marker'}:
            raise ChildEventError('invalid_observer_binding',400)
        native=identity(data['native_session_id']);marker=data['activation_marker']
        if native.startswith('pending:') or not isinstance(marker,str) or not 1<=len(marker)<=128:
            raise ChildEventError('invalid_observer_binding',400)
        b=self.bridge
        with b.commands.lock,b.history.lock:
            scope=self.observer_scope(ident,token,allow_pending=True)
            if expected_generation is not None and expected_generation!=scope['generation']:
                raise ChildEventError('observer_generation_expired',403)
            row=self.db.execute('SELECT * FROM child_observers WHERE id=?',(ident,)).fetchone()
            if row['provider']!='cursor' or not row['activation_sha256'] or not hmac.compare_digest(hashlib.sha256(marker.encode()).hexdigest(),row['activation_sha256']):
                raise ChildEventError('invalid_observer_activation',403)
            if self._independent_root(row['provider'],row['root_id'],native):
                raise ChildEventError('native_session_already_observed')
            if not row['native_session_id'].startswith('pending:'):
                if native!=row['native_session_id']:raise ChildEventError('observer_binding_conflict')
                return dict(self._binding(row),pending=False)
            generation=uuid.uuid4().hex;new_token=secrets.token_urlsafe(32)
            path=Path(row['credential_path']);credential=self._read_private(path)
            credential.update(native_session_id=native,generation=generation,token=new_token)
            prepared=path.with_name(path.stem+'-'+uuid.uuid4().hex+'.prepared')
            self._write_private(prepared,credential)
            digest=hashlib.sha256(new_token.encode()).hexdigest()
            with self.db:
                self.db.execute('UPDATE observed_agent_roots SET generation=? WHERE root_id=?',(generation,row['root_id']))
                self.db.execute('UPDATE child_observers SET generation=?,native_session_id=?,token_sha256=? WHERE id=?',(generation,native,digest,ident))
                self.db.execute('INSERT INTO child_observer_rotations VALUES (?,?,?,?,?)',(ident,str(path),str(prepared),generation,digest))
            self._recover_rotations()
            current=self.db.execute('SELECT * FROM child_observers WHERE id=? AND active=1',(ident,)).fetchone()
            if not current:raise ChildEventError('observer_rotation_unavailable',503)
            self.refresh_index()
            return dict(self._binding(current),pending=False)

    def observer_scope(self, ident, token, allow_pending=False):
        self._recover_rotations()
        row=self.db.execute('SELECT * FROM child_observers WHERE id=? AND active=1',(ident,)).fetchone()
        if not row or not token or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(),row['token_sha256']):
            raise ChildEventError('invalid_observer_credential',403)
        try:root,state=self._root(row['root_id'])
        except ChildEventError as error:raise ChildEventError('invalid_observer_credential',403) from error
        if state['generation']!=row['generation'] or root['provider']!=row['provider']:
            raise ChildEventError('observer_generation_expired',403)
        if row['native_session_id'].startswith('pending:') and not allow_pending:
            raise ChildEventError('observer_binding_pending',403)
        if token not in self.bridge.history.secrets:self.bridge.history.secrets=(*self.bridge.history.secrets,token)
        return {'scope':row['id'],'root_id':root['id'],'provider':root['provider'],
                'generation':state['generation'],'native_parent_id':row['native_session_id'],
                'source_kind':row['source_kind'],'evidence':row['evidence']}

    def report_scope(self, ident, token):
        try:root,state=self._root(ident)
        except ChildEventError as error:raise ChildEventError('invalid_agent_credential',403) from error
        if root['driver']!='push' or not token or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(),root.get('token_sha256','')):
            raise ChildEventError('invalid_agent_credential',403)
        if token not in self.bridge.history.secrets:self.bridge.history.secrets=(*self.bridge.history.secrets,token)
        native=root['source_id']
        binding=self.db.execute('SELECT native_session_id FROM child_observers WHERE root_id=? AND generation=? AND active=1 ORDER BY rowid DESC LIMIT 1',(ident,state['generation'])).fetchone()
        return {'scope':'report:'+ident+':'+state['generation'],'root_id':ident,'provider':root['provider'],
                'generation':state['generation'],'native_parent_id':native,
                'root_alias':binding[0] if binding else native,
                'source_kind':'parent_reported','evidence':'parent_reported'}

    def authorize(self, mode, ident, token):
        b=self.bridge
        with b.commands.lock,b.history.lock:
            return self.observer_scope(ident,token,allow_pending=mode=='bind') if mode in {'observer','bind'} else self.report_scope(ident,token)

    def accept(self, mode, ident, token, data, expected_generation=None):
        validate(data)
        b=self.bridge
        with b.commands.lock,b.history.lock,b.history.db:
            scope=self.observer_scope(ident,token) if mode=='observer' else self.report_scope(ident,token)
            if expected_generation is not None and expected_generation!=scope['generation']:
                raise ChildEventError('observer_generation_expired',403)
            return self._accept(scope,data)

    def _parent(self,scope,native):
        if native in {scope['native_parent_id'],scope.get('root_alias')}:
            return scope['root_id'],0
        row=self.db.execute('''SELECT * FROM observed_agents WHERE root_id=? AND generation=?
            AND source_kind=? AND native_id=?''',(scope['root_id'],scope['generation'],scope['source_kind'],native)).fetchone()
        if row is None:
            row=self.db.execute('''SELECT a.* FROM child_native_aliases n JOIN observed_agents a ON a.id=n.child_id
                WHERE n.root_id=? AND n.generation=? AND n.source_kind=? AND n.kind='conversation' AND n.native_id=?''',
                (scope['root_id'],scope['generation'],scope['source_kind'],native)).fetchone()
        if row is None:raise ChildEventError('child_parent_not_observed')
        return row['id'],row['depth']

    def _alias(self,scope,child_id,data,store=True):
        alias=data.get('native_alias')
        if alias is None:return
        if scope['source_kind']!='cursor_hook':raise ChildEventError('native_alias_not_supported',400)
        kind,native=alias['kind'],alias['id']
        if kind=='conversation' and native in {scope['native_parent_id'],scope.get('root_alias')}:
            raise ChildEventError('child_alias_conflict')
        prior=self.db.execute('SELECT child_id FROM child_native_aliases WHERE root_id=? AND generation=? AND source_kind=? AND kind=? AND native_id=?',
            (scope['root_id'],scope['generation'],scope['source_kind'],kind,native)).fetchone()
        own=self.db.execute('SELECT native_id FROM child_native_aliases WHERE child_id=? AND kind=?',(child_id,kind)).fetchone() if kind=='conversation' else None
        if prior and prior[0]!=child_id or own and own[0]!=native:
            raise ChildEventError('child_alias_conflict')
        if kind=='tool_call':
            if data['type']!='started':raise ChildEventError('child_alias_requires_start')
        elif not prior:
            call=self.db.execute("SELECT 1 FROM child_native_aliases WHERE child_id=? AND kind='tool_call' AND native_id=?",(child_id,alias.get('parent_tool_call_id'))).fetchone()
            if not call:
                raise ChildEventError('child_alias_unverified')
        if store:
            self.db.execute('INSERT OR IGNORE INTO child_native_aliases VALUES (?,?,?,?,?,?)',
                (scope['root_id'],scope['generation'],scope['source_kind'],kind,native,child_id))

    def _birth_window(self,scope,parent,data,born):
        root_source=self.db.execute('SELECT * FROM runs WHERE agent_id=? AND run_id=?',
            (scope['root_id'],data['parent_run_id'])).fetchone()
        if not root_source:raise ChildEventError('parent_run_not_current')
        if parent==scope['root_id']:
            live=self.db.execute('SELECT run_id FROM live_runs WHERE agent_id=?',(parent,)).fetchone()
            if not live or live[0]!=data['parent_run_id']:raise ChildEventError('parent_run_not_current')
            if born<seconds(root_source['started_at'])-1 or root_source['finished_at'] and born>seconds(root_source['finished_at'])+1:
                raise ChildEventError('child_birth_outside_parent_run')
        else:
            direct=self.db.execute('SELECT * FROM observed_agents WHERE id=? AND root_id=? AND generation=?',
                (parent,scope['root_id'],scope['generation'])).fetchone()
            live=self.db.execute('SELECT * FROM live_runs WHERE agent_id=?',(parent,)).fetchone()
            source=self.db.execute('SELECT * FROM runs WHERE agent_id=? AND run_id=?',
                (parent,direct['run_id'] if direct else '')).fetchone()
            if (not direct or direct['status']!='working' or direct['parent_run_id']!=data['parent_run_id'] or
                not live or live['run_id']!=direct['run_id'] or live['status']!='working' or
                time.time()-seconds(live['updated_at'])>1800 or not source or source['finished_at']):
                raise ChildEventError('direct_parent_not_active')
            if born<seconds(source['started_at'])-1:raise ChildEventError('child_birth_outside_parent_run')
        return seconds(root_source['started_at'])

    def _accept(self,scope,data):
        b=self.bridge
        fingerprint=hashlib.sha256(json.dumps(data,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        old=self.db.execute('SELECT * FROM child_event_receipts WHERE scope=? AND event_id=?',(scope['scope'],data['event_id'])).fetchone()
        if old:
            if old['fingerprint']!=fingerprint:raise ChildEventError('child_event_id_conflict')
            return {'accepted':True,'duplicate':True,'agent_id':old['child_id'],'run_id':old['run_id'],'status':old['status'],'evidence':scope['evidence']}
        if data['native_child_id'] in {scope['native_parent_id'],scope.get('root_alias'),data['native_parent_id']}:
            raise ChildEventError('child_parent_cycle')
        parent,depth=self._parent(scope,data['native_parent_id'])
        if depth+1>MAX_DEPTH:raise ChildEventError('child_depth_limit')
        cid='child:'+scope['provider']+':'+hashlib.sha256((scope['root_id']+':'+scope['generation']+':'+scope['source_kind']+':'+data['native_child_id']).encode()).hexdigest()[:32]
        row=self.db.execute('SELECT * FROM observed_agents WHERE id=?',(cid,)).fetchone()
        at=timestamp(data.get('at'));born=seconds(at)
        if row is None:
            if data['type']!='started':raise ChildEventError('child_start_required')
            if self._independent_root(scope['provider'],scope['root_id'],data['native_child_id']):
                raise ChildEventError('child_independently_enrolled')
            for selected in b.definitions.values():
                if selected['provider']==scope['provider'] and selected['source_id']==data['native_child_id']:
                    raise ChildEventError('child_independently_enrolled')
            root_started=self._birth_window(scope,parent,data,born)
            count=self.db.execute("SELECT count(*) FROM observed_agents WHERE root_id=? AND generation=? AND suppressed=0 AND status NOT IN ('done','error','cancelled')",(scope['root_id'],scope['generation'])).fetchone()[0]
            if count>=MAX_CHILDREN:raise ChildEventError('child_capacity')
            row={'id':cid,'provider':scope['provider'],'native_id':data['native_child_id'],
                 'root_id':scope['root_id'],'parent_id':parent,'generation':scope['generation'],
                 'depth':depth+1,'name':clean_name(redact(data.get('name'),b.history.secrets),scope['provider']+' 子 Agent')+' · '+cid[-6:],
                 'agent_path':'','born':born,'parent_run_id':data['parent_run_id'],
                 'root_started':root_started,'status':'waiting_user','step':'子 Agent 已建立；等待工作事件',
                 'run_id':None,'updated_at':born,'terminal_at':None,'source_path':'','parent_context_run':data['parent_run_id'],
                 'native_revision':'','source_kind':scope['source_kind'],'evidence':scope['evidence']}
            self.db.execute('INSERT INTO observed_agents ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
        else:
            row=dict(row)
            if row['generation']!=scope['generation'] or row['parent_id']!=parent:
                raise ChildEventError('child_parent_conflict')
        native_run=data['child_run_id']
        run=native_run if re.fullmatch(r'[A-Za-z0-9_-]{1,160}',native_run) else 'native-'+hashlib.sha256(native_run.encode()).hexdigest()[:40]
        alias=data.get('native_alias')
        independent=alias and alias['kind']=='conversation' and self._independent_root(scope['provider'],scope['root_id'],alias['id'])
        mapping=self.db.execute('SELECT * FROM child_callback_runs WHERE child_id=? AND native_run_id=?',(cid,native_run)).fetchone()
        if mapping and mapping['parent_run_id']!=data['parent_run_id']:
            raise ChildEventError('child_run_parent_conflict')
        if data['type']=='started' and not mapping:
            self._birth_window(scope,parent,data,born)
        elif not mapping:
            raise ChildEventError('child_start_required')
        self._alias(scope,cid,data,store=not (row.get('suppressed') or independent))
        if row.get('suppressed') or independent:
            # A Start may identify only a Task ID. Once its verified conversation
            # is independently enrolled, keep that member and retire this
            # provisional projection; do not attach it to another selected root.
            self.db.execute('UPDATE observed_agents SET suppressed=1 WHERE id=?',(cid,))
            self.db.execute('INSERT INTO child_event_receipts VALUES (?,?,?,?,?,?)',(scope['scope'],data['event_id'],fingerprint,cid,run,'unknown'))
            return {'accepted':True,'duplicate':False,'ignored':True,'reason':'child_independently_enrolled',
                    'agent_id':cid,'run_id':run,'status':'unknown','evidence':scope['evidence']}
        event={'source_id':data['native_child_id'],'run_id':run,
               'event_id':'child:'+hashlib.sha256(data['event_id'].encode()).hexdigest(),
               'type':KINDS[data['type']],'at':at}
        for key in ('text','role','phase'):
            if key in data:event[key]=data[key]
        proxy={'id':cid,'provider':scope['provider'],'name':row['name'],'source_id':data['native_child_id']}
        late_public=(data['type']=='message' and row.get('run_id')==run and row['status'] in TERMINAL)
        prior_finish=self.db.execute('SELECT finished_at FROM runs WHERE agent_id=? AND run_id=?',(cid,run)).fetchone() if late_public else None
        prior_live=self.db.execute('SELECT updated_at FROM live_runs WHERE agent_id=?',(cid,)).fetchone() if late_public else None
        try:receipt=ingest(b.history,proxy,event,transaction=False)
        except EventConflict as error:raise ChildEventError('child_run_conflict') from error
        status=receipt.get('status')
        if status is None:
            status=self.db.execute('SELECT status FROM live_runs WHERE agent_id=?',(cid,)).fetchone()[0]
        visible_status='waiting_user' if status=='working' and (time.time()-born>1800 or cid in self.restored_ids and born<self.started) else status
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO child_callback_runs VALUES (?,?,?,?)',(cid,native_run,run,data['parent_run_id']))
            self.db.execute('INSERT OR IGNORE INTO observed_agent_runs VALUES (?,?,?)',(cid,run,data['parent_run_id']))
            terminal_at=(row.get('terminal_at') or born) if late_public else born if status in TERMINAL else None
            self.db.execute('UPDATE observed_agents SET status=?,step=?,run_id=?,parent_run_id=?,parent_context_run=?,updated_at=?,terminal_at=? WHERE id=?',
                (visible_status,self._step(visible_status),run,data['parent_run_id'],data['parent_run_id'],row['updated_at'] if late_public else born,terminal_at,cid))
            if late_public:
                if prior_finish:self.db.execute('UPDATE runs SET finished_at=? WHERE agent_id=? AND run_id=?',(prior_finish[0],cid,run))
                if prior_live:self.db.execute('UPDATE live_runs SET updated_at=? WHERE agent_id=?',(prior_live[0],cid))
            self.public_sync.track(scope,cid,data,run)
            self.db.execute('INSERT INTO child_event_receipts VALUES (?,?,?,?,?,?)',(scope['scope'],data['event_id'],fingerprint,cid,run,status))
        return {'accepted':True,'duplicate':bool(receipt.get('duplicate')),'agent_id':cid,'run_id':run,'status':status,'evidence':scope['evidence']}

    @staticmethod
    def _step(status):
        return {'working':'子 Agent 正在工作','done':'子 Agent 已完成','error':'子 Agent 執行失敗',
                'cancelled':'子 Agent 已中止','waiting_user':'來源暫無更新；未確認完成'}.get(status,'未能確認子 Agent 狀態')

    def root_event(self,ident,token,data,expected_generation=None):
        b=self.bridge
        with b.commands.lock,b.history.lock:
            scope=self.observer_scope(ident,token)
            if expected_generation is not None and expected_generation!=scope['generation']:
                raise ChildEventError('observer_generation_expired',403)
            root=b.definitions[scope['root_id']]
            if root['driver']!='push':raise ChildEventError('root_observer_not_supported')
            if not isinstance(data,dict) or data.get('source_id')!=scope['native_parent_id']:
                raise ChildEventError('root_native_session_mismatch',400)
            normalized=dict(data,source_id=root['source_id'])
            validate_event(normalized,root['source_id'])
            with b.history.db:
                if (not b.history.db.execute(
                        'SELECT 1 FROM event_receipts WHERE agent_id=? AND event_id=?',
                        (root['id'],normalized['event_id'])).fetchone()):
                    self.public_sync.track_root(scope,root['id'],data)
                # Cursor reports its final public response before the Stop
                # envelope. Promote only that trailing public projection when
                # the same redacted body becomes a final delivery. Keep both
                # source event IDs/receipts; the history reader dedupes final
                # bodies before pagination. Advancing seq makes this revision
                # visible to an already-open conversation using an after cursor.
                # The existing ingest transaction commits the promotion with
                # the final event, or rolls it back on a run/receipt conflict.
                # A duplicate receipt must not change a newer public message.
                if (scope['provider']=='cursor' and scope['source_kind']=='cursor_hook'
                        and scope['evidence']=='native_hook' and normalized.get('text')
                        and not b.history.db.execute('SELECT 1 FROM event_receipts WHERE agent_id=? AND event_id=?',
                            (root['id'],normalized['event_id'])).fetchone()):
                    live=b.history.db.execute('SELECT run_id,status FROM live_runs WHERE agent_id=?',
                        (root['id'],)).fetchone()
                    final=(normalized['type']=='turn_completed' or normalized['type']=='message'
                        and normalized.get('role')=='assistant' and normalized.get('phase')=='final'
                        and live and live['run_id']==normalized['run_id'] and live['status'] in TERMINAL)
                    if final:
                        trailing=b.history.db.execute('SELECT seq,role,phase,text FROM messages '
                            'WHERE agent_id=? AND run_id=? ORDER BY seq DESC LIMIT 1',
                            (root['id'],normalized['run_id'])).fetchone()
                        if (trailing and trailing['role']=='assistant' and trailing['phase']=='commentary'
                                and trailing['text']==redact(normalized['text'],b.history.secrets)):
                            b.history.db.execute("UPDATE messages SET phase='final',"
                                'seq=(SELECT COALESCE(MAX(seq),0)+1 FROM messages) WHERE seq=?',
                                (trailing['seq'],))
                native_cursor=(scope['provider']=='cursor' and scope['source_kind']=='cursor_hook'
                    and scope['evidence']=='native_hook')
                receipt=(b.accept_agent_event(root,normalized,transaction=False) if native_cursor
                    else b.accept_agent_event(root,normalized))
                self.public_sync.suppress_root_callback_projection(scope,root['id'],data)
                return receipt

    def poll(self):
        b=self.bridge
        with b.commands.lock,b.history.lock,b.history.db:
            rows=self.db.execute('''SELECT a.* FROM observed_agents a JOIN observed_agent_roots r
                ON a.root_id=r.root_id AND a.generation=r.generation WHERE r.active=1
                AND a.source_kind!='codex_native' AND a.suppressed=0 AND a.status NOT IN ('done','error','cancelled')
                ORDER BY a.updated_at LIMIT 256''').fetchall()
            for row in rows:
                if row['root_id'] not in b.definitions:continue
                live=self.db.execute('SELECT * FROM live_runs WHERE agent_id=?',(row['id'],)).fetchone()
                if not live:continue
                status=live['status'];at=row['updated_at']
                if status=='working' and (row['id'] in self.restored_ids and at<self.started or time.time()-at>1800):status='waiting_user'
                terminal=(row['terminal_at'] or at) if status in TERMINAL else None
                self.db.execute('UPDATE observed_agents SET status=?,step=?,run_id=?,updated_at=?,terminal_at=? WHERE id=?',
                    (status,self._step(status),live['run_id'],at,terminal,row['id']))
            self.refresh_index()
        self.public_sync.poll()
