"""Local workspace identity and display grouping, independent of agent provider."""
from collections import Counter
import hashlib
from pathlib import Path
import time
import unicodedata

UNASSIGNED={'id':'workspace-unassigned','name':'未指定 Workspace'}

def workspace_path(value):
    if not isinstance(value,str) or not value.strip() or len(value)>4096 or any(ord(c)<32 or ord(c)==127 for c in value):
        raise ValueError('invalid_workspace_path')
    path=Path(value).expanduser()
    if not path.is_absolute():raise ValueError('absolute_workspace_path_required')
    return str(path.resolve())

def workspace_name(value):
    if not isinstance(value,str):raise ValueError('invalid_workspace_name')
    value=unicodedata.normalize('NFC',value).strip()
    if not value or len(value)>80 or any(ord(c)<32 or ord(c)==127 for c in value):raise ValueError('invalid_workspace_name')
    return value

class Workspaces:
    def __init__(self,history):
        self.history=history
        with history.lock,history.db:
            history.db.execute('''CREATE TABLE IF NOT EXISTS workspaces (
                id TEXT PRIMARY KEY,path TEXT UNIQUE NOT NULL,name TEXT NOT NULL)''')
            history.db.execute('''CREATE TABLE IF NOT EXISTS agent_workspaces (
                agent_id TEXT PRIMARY KEY,detected_id TEXT,detected_source TEXT,observed REAL NOT NULL DEFAULT 0,
                mode TEXT NOT NULL DEFAULT 'auto',manual_id TEXT)''')
    def _ensure(self,path,name=None):
        path=workspace_path(path);ident='workspace-'+hashlib.sha256(path.encode()).hexdigest()[:24]
        label=workspace_name(name) if name else Path(path).name or path
        self.history.db.execute('INSERT OR IGNORE INTO workspaces VALUES (?,?,?)',(ident,path,label))
        if name:self.history.db.execute('UPDATE workspaces SET name=? WHERE id=?',(label,ident))
        return ident
    def create(self,path,name=None):
        with self.history.lock,self.history.db:ident=self._ensure(path,name)
        return next(w for w in self.catalog() if w['id']==ident)
    def require(self,ident):
        if not isinstance(ident,str) or not self.history.db.execute('SELECT 1 FROM workspaces WHERE id=?',(ident,)).fetchone():
            raise ValueError('workspace_not_found')
        return ident
    def catalog(self):
        with self.history.lock:rows=[dict(r) for r in self.history.db.execute('SELECT * FROM workspaces ORDER BY name,path')]
        counts=Counter(row['name'][:30] for row in rows)
        for row in rows:
            row['name']=row['name'][:30]+(' · '+row['id'][-6:] if counts[row['name'][:30]]>1 else '')
        return rows
    def _observe(self,agent,path,source,at=None):
        observed=time.time() if at is None else float(at)
        row=self.history.db.execute('SELECT observed FROM agent_workspaces WHERE agent_id=?',(agent,)).fetchone()
        if row and observed<row['observed']:return
        ident=self._ensure(path) if path else None
        self.history.db.execute('''INSERT INTO agent_workspaces(agent_id,detected_id,detected_source,observed) VALUES (?,?,?,?)
            ON CONFLICT(agent_id) DO UPDATE SET detected_id=excluded.detected_id,detected_source=excluded.detected_source,observed=excluded.observed''',
            (agent,ident,source,observed))
    def observe(self,agent,path,source,at=None):
        with self.history.lock,self.history.db:self._observe(agent,path,source,at)
    def _assign(self,agent,ident=None,mode='manual'):
        if mode not in {'auto','manual'} or (mode=='auto' and ident is not None):raise ValueError('invalid_workspace_selection')
        if ident:self.require(ident)
        self.history.db.execute('''INSERT INTO agent_workspaces(agent_id,mode,manual_id) VALUES (?,?,?)
            ON CONFLICT(agent_id) DO UPDATE SET mode=excluded.mode,manual_id=excluded.manual_id''',(agent,mode,ident))
    def assign(self,agent,ident=None,mode='manual'):
        with self.history.lock,self.history.db:self._assign(agent,ident,mode)
        return self.for_agent(agent)
    def for_agent(self,agent,catalog=None):
        with self.history.lock:row=self.history.db.execute('SELECT * FROM agent_workspaces WHERE agent_id=?',(agent,)).fetchone()
        mode=row['mode'] if row else 'auto';ident=(row['manual_id'] if mode=='manual' else row['detected_id']) if row else None
        record=next((w for w in (catalog if catalog is not None else self.catalog()) if w['id']==ident),UNASSIGNED)
        return {'id':record['id'],'name':record['name'],'mode':mode,'source':'owner' if mode=='manual' else row['detected_source'] if row else None}
    def known_parent(self,folder):
        folder=Path(workspace_path(str(folder)))
        matches=[w['path'] for w in self.catalog() if Path(w['path'])==folder or Path(w['path']) in folder.parents]
        return max(matches,key=len) if matches else None
