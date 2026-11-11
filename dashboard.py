#!/usr/bin/env python3
"""Local, read-only bot observers and an ntfy-only loopback proxy.

No prompts, tool arguments, responses, credentials, or source titles are published.
The inventory explicitly selects Codex App chats. Crouton lease/status files are
read locally; neither the feed secret nor user data is sent to its model.
"""
import argparse
import datetime as dt
import hashlib
import hmac
import http.server
import json
import mimetypes
import os
from pathlib import Path
import queue
import re
import signal
import sys
import uuid
import sqlite3
from contextlib import closing, nullcontext
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from history import HistoryStore, redact
from configuration import DEFAULT_CONFIG, codex_targets, load_config
from event_ingest import ingest, EventConflict, validate_event
from invitations import Invitations, InvitationError
from public_reporting import ReportingError
from workspaces import Workspaces, workspace_path
from commands import Commands, CommandError
from subagents import ObservedChildren, seconds

ROOT = Path(__file__).resolve().parent
PRIVATE_CONFIG = DEFAULT_CONFIG
ORIGIN = 'https://bodila51.github.io'
SAFE_STATUSES = {'working', 'done', 'idle', 'waiting_user', 'error'}
TERMINALS = {'task_complete': ('done', '已完成'),
             'task_aborted': ('idle', '回合已中止'),
             'turn_aborted': ('idle', '回合已中止'),
             'task_failed': ('error', '執行失敗'),
             'turn_failed': ('error', '執行失敗')}


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def parsed_time(value):
    try:
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, AttributeError, TypeError):
        return 0


def atomic_json(path, data):
    tmp = path.with_name(path.name + '.tmp')
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def bounded_json(path, limit=256 * 1024):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as f:
        s = os.fstat(f.fileno())
        if not stat.S_ISREG(s.st_mode) or s.st_size > limit:
            raise ValueError('Invalid source file')
        return json.load(f)


def alive(pid):
    if type(pid) is not int or pid < 2:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class CodexTail:
    """Verify session identity, then consume only event types and timestamps."""
    def __init__(self, identity, path, on_history=None, on_message=None):
        self.identity = identity
        self.path = Path(path)
        self.offset = 0
        self.inode = None
        self.pending = b''
        self.initialized = False
        self.run = None
        self.run_started = 0
        self.native_root_run = None
        self.agent_path = '/root'
        self.last_event = 0
        self.state = ('waiting_user', '未能確認來源狀態')
        self.state_key = None
        self.on_history = on_history
        self.on_message = on_message
        self.final_text = ''

    def history_event(self, status, rec, **fields):
        if self.on_history and self.run:
            self.on_history(run_id=self.run, status=status, at=rec.get('timestamp'), **fields)

    def public_message(self, role, phase, text, rec):
        if self.on_message and self.run and text:
            self.on_message(run_id=self.run, role=role, phase=phase, text=text, at=rec.get('timestamp'))

    def accept(self, rec):
        p = rec.get('payload')
        if not isinstance(p, dict):
            return None
        if rec.get('type') == 'response_item' and p.get('type') == 'message' and self.run:
            texts = [x.get('text', '') for x in p.get('content', []) if isinstance(x, dict) and isinstance(x.get('text'), str)]
            if p.get('role') == 'user' and self.state[0] == 'working':
                texts = [x for x in texts if not x.lstrip().startswith(('<environment_context>', '# AGENTS.md instructions', '<system_reminder>'))]
                if texts:
                    self.history_event('working', rec, prompt='\n\n'.join(texts))
                    self.public_message('user', 'prompt', '\n\n'.join(texts), rec)
            elif p.get('role') == 'assistant' and (p.get('phase') == 'commentary' or p.get('channel') == 'commentary'):
                self.public_message('assistant', 'commentary', '\n'.join(texts), rec)
            elif p.get('role') == 'assistant' and (p.get('phase') == 'final_answer' or p.get('channel') == 'final'):
                self.final_text = '\n'.join(texts)
                self.public_message('assistant', 'final', self.final_text, rec)
        if rec.get('type') == 'response_item' and self.state[0] == 'working':
            if p.get('type') in {'function_call', 'custom_tool_call', 'function_call_output', 'custom_tool_call_output', 'reasoning'} or (p.get('type') == 'message' and p.get('role') == 'assistant'):
                self.last_event = max(self.last_event, parsed_time(rec.get('timestamp')))
        if rec.get('type') != 'event_msg':
            return None
        kind = p.get('type')
        stamp = parsed_time(rec.get('timestamp'))
        if kind == 'token_count' and self.state[0] == 'working':
            self.last_event = max(self.last_event, stamp)
        if kind == 'task_started':
            run = p.get('turn_id')
            if not isinstance(run, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', run):
                return None
            self.run = run
            self.run_started = seconds(p.get('started_at')) or stamp
            self.native_root_run = p.get('root_turn_id') or run
            self.state = ('working', '處理訊息')
            self.final_text = ''
            self.history_event('working', rec, started_at=p.get('started_at'))
        elif kind in TERMINALS:
            if self.run and p.get('turn_id') and p['turn_id'] != self.run:
                return None
            self.run = p.get('turn_id', self.run)
            # Codex may emit task_complete with an error when a model is retired.
            self.state = ('error', '回合失敗；請查看原對話') if p.get('error') else TERMINALS[kind]
            history_status = 'cancelled' if self.state[0] == 'idle' else self.state[0]
            self.history_event(history_status, rec, delivery=p.get('last_agent_message') or self.final_text,
                               started_at=p.get('started_at'))
            self.public_message('assistant', 'final', p.get('last_agent_message') or self.final_text, rec)
        else:
            return None
        self.last_event = stamp
        key = (self.run, kind, rec.get('timestamp'))
        if key == self.state_key:
            return None
        self.state_key = key
        return {'status': self.state[0], 'step': self.state[1], 'source_time': rec.get('timestamp'), 'run_id':self.run}

    def read(self):
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as f:
            s = os.fstat(f.fileno())
            if not stat.S_ISREG(s.st_mode):
                raise ValueError('Invalid rollout')
            if self.inode is not None and (self.inode != s.st_ino or s.st_size < self.offset):
                raise ValueError('Rollout identity changed')
            if self.inode is None:
                line = f.readline(8 * 1024 * 1024)
                first = json.loads(line)
                if first.get('type') != 'session_meta' or first.get('payload', {}).get('id') != self.identity:
                    raise ValueError('Session identity mismatch')
                self.agent_path = first['payload'].get('agent_path') or '/root'
                self.inode = s.st_ino
                # A bounded initial tail is enough for last start/end events.
                start = max(len(line), s.st_size - 8 * 1024 * 1024)
                f.seek(start)
                if start > len(line):
                    f.readline(8 * 1024 * 1024)
                self.offset = f.tell()
            f.seek(self.offset)
            data = f.read(16 * 1024 * 1024)
            self.offset += len(data)
        self.pending += data
        if len(self.pending) > 24 * 1024 * 1024:
            raise ValueError('Rollout line too large')
        lines = self.pending.split(b'\n')
        self.pending = lines.pop()
        events = []
        for line in lines:
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            event = self.accept(rec)
            if event:
                events.append(event)
        if not self.initialized:
            self.initialized = True
            status, step = self.state
            if status == 'done':
                status, step = 'idle', '未執行；上次回合已結束'
            if status == 'working' and time.time() - self.last_event > 900:
                status, step = 'waiting_user', '來源狀態過期；未確認完成'
            return [{'status': status, 'step': step, 'initial': True, 'run_id':self.run}]
        if self.state[0] == 'working' and time.time() - self.last_event > 1800 and not events:
            return [{'status': 'waiting_user', 'step': '來源長時間無回合結束；需查看對話'}]
        return events


class CroutonSource:
    def __init__(self, root, workspace_root=None):
        self.root = Path(root).resolve()
        self.allowed_roots = [self.root]
        if workspace_root:
            self.allowed_roots.append(Path(workspace_root).resolve())
        self.last = {}

    def inspect(self, agent):
        lease_path = self.root / 'agents' / (agent + '.lock')
        try:
            lease = bounded_json(lease_path)
        except FileNotFoundError:
            previous = self.last.get(agent)
            if not previous:
                return {'status': 'idle', 'step': '按需啟動；目前未執行', 'source_state': 'not_running'}
            lease = previous
        batch, run = lease.get('batch_id'), lease.get('run_id')
        if not re.fullmatch(r'[a-f0-9-]{36}', batch or '') or not re.fullmatch(re.escape(agent) + r'-[a-f0-9-]{36}', run or ''):
            raise ValueError('Invalid lease')
        folder = Path(lease.get('runDir') or self.root / 'batches' / batch / agent)
        if not folder.is_absolute() or folder.resolve() != folder:
            raise ValueError('Invalid lease path')
        if not any(root == folder or root in folder.parents for root in self.allowed_roots):
            raise ValueError('Lease path outside selected workspace roots')
        record = bounded_json(folder / 'status.json')
        if 'run_id' in record:
            if record.get('agent_id') != agent or record['run_id'] != run:
                raise ValueError('Run identity mismatch')
            worker_pid = record.get('workerPid')
        else:
            # Long-running local callers reserve the same routing ID but store
            # a camelCase status beside their job. Bind both IDs to the lease.
            if record.get('runId') != run or record.get('batchId') != batch:
                raise ValueError('Run identity mismatch')
            if record.get('agent_id', agent) != agent:
                raise ValueError('Agent identity mismatch')
            worker_pid = record.get('pid')
            if record.get('status') in {'starting', 'running'} and worker_pid != lease.get('pid'):
                raise ValueError('Worker identity mismatch')
        source_state = record.get('status')
        mapping = {'queued': ('working', '等待執行'), 'starting': ('working', '正在啟動'),
                   'running': ('working', '處理訊息'), 'completed': ('done', '已完成'),
                   'failed': ('error', '執行失敗'), 'timed_out': ('error', '執行逾時'),
                   'cancelled': ('idle', '已取消'), 'skipped': ('idle', '本次未執行'),
                   'interrupted': ('idle', '已中止')}
        if source_state not in mapping:
            raise ValueError('Unknown runtime state')
        if source_state in {'queued', 'starting', 'running'} and not alive(lease.get('pid')):
            raise ValueError('Missing supervisor')
        if source_state in {'starting', 'running'} and not alive(worker_pid):
            raise ValueError('Missing worker')
        self.last[agent] = dict(lease)
        status, step = mapping[source_state]
        return {'status': status, 'step': step, 'source_state': source_state, 'run_id': run, 'source_ref': str(folder)}


class Bridge:
    def __init__(self, config, dry_run=False):
        self.config = config
        self.run_dir = Path(config['run_dir'])
        self.dry_run = dry_run
        self.stop = threading.Event()
        self.outbound = queue.Queue(maxsize=1000)
        self.counts = {'published': 0, 'publish_errors': 0, 'codex_events': 0, 'crouton_events': 0}
        self.last = {}
        self.sources = {}
        self.tails = {}
        self.crouton = CroutonSource(config['crouton_root'], config.get('crouton_workspace_root')) if config.get('crouton_root') else None
        self.health = {'state': 'starting'}
        self.limit_until = 0
        self.backoff_path = Path(config['data_dir']) / 'publisher-backoff.json'
        self.restore_publish_pause()
        self.definitions = {a['id']: a for a in config['agents']}
        self.codex_targets = codex_targets(config)
        self.names = {sid:a['name'] for sid,a in self.codex_targets.items()}
        self.crouton_ids = [a['source_id'] for a in config['agents'] if a['driver']=='crouton']
        if any(not re.fullmatch(r'crouton-(0[1-9]|10)', a) for a in self.crouton_ids):
            raise ValueError('Invalid selected Crouton identity')
        self.crouton_names = {a['source_id']: a['name'] for a in config['agents'] if a['driver']=='crouton'}
        self.agents = {a['id']: {'id':a['id'],'name':a['name'],'provider':a['provider'],
                       'native_url':'codex://threads/'+a['source_id'] if a['driver']=='codex' else None} for a in config['agents']}
        self.allowed_names = {a['name'] for a in self.agents.values()}
        self.history = HistoryStore(Path(config['data_dir']) / 'history.sqlite3', secrets=(config['topic'],))
        self.commands = Commands(config,self.history,enabled=not dry_run,on_status=self.native_status)
        self.workspaces = Workspaces(self.history)
        from office_members import OfficeMembers
        self.members = OfficeMembers(self)
        for a in config['agents']:
            if a['driver']!='push':
                continue
            with self.history.lock:
                live=self.history.db.execute('SELECT * FROM live_runs WHERE agent_id=?',(a['id'],)).fetchone()
                inbox=self.history.db.execute("SELECT run_id,status,updated_at FROM runs WHERE agent_id=? AND source_kind='agent_inbox' ORDER BY updated_at DESC LIMIT 1",(a['id'],)).fetchone()
            restored_inbox=bool(inbox and (not live or inbox['updated_at']>live['updated_at']))
            if restored_inbox:live=inbox
            status = live['status'] if live else 'waiting_user'
            if status=='working':
                status='waiting_user'  # A restart requires fresh evidence from push-only sources.
            if status=='cancelled':
                status='idle'
            step=('上次 OFFICE 指示未確認完成；請查看原聊天' if status=='waiting_user' else '上次 OFFICE 指示已結束') if restored_inbox else ('等待來源更新' if status=='waiting_user' else '上次回合已結束')
            self.sources[a['name']]={'status':status,'step':step,'run_id':live['run_id'] if live else None,
                                     'provider':a['provider'],'observed_at':utc(),'changed_at':live['updated_at'] if live else utc()}
        self.history_seen = {}
        from agent_inbox import Inbox
        self.inbox = Inbox(self)
        self.commands.inbox = self.inbox
        self.invitations = Invitations(config,self.history,self.activate_push_agent,self.workspaces,conversation=self.inbox.ensure,capability=self.commands.capability,lifecycle_lock=self.commands.lock,
                                      reporting_setup=lambda agent_id:self.get_public_reporting().ensure(agent_id))
        from connections import Connections
        self.connections = Connections(self)
        from connection_checks import ConnectionChecks
        self.connection_checks = ConnectionChecks(self.commands)
        self.commands.connection_checks = self.connection_checks
        self.commands.public_sync_observer = self.public_sync_observer
        for a in config['agents']:
            if a['driver']=='hermes':
                settings=config.get('command_adapters',{}).get('hermes_acp',{}).get('sessions',{}).get(a['source_id'],{})
                if settings.get('cwd'):self.workspaces.observe(a['id'],settings['cwd'],'hermes_native')
                with self.commands.lock:
                    uncertain=self.commands.db.execute("SELECT 1 FROM commands WHERE agent_id=? AND state='delivery_unknown' LIMIT 1",(a['id'],)).fetchone()
                self.sources[a['name']]={'status':'waiting_user' if uncertain else 'idle',
                    'step':'上次執行結果未確認；不會自動重送' if uncertain else 'Hermes 已接入；收到指示時背景啟動',
                    'provider':'hermes','observed_at':utc(),'changed_at':utc()}
        self.refresh_connections(config)
        self.children = ObservedChildren(self)
        from child_events import ChildEvents
        self.child_events = ChildEvents(self)
        # Redaction includes only explicitly configured platform credentials.
        from platform_adapters import secret_file
        for kind in ('cursor_cloud','grok_webhook'):
            for settings in config.get('command_adapters',{}).get(kind,{}).get('sessions',{}).values():
                try:
                    secrets=secret_file(settings['credential_file'])
                    self.history.secrets=(*self.history.secrets,*(v for v in secrets.values() if isinstance(v,str)))
                except (ValueError,OSError,KeyError):pass

    def native_status(self, agent, event):
        info=self.agents.get(agent)
        if not info:return
        label='Cursor' if info['provider']=='cursor' else 'Hermes'
        kind=event['type'];status='working';step=label+' 正在處理指示'
        if kind=='status':
            status='waiting_user' if event.get('waiting') else 'working';step=event['step']
        elif kind in {'finished','interrupted'}:
            status={'completed':'done','failed':'error','cancelled':'idle','delivery_unknown':'waiting_user'}[event['state']]
            step={'done':'已完成','error':label+' 執行失敗；請查看紀錄','idle':'已中止','waiting_user':'執行結果未確認；不會自動重送'}[status]
        self.report(info['name'],{'status':status,'step':step,'run_id':event['run_id']},info['provider'],agent_id=agent)

    def get_connection_checks(self):
        """Keep checks attached to the active durable command store."""
        with self.commands.lock:
            current=getattr(self,'connection_checks',None)
            if current is None or current.commands is not self.commands:
                from connection_checks import ConnectionChecks
                current=self.connection_checks=ConnectionChecks(self.commands)
            self.commands.connection_checks=current
            self.commands.public_sync_observer=self.public_sync_observer
            return current

    def get_public_reporting(self):
        """One install-local reporting setup service; no native model or queue."""
        with self.commands.lock,self.history.lock:
            current=getattr(self,'public_reporting',None)
            if current is None:
                from public_reporting import PublicReporting
                current=self.public_reporting=PublicReporting(self)
            return current

    def public_sync_observer(self, agent_id):
        """Describe an activated scoped Hook, never infer it from dispatch."""
        root=getattr(self,'definitions',{}).get(agent_id)
        if not root or root.get('provider')!='cursor' or root.get('driver')!='push':
            return False
        if not hasattr(self,'child_events'):
            return False
        with self.commands.lock,self.history.lock:
            try:
                row=self.history.db.execute('''SELECT o.native_session_id FROM child_observers o
                    JOIN observed_agent_roots r ON r.root_id=o.root_id AND r.generation=o.generation
                    WHERE o.root_id=? AND o.active=1 AND r.active=1 AND o.provider='cursor'
                    AND o.source_kind='cursor_hook' AND o.evidence='native_hook'
                    ORDER BY o.rowid DESC LIMIT 1''',(agent_id,)).fetchone()
                return bool(row and row['native_session_id'] and not row['native_session_id'].startswith('pending:'))
            except sqlite3.Error:
                return False

    def refresh_connections(self, config):
        # Called under command -> history locks. Running adapters remain alive;
        # only owner-selected descriptors change. No service restart is needed.
        selected={a['id'] for a in config['agents']}
        names={a['name'] for a in config['agents']}
        self.agents={k:v for k,v in self.agents.items() if k in selected}
        self.allowed_names=names
        self.sources={k:v for k,v in self.sources.items() if k in names}
        self.last={k:v for k,v in self.last.items() if k in names}
        for a in config['agents']:
            if a['id'] not in self.definitions:
                self.activate_push_agent(a)
            if a['driver']=='cursor':
                self.agents[a['id']]['native_url']='https://cursor.com/agents/'+a['source_id']
                if a['name'] not in self.sources or self.sources[a['name']].get('step')=='等待 Agent 首次回報':
                    with self.commands.lock:
                        previous=self.commands.db.execute('SELECT state FROM commands WHERE agent_id=? ORDER BY created_at DESC LIMIT 1',(a['id'],)).fetchone()
                    pending=previous and previous['state'] in {'queued','running','waiting_user','delivery_unknown'}
                    self.sources[a['name']]={'status':'waiting_user' if pending else 'idle','step':'正在恢復原生執行紀錄' if pending else 'Cursor 已接入；等待 OFFICE 指示','provider':'cursor','observed_at':utc(),'changed_at':utc()}
        self.config['agents']=config['agents']
        self.config['command_bindings']=config.get('command_bindings',{})
        self.config['command_adapters']=config.get('command_adapters',{})
        for key in ('disabled_command_agents','logged_out_agent_ids','revoked_invitation_ids'):
            self.config[key]=config.get(key,[])
        self.commands.bindings=self.config['command_bindings']
        self.commands.migrate_pending_inbox()
        self.definitions={a['id']:a for a in config['agents']}
        self.codex_targets=codex_targets(config)
        self.names={sid:a['name'] for sid,a in self.codex_targets.items()}
        self.tails={sid:tail for sid,tail in self.tails.items() if sid in self.names}
        self.crouton_ids=[a['source_id'] for a in config['agents'] if a['driver']=='crouton']
        self.crouton_names={a['source_id']:a['name'] for a in config['agents'] if a['driver']=='crouton'}
        for sid,a in self.codex_targets.items():
            self.agents[a['id']]['native_url']='codex://threads/'+sid
        for key in ('cursor_cloud','grok_webhook'):
            adapter=self.commands.adapters.get(key)
            if adapter:adapter.sessions=self.config['command_adapters'].get(key,{}).get('sessions',{})
        self.members.sync(config)
        if hasattr(self, 'children'):
            self.children.sync()
        if hasattr(self,'child_events'):
            self.child_events.refresh_index()

    def activate_push_agent(self, agent):
        # Copy-on-write keeps the observer's current iteration stable during enrollment.
        with self.commands.lock,self.history.lock:
            if agent['id'] in self.definitions:
                return
            self.sources = {**self.sources, agent['name']:{'status':'waiting_user','step':'等待 Agent 首次回報',
                'provider':agent['provider'],'observed_at':utc(),'changed_at':utc()}}
            self.definitions = {**self.definitions,agent['id']:agent}
            self.agents = {**self.agents,agent['id']:{'id':agent['id'],'name':agent['name'],
                'provider':agent['provider'],'native_url':None}}
            self.allowed_names = self.allowed_names | {agent['name']}
            self.config['agents'] = [*self.config['agents'],agent]
            self.members.sync(self.config)
            # A newly joined native observer can be installed immediately in
            # the same entry turn, before the periodic member poll runs.
            if hasattr(self, 'children'):
                self.children.sync()
            if hasattr(self, 'child_events'):
                self.child_events.refresh_index()

    def record_codex(self, ident, **event):
        with self.commands.lock,self.history.lock:return self._record_codex(ident,**event)

    def _record_codex(self, ident, **event):
        if ident not in self.codex_targets:return
        try:
            definition=self.codex_targets[ident];agent_id=definition['id']
            if parsed_time(event.get('at'))<parsed_time(definition.get('native_observe_after')):return
            if event.get('prompt'):
                event['prompt']=self.commands.observe_prompt(agent_id,event['run_id'],event['prompt'])
            self.history.record(agent_id, 'codex', self.names[ident], source_kind='codex_turn', **event)
            self.commands.observe_run(agent_id,event['run_id'],event['status'])
        except (OSError, sqlite3.Error):
            self.health['history_error'] = 'history_write_failed'

    def message_codex(self, ident, **event):
        with self.commands.lock,self.history.lock:return self._message_codex(ident,**event)

    def _message_codex(self, ident, **event):
        if ident not in self.codex_targets:return
        try:
            definition=self.codex_targets[ident];agent_id=definition['id']
            if parsed_time(event.get('at'))<parsed_time(definition.get('native_observe_after')):return
            if event.get('role')=='user':
                original=event['text']
                event['text']=self.commands.observe_prompt(agent_id,event['run_id'],original)
                if event['text']!=original:
                    with self.history.lock:
                        migrated=self.history.db.execute("SELECT 1 FROM messages WHERE agent_id=? AND run_id=? AND event_id LIKE 'inbox:%:prompt'",(agent_id,event['run_id'])).fetchone()
                    if migrated:return
            self.history.message(agent_id, **event)
        except (OSError, sqlite3.Error):
            self.health['history_error'] = 'message_write_failed'

    def capture_crouton(self, agent, folder, expected_run=None):
        with self.commands.lock if hasattr(self,'commands') else nullcontext():
            if hasattr(self,'definitions') and 'crouton:'+agent not in self.definitions:return
            return self._capture_crouton(agent,folder,expected_run)

    def _capture_crouton(self, agent, folder, expected_run=None):
        """Only explicit text fields from the bound run; never internal reasoning or tool outputs."""
        folder = Path(folder)
        if folder.resolve() != folder or not any(root == folder or root in folder.parents for root in self.crouton.allowed_roots):
            raise ValueError('Invalid history source')
        status_path = folder / 'status.json'
        result_path = folder / 'result.json'
        sig = (status_path.stat().st_mtime_ns, result_path.stat().st_mtime_ns if result_path.exists() else 0)
        if self.history_seen.get(str(folder)) == sig:
            return
        record = bounded_json(status_path, limit=4 * 1024 * 1024)
        run = record.get('run_id') or record.get('runId')
        if not re.fullmatch(re.escape(agent) + r'-[a-f0-9-]{36}', run or '') or (expected_run and run != expected_run):
            raise ValueError('History run mismatch')
        if record.get('agent_id', agent) != agent:
            raise ValueError('History agent mismatch')
        if result_path.exists():
            result = bounded_json(result_path, limit=4 * 1024 * 1024)
            result_run = result.get('run_id') or result.get('runId')
            # Some runtimes save a raw tool/service result here. Its content is
            # not an assistant delivery unless it is bound to this exact run.
            if result_run:
                if result_run != run or result.get('agent_id', agent) != agent:
                    raise ValueError('History result identity mismatch')
                record.update(result)
        status = {'queued': 'working', 'starting': 'working', 'running': 'working', 'completed': 'done',
                  'failed': 'error', 'timed_out': 'error', 'cancelled': 'cancelled', 'interrupted': 'cancelled', 'skipped': 'cancelled'}.get(record.get('status'))
        if not status:
            return
        if status == 'working':
            try:
                live = self.crouton.inspect(agent)
                if live.get('run_id') != run or live.get('status') != 'working':
                    status = 'unknown'
            except (OSError, ValueError):
                status = 'unknown'
        prompt = None
        request_path = folder / 'request.json'
        if request_path.exists():
            request = bounded_json(request_path, limit=1024 * 1024)
            if request.get('run_id') == run and request.get('job', {}).get('agent_id') == agent:
                prompt = request['job'].get('instruction')
                detected_workspace = request['job'].get('workspace')
            else:
                detected_workspace = None
        else:
            detected_workspace = None
        workspace_source = 'crouton_request' if detected_workspace else 'run_location'
        if not detected_workspace:
            detected_workspace = self.workspaces.known_parent(folder)
        observed_at = parsed_time(record.get('startedAt') or record.get('finishedAt') or record.get('updatedAt')) or status_path.stat().st_mtime
        try:
            self.workspaces.observe('crouton:'+agent,detected_workspace,workspace_source,observed_at)
        except ValueError:
            self.workspaces.observe('crouton:'+agent,None,'unavailable',observed_at)
        delivery = next((record[k] for k in ('text', 'summary', 'delivery') if isinstance(record.get(k), str)), None)
        self.history.record('crouton:' + agent, 'crouton', self.crouton_names[agent], run, status,
                            at=record.get('finishedAt') or record.get('updatedAt') or record.get('startedAt'),
                            started_at=record.get('startedAt'), prompt=prompt, delivery=delivery,
                            source_ref=str(folder), source_kind='crouton_run')
        if prompt:
            self.history.message('crouton:' + agent, run, 'user', 'prompt', prompt,
                                 at=record.get('startedAt'), event_id=run + ':prompt')
        if delivery:
            self.history.message('crouton:' + agent, run, 'assistant', 'final', delivery,
                                 at=record.get('finishedAt'), event_id=run + ':final')
        self.history_seen[str(folder)] = sig

    def backfill_known_history(self):
        # Only explicitly configured imports and already known local history sources.
        try:
            for item in self.config.get('history_imports', []):
                if item.get('agent') in self.crouton_ids:
                    self.capture_crouton(item['agent'], item['path'])
            for record in self.history.references(self.agents):
                if record['agent_id'].startswith('crouton:'):
                    self.capture_crouton(record['agent_id'].split(':', 1)[1], record['source_ref'], record['run_id'])
        except (OSError, ValueError, sqlite3.Error):
            self.health['history_error'] = 'history_backfill_incomplete'
        # Workspace recovery includes completed deliveries, which do not need
        # history backfill. Read only the latest known run of selected helpers.
        for agent in self.crouton_ids:
            with self.history.lock:
                row=self.history.db.execute("SELECT run_id,source_ref FROM runs WHERE agent_id=? AND source_ref!='' ORDER BY started_at DESC LIMIT 1",('crouton:'+agent,)).fetchone()
            if row:
                try:self.capture_crouton(agent,row['source_ref'],row['run_id'])
                except (OSError,ValueError,sqlite3.Error):pass

    def local_ready(self):
        return not self.health.get('source_lookup_error') and all(info['name'] in self.sources for info in self.agents.values())

    def work_snapshot(self):
        with self.commands.lock,self.history.lock:return self._work_snapshot()

    def history_agents(self):
        """Observation grants read access only, never native action authority."""
        return dict(self.agents, **(self.children.allowed() if hasattr(self,'children') else {}))

    def _work_snapshot(self):
        agents = []
        seating=self.members.snapshot() if hasattr(self,'members') else {'placements':{},'revision':0}
        catalog = self.workspaces.catalog()
        latest = self.history.latest_messages(self.agents)
        for info in self.agents.values():
            source = self.sources.get(info['name'], {})
            message = latest.get(info['id'])
            if message and source.get('run_id') and message['run_id'] != source['run_id']:
                message = None
            if message:
                message = dict(message, text=message['text'][:240])
            agents.append(dict(info, state=source.get('status', 'waiting_user'),
                               ground_desk_id=seating['placements'].get(info['id']),
                               command_capability=self.commands.capability(info['id']),
                               workspace=self.workspaces.for_agent(info['id'],catalog),
                               step=source.get('step', '未能確認來源狀態'),
                               state_changed_at=source.get('changed_at'),
                               latest_message=message))
        child_agents=self.children.snapshot({a['id']:a for a in agents}) if hasattr(self,'children') else []
        for agent in agents:
            agent['child_count']=sum(c['root_agent_id']==agent['id'] for c in child_agents)
        finished=self.history.list(self.history_agents(), terminal=True)
        if hasattr(self,'children'):finished=self.children.decorate_runs(finished)
        return {'agents': agents+child_agents, 'finished': finished,
                'subagent_count':len(child_agents),
                'subagents_ok':not (self.health.get('subagent_lookup_error') or self.health.get('child_observer_error')),
                'seating_revision':seating['revision'],
                'grouping':'workspace',
                'brand': self.config.get('brand','VIRTUAL OFFICE'), 'feed_enabled': bool(self.config.get('feed')),
                'local_ok': self.local_ready(),
                'history_ok': not self.health.get('history_error')}

    def accept_agent_event(self, agent, event, *, transaction=True):
        # Commit and update the visible state in the same order across HTTP workers.
        with self.commands.lock, self.history.lock:
            current=self.definitions.get(agent['id'])
            if not current or current.get('token_sha256')!=agent.get('token_sha256'):
                raise EventConflict('agent_not_selected')
            validate_event(event,agent['source_id'])
            # A command start must pass exact matching before ingestion creates
            # any run, workspace observation or accepted-event receipt.
            clean=self.commands.grok_start_body(agent,event)
            if agent.get('provider')=='grok_bot' and event['type']=='turn_started' and clean is None:
                prior=self.history.db.execute('SELECT 1 FROM event_receipts WHERE agent_id=? AND event_id=?',
                    (agent['id'],event['event_id'])).fetchone()
                active=self.commands.db.execute("SELECT 1 FROM commands WHERE agent_id=? AND adapter='grok_webhook' "
                    "AND state IN ('sending','queued','running','waiting_user','delivery_unknown') LIMIT 1",(agent['id'],)).fetchone()
                if active and not prior:
                    # A Bot-level routine has no independent chat identity.
                    # An unrelated App turn must not replace its live run and
                    # strand the command's later authenticated completion.
                    raise EventConflict('grok_work_in_progress')
            receipt = ingest(self.history, agent, event, on_start=lambda item:
                self.workspaces._observe(agent['id'],item.get('workspace_path'),agent['provider']+'_report'),
                display_text=clean,transaction=transaction)
            if clean is not None:
                self.commands.observe_prompt(agent['id'],event['run_id'],event['text'])
                with self.history.db:
                    # A migrated inbox message already represents this prompt.
                    prior=self.history.db.execute('SELECT 1 FROM messages WHERE agent_id=? AND event_id=?',
                        (agent['id'],'inbox:'+event['run_id']+':prompt')).fetchone()
                    if prior:self.history.db.execute('DELETE FROM messages WHERE agent_id=? AND event_id=?',
                        (agent['id'],event['event_id']))
                    self.history.db.execute('UPDATE runs SET prompt=? WHERE agent_id=? AND run_id=?',
                        (redact(clean,self.history.secrets),agent['id'],event['run_id']))
                    known=self.history.db.execute('SELECT status FROM runs WHERE agent_id=? AND run_id=?',
                        (agent['id'],event['run_id'])).fetchone()
                # An authenticated duplicate start may reconcile an earlier
                # callback after its real completion is already in history.
                if known:self.commands.observe_run(agent['id'],event['run_id'],known['status'])
            if event.get('type') in {'turn_completed','turn_failed','turn_cancelled'}:
                status={'turn_completed':'done','turn_failed':'error','turn_cancelled':'cancelled'}[event['type']]
                self.commands.observe_run(agent['id'],event['run_id'],status)
            self.get_connection_checks().observe(agent,event,receipt)
            if not receipt['duplicate']:
                state = {'cancelled':'idle'}.get(receipt['status'],receipt['status'])
                self.report(agent['name'], {'status':state,
                    'step':{'working':'處理訊息','done':'已完成','error':'執行失敗','idle':'已中止'}[state],
                    'run_id':receipt['run_id'],'source_time':receipt['at']}, agent['provider'],agent_id=agent['id'])
            return receipt

    def filter_record(self, row):
        """Roster membership is never implicit enrollment."""
        if row.get('event') != 'message':
            return None
        try:
            p = json.loads(row.get('message', ''))
        except (ValueError, TypeError):
            return None
        if not isinstance(p, dict):
            return None
        allowed = self.allowed_names
        if 'roster' in p:
            p = {'roster': [n for n in p['roster'] if n in allowed]} if isinstance(p['roster'], list) else None
        elif any(k in p for k in ('groups', 'looks', 'chat')):
            k = next(k for k in ('groups', 'looks', 'chat') if k in p)
            p = {k: {n: v for n, v in p[k].items() if n in allowed}} if isinstance(p[k], dict) else None
        elif 'routines' in p:
            p = p if isinstance(p['routines'], dict) and p['routines'].get('bot') in allowed else None
        elif 'check' in p:
            p = {'check': {'bots': [n for n in p['check'].get('bots', []) if n in allowed]}} if isinstance(p['check'], dict) else None
        elif p.get('type') == 'usage':
            # Account-wide usage is outside individual-agent enrollment.
            p = None
        elif p.get('bot') not in allowed or p.get('status') not in SAFE_STATUSES:
            p = None
        else:
            fixed = {'working': '處理訊息', 'done': '已完成', 'idle': '待命', 'waiting_user': '等待確認', 'error': '發生錯誤'}
            p = {'bot': p['bot'], 'status': p['status'], 'step': fixed[p['status']]}
        if p is None:
            return None
        return {k: row[k] for k in ('id', 'time', 'event') if k in row} | {'message': json.dumps(p, ensure_ascii=False)}

    def enqueue(self, payload):
        if hasattr(self,'config') and not self.config.get('feed'):
            return
        raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        if len(raw) > 4000:
            raise ValueError('Message exceeds ntfy limit')
        self.outbound.put_nowait(payload)

    def report(self, name, event, provider, agent_id=None):
        with self.commands.lock if hasattr(self,'commands') else nullcontext():return self._report(name,event,provider,agent_id)

    def _report(self, name, event, provider, agent_id=None):
        if hasattr(self,'allowed_names') and name not in self.allowed_names:return
        if agent_id is not None and self.agents.get(agent_id,{}).get('name')!=name:return
        status, step = event['status'], event['step']
        if status not in SAFE_STATUSES:
            raise ValueError('Unknown status')
        key = (status, step, event.get('run_id'), event.get('source_time'))
        observed = utc()
        changed = self.last.get(name) != key
        changed_at = (event.get('source_time') if parsed_time(event.get('source_time')) else observed) if changed else self.sources[name]['changed_at']
        self.sources[name] = {'status': status, 'step': step, 'provider': provider,
                              'observed_at': observed, 'changed_at': changed_at, 'run_id':event.get('run_id')}
        if self.last.get(name) == key:
            return
        self.last[name] = key
        # Publish the current baseline, including idle: retained feed history
        # may otherwise leave a selected agent stuck on its pre-restart state.
        self.counts[provider + '_events'] = self.counts.get(provider + '_events', 0) + 1
        self.enqueue({'bot': name, 'status': status, 'step': step})

    def metadata(self):
        names = [a['name'] for a in self.agents.values()]
        grok_names = {a['name'] for a in self.agents.values() if a['provider'] == 'grok_bot'}
        for start in range(0, len(names), 15):
            chunk = names[start:start + 15]
            self.enqueue({'roster': chunk})
            self.enqueue({'groups': {a['name']: {'codex':'Codex','crouton':'Crouton','cursor':'Cursor','grok_bot':'Grok Bot','hermes':'Hermes'}.get(a['provider'],'Agents') for a in self.agents.values() if a['name'] in chunk}})
            looks = {}
            for name in chunk:
                # Original upstream Grok Bot reference: orange body and dark stem.
                # Choose by registration provider; a display name is not identity.
                if name in grok_names:
                    looks[name] = {'shape': 'sphere', 'color': '#FC6A14', 'accessory': 'stem'}
                else:
                    looks[name] = {'shape': 'rounded_cube' if name.startswith('Codex / ') else 'sphere',
                                   'color': '#4087E8' if name.startswith('Codex / ') else '#6CAC72',
                                   'accessory': 'antenna' if name.startswith('Codex / ') else 'glasses'}
            self.enqueue({'looks': looks})

    def load_paths(self):
        pending = [ident for ident in self.names if ident not in self.tails]
        if not pending:
            return
        con = None
        try:
            con = sqlite3.connect('file:' + self.config['codex_db'] + '?mode=ro', uri=True, timeout=2)
            for ident in pending:
                row = con.execute('SELECT rollout_path FROM threads WHERE id=?', (ident,)).fetchone()
                if row and row[0]:
                    with self.commands.lock if hasattr(self,'commands') else nullcontext():
                        if ident in self.names:
                            self.tails[ident] = CodexTail(ident, row[0], lambda ident=ident, **event: self.record_codex(ident, **event),
                                                        lambda ident=ident, **event: self.message_codex(ident, **event))
            self.health.pop('source_lookup_error', None)
        except (sqlite3.Error, OSError) as error:
            # The app may temporarily replace or make its database unavailable.
            # Keep existing tails and other providers alive; retry missing paths.
            self.health['source_lookup_error'] = type(error).__name__
        finally:
            if con is not None:
                con.close()

    def refresh_codex_workspaces(self):
        if not self.names:
            return
        try:
            with closing(sqlite3.connect('file:'+self.config['codex_db']+'?mode=ro',uri=True,timeout=2)) as con:
                columns={row[1] for row in con.execute('PRAGMA table_info(threads)')}
                if 'cwd' not in columns:
                    return
                for ident in self.names:
                    row=con.execute('SELECT cwd FROM threads WHERE id=?',(ident,)).fetchone()
                    if row and row[0]:
                        with self.commands.lock if hasattr(self,'commands') else nullcontext():
                            target=self.codex_targets.get(ident)
                            if target:
                                try:self.workspaces.observe(target['id'],row[0],'codex_cwd')
                                except ValueError:pass
        except (OSError,sqlite3.Error):
            pass  # Keep the last observed workspace while the source is unavailable.

    def restore_publish_pause(self):
        try:
            self.limit_until = float(bounded_json(self.backoff_path).get('until', 0))
        except (OSError, ValueError, TypeError):
            self.limit_until = 0
        if time.time() < self.limit_until:
            self.health['last_publish_error'] = 'HTTP 429'

    def publish_loop(self):
        while not self.stop.is_set():
            if time.time() < self.limit_until:
                self.stop.wait(2)
                continue
            try:
                payload = self.outbound.get(timeout=1)
            except queue.Empty:
                continue
            raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
            receipt = {'at': utc(), 'payload_sha256': hashlib.sha256(raw).hexdigest(),
                       'kind': 'status' if 'bot' in payload else next(iter(payload)),
                       'bot': payload.get('bot'), 'status': payload.get('status')}
            try:
                if self.dry_run:
                    response = {'id': 'dry-run'}
                else:
                    req = urllib.request.Request(self.config['feed'], data=raw, method='POST', headers={'Content-Type': 'text/plain; charset=utf-8'})
                    with urllib.request.urlopen(req, timeout=15) as r:
                        response = json.load(r)
                    if response.get('event') != 'message' or response.get('message') != raw.decode():
                        raise ValueError('Unverified receipt')
                receipt.update(result='confirmed', message_id=response.get('id'))
                self.counts['published'] += 1
                self.health.pop('last_publish_error', None)
            except urllib.error.HTTPError as e:
                self.counts['publish_errors'] += 1
                receipt.update(result='rejected', http_status=e.code)
                if e.code == 429:
                    self.limit_until = time.time() + 3600
                    atomic_json(self.backoff_path, {'until': self.limit_until})
                    receipt['pause_seconds'] = 3600
                self.health['last_publish_error'] = 'HTTP %s' % e.code
            except Exception as e:
                self.counts['publish_errors'] += 1
                receipt.update(result='unconfirmed', error_type=type(e).__name__)
                self.health['last_publish_error'] = type(e).__name__
            with open(self.run_dir / 'receipts.jsonl', 'a') as f:
                f.write(json.dumps(receipt, ensure_ascii=False) + '\n')
            self.outbound.task_done()
            self.stop.wait(1.1)

    def status(self):
        state = {'run_id': self.config['run_id'], 'pid': os.getpid(),
                 'stop_target': '/bin/kill -TERM %s' % os.getpid(), 'updated_at': utc(),
                 'state': 'running' if not self.stop.is_set() else 'stopped', 'dry_run': self.dry_run,
                 'counts': self.counts.copy(), 'queue_depth': self.outbound.qsize(),
                 'source_count': len(self.sources), 'sources': self.sources.copy(),
                 'limits_paused': time.time() < self.limit_until,
                 'publisher_resume_at': self.limit_until,
                 'last_publish_error': self.health.get('last_publish_error'),
                 'source_lookup_error': self.health.get('source_lookup_error'),
                 'history_error': self.health.get('history_error')}
        atomic_json(self.run_dir / 'status.json', state)
        return state

    def observe_loop(self):
        self.load_paths()
        self.refresh_codex_workspaces()
        self.backfill_known_history()
        self.metadata()
        next_metadata = time.time() + 6 * 3600
        next_paths = time.time() + 30
        while not self.stop.is_set():
            if time.time() >= next_paths:
                self.load_paths()
                self.refresh_codex_workspaces()
                next_paths = time.time() + 30
            for ident,name,agent_id in [(sid,a['name'],a['id']) for sid,a in self.codex_targets.items()]:
                try:
                    tail = self.tails.get(ident)
                    if tail is None:
                        raise ValueError('Missing selected session')
                    for event in tail.read():
                        self.report(name, event, 'codex',agent_id=agent_id)
                except Exception:
                    self.report(name, {'status': 'waiting_user', 'step': '未能讀取來源；狀態未知'}, 'codex',agent_id=agent_id)
            self.children.poll()
            try:self.child_events.poll()
            except (OSError,ValueError,sqlite3.Error):self.health['child_observer_error']='child_observer_unavailable'
            for agent,name in list(self.crouton_names.items()):
                try:
                    event = self.crouton.inspect(agent)
                    if name not in self.last and event['status'] == 'idle':
                        event['initial'] = True
                    self.report(name, event, 'crouton',agent_id='crouton:'+agent)
                    if event.get('source_ref'):
                        try:
                            self.capture_crouton(agent, event['source_ref'], event['run_id'])
                        except (OSError, ValueError, sqlite3.Error):
                            self.health['history_error'] = 'history_capture_failed'
                except Exception:
                    self.report(name, {'status': 'waiting_user', 'step': '未能確認執行器；狀態未知'}, 'crouton',agent_id='crouton:'+agent)
            for definition in self.definitions.values():
                if definition['driver']=='push':
                    source=self.sources.get(definition['name'],{})
                    if source.get('status')=='working' and time.time()-parsed_time(source.get('observed_at'))>1800:
                        self.report(definition['name'], {'status':'waiting_user','step':'來源暫無更新；未確認完成'},definition['provider'],agent_id=definition['id'])
            if time.time() >= next_metadata:
                self.metadata()
                next_metadata = time.time() + 6 * 3600
            self.status()
            self.stop.wait(1)
        state = self.status()
        atomic_json(self.run_dir / 'result.json', state)


def proxy_handler(bridge):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass  # Never log the topic-bearing URL.

        def allowed(self):
            return (self.headers.get('Host') in {'127.0.0.1:%s' % bridge.config['port'], 'localhost:%s' % bridge.config['port']}
                    and self.headers.get('Origin') in {None, ORIGIN, 'http://127.0.0.1:%s' % bridge.config['port'], 'http://localhost:%s' % bridge.config['port']})

        def headers_common(self):
            origin = self.headers.get('Origin', ORIGIN)
            self.send_header('Access-Control-Allow-Origin', origin if origin in {ORIGIN, 'http://127.0.0.1:%s' % bridge.config['port'], 'http://localhost:%s' % bridge.config['port']} else ORIGIN)
            self.send_header('Vary', 'Origin')
            self.send_header('Access-Control-Allow-Private-Network', 'true')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')

        def small(self, code, data):
            body = json.dumps(data).encode()
            self.send_response(code)
            self.headers_common()
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            if not self.allowed():
                return self.small(403, {'error': 'forbidden'})
            self.send_response(204)
            self.headers_common()
            self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.send_header('Content-Length', '0')
            self.end_headers()

        def do_POST(self):
            self.connection.settimeout(5)
            route = urllib.parse.urlsplit(self.path).path
            if not self.allowed():
                self.close_connection = True
                return self.small(403, {'error':'forbidden'})
            local_origins = {None, 'http://127.0.0.1:%s' % bridge.config['port'], 'http://localhost:%s' % bridge.config['port']}
            if self.headers.get('Origin') not in local_origins:
                self.close_connection = True
                return self.small(403, {'error':'forbidden'})
            if route == '/api/office/child-observers':
                if not hmac.compare_digest(self.headers.get('X-Office-Key',''),bridge.config['topic']):
                    self.close_connection=True
                    return self.small(403,{'error':'private_office_required'})
                from child_events import ChildEventError
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if self.headers.get('Transfer-Encoding') or self.headers.get_content_type()!='application/json' or not 0<length<=4096:raise ValueError()
                    return self.small(200,bridge.child_events.create_observer(json.loads(self.rfile.read(length))))
                except ChildEventError as error:return self.small(error.status,{'error':error.code})
                except (ValueError,TypeError,KeyError):return self.small(400,{'error':'invalid_child_observer'})
                except (OSError,sqlite3.Error):return self.small(503,{'error':'child_observer_unavailable'})
            if route in {'/api/agents/children/events','/api/agents/children/reports','/api/agents/observers/root-events',
                         '/api/agents/observers/pending','/api/agents/observers/bind'}:
                from child_events import ChildEventError
                mode='report' if route.endswith(('/reports','/pending')) else 'bind' if route.endswith('/bind') else 'observer'
                ident=self.headers.get('X-Agent-Id' if mode=='report' else 'X-Observer-Id','')
                token=self.headers.get('Authorization','').removeprefix('Bearer ')
                try:
                    scope=bridge.child_events.authorize(mode,ident,token)
                    length=int(self.headers.get('Content-Length','0'))
                    if self.headers.get('Transfer-Encoding') or self.headers.get_content_type()!='application/json' or not 0<length<=100000:raise ValueError()
                    data=json.loads(self.rfile.read(length))
                    if route.endswith('/pending'):
                        if data!={}:raise ValueError()
                        result=bridge.child_events.register_pending(ident,token,expected_generation=scope['generation'])
                    elif route.endswith('/bind'):
                        result=bridge.child_events.bind(ident,token,data,expected_generation=scope['generation'])
                    elif route.endswith('/root-events'):
                        result=bridge.child_events.root_event(ident,token,data,expected_generation=scope['generation'])
                    else:
                        result=bridge.child_events.accept(mode,ident,token,data,expected_generation=scope['generation'])
                    return self.small(200,result)
                except ChildEventError as error:return self.small(error.status,{'error':error.code})
                except EventConflict:return self.small(409,{'error':'root_event_conflict'})
                except (ValueError,TypeError,KeyError):return self.small(400,{'error':'invalid_child_event'})
                except (OSError,sqlite3.Error):return self.small(503,{'error':'child_events_unavailable'})
            if route in {'/api/office/agents/logout','/api/office/seating'}:
                if not hmac.compare_digest(self.headers.get('X-Office-Key',''),bridge.config['topic']):
                    self.close_connection=True
                    return self.small(403,{'error':'private_office_required'})
                from office_members import MemberError
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if self.headers.get('Transfer-Encoding') or self.headers.get_content_type()!='application/json' or not 0<length<=4096:raise ValueError()
                    data=json.loads(self.rfile.read(length))
                    result=bridge.members.logout(data) if route.endswith('/logout') else bridge.members.assign(data)
                    return self.small(200,result)
                except MemberError as error:return self.small(error.status,{'error':error.code})
                except (ValueError,TypeError,KeyError):return self.small(400,{'error':'invalid_member_request'})
                except (OSError,sqlite3.Error):return self.small(503,{'error':'membership_unavailable'})
            if route == '/api/agents/connect':
                agent_id=self.headers.get('X-Agent-Id','')
                agent=bridge.definitions.get(agent_id)
                token=self.headers.get('Authorization','').removeprefix('Bearer ')
                reporter=bool(agent and agent.get('driver')=='push' and token and hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(),agent.get('token_sha256','')))
                if not reporter and not bridge.inbox.authenticated(agent_id,token):
                    self.close_connection=True
                    return self.small(403,{'error':'invalid_agent_credential'})
                from connections import ConnectionError
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if self.headers.get('Transfer-Encoding') or self.headers.get_content_type()!='application/json' or not 0<length<=16000:raise ValueError()
                    data=json.loads(self.rfile.read(length))
                    with bridge.commands.lock,bridge.history.lock:
                        current=bridge.definitions.get(agent_id)
                        reporter=bool(current and current.get('driver')=='push' and token and hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(),current.get('token_sha256','')))
                        if not reporter and not bridge.inbox.authenticated(agent_id,token):return self.small(403,{'error':'invalid_agent_credential'})
                        result=bridge.connections.connect_agent(agent_id,data)
                    return self.small(200,result)
                except ConnectionError as error:return self.small(error.status,{'error':error.code})
                except (ValueError,KeyError,TypeError):return self.small(400,{'error':'invalid_connection'})
                except (OSError,sqlite3.Error):return self.small(503,{'error':'connection_unavailable'})
            if route == '/api/agents/reporting/setup':
                agent_id=self.headers.get('X-Agent-Id','')
                token=self.headers.get('Authorization','').removeprefix('Bearer ')
                with bridge.commands.lock,bridge.history.lock:
                    agent=bridge.definitions.get(agent_id)
                    authorized=bool(agent and agent.get('driver')=='push' and token and
                        hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(),agent.get('token_sha256','')))
                    if not authorized:
                        self.close_connection=True
                        return self.small(403,{'error':'invalid_agent_credential'})
                    try:
                        length=int(self.headers.get('Content-Length','0'))
                        if self.headers.get('Transfer-Encoding') or self.headers.get_content_type()!='application/json' or not 0<length<=4096:
                            raise ValueError()
                        result=bridge.get_public_reporting().confirm(agent_id,json.loads(self.rfile.read(length)))
                        return self.small(200,result)
                    except ReportingError as error:return self.small(error.status,{'error':error.code})
                    except (ValueError,KeyError,TypeError):return self.small(400,{'error':'invalid_reporting_setup'})
                    except (OSError,sqlite3.Error):return self.small(503,{'error':'reporting_setup_unavailable'})
            if route == '/api/office/shutdown':
                if not hmac.compare_digest(self.headers.get('X-Office-Key',''),bridge.config['topic']):
                    self.close_connection = True
                    return self.small(403, {'error':'private_office_required'})
                self.close_connection = True
                bridge.stop.set()
                return self.small(200, {'state':'stop_requested','pid':os.getpid()})
            if route in {'/api/office/conversation','/api/agents/inbox'}:
                from agent_inbox import InboxError
                agent_id=self.headers.get('X-Agent-Id','')
                if route=='/api/office/conversation':
                    authorized=hmac.compare_digest(self.headers.get('X-Office-Key',''),bridge.config['topic'])
                else:authorized=bridge.inbox.authenticated(agent_id,self.headers.get('Authorization','').removeprefix('Bearer '))
                if not authorized:
                    self.close_connection=True
                    return self.small(403,{'error':'invalid_agent_credential' if route=='/api/agents/inbox' else 'private_office_required'})
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if self.headers.get('Transfer-Encoding') or self.headers.get_content_type()!='application/json' or not 0<length<=150000:raise ValueError()
                    data=json.loads(self.rfile.read(length))
                    if not isinstance(data,dict):raise ValueError()
                    if route=='/api/office/conversation':
                        if set(data)!={'agent_id'} or not isinstance(data['agent_id'],str):raise ValueError()
                        return self.small(200,bridge.inbox.ensure(data['agent_id']))
                    def connected():
                        import select, socket
                        try:return not select.select([self.connection],[],[],0)[0] or bool(self.connection.recv(1,socket.MSG_PEEK))
                        except OSError:return False
                    token=self.headers.get('Authorization','').removeprefix('Bearer ')
                    result=bridge.inbox.receive(agent_id,data,connected,authenticated_token=token) if data.get('action')=='receive' else bridge.inbox.event(agent_id,data,authenticated_token=token)
                    if result.get('state')=='receiver_disconnected':
                        self.close_connection=True
                        return
                    return self.small(200,result)
                except (InboxError,ReportingError) as error:return self.small(error.status,{'error':error.code})
                except (ValueError,KeyError,TypeError):return self.small(400,{'error':'invalid_inbox_request'})
                except (OSError,sqlite3.Error):return self.small(503,{'error':'inbox_unavailable'})
            if route == '/api/office/connections/check':
                if not hmac.compare_digest(self.headers.get('X-Office-Key',''),bridge.config['topic']):
                    self.close_connection=True
                    return self.small(403,{'error':'private_office_required'})
                from connection_checks import ConnectionCheckError
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if self.headers.get('Transfer-Encoding') or self.headers.get_content_type()!='application/json' or not 0<length<=4096:raise ValueError()
                    data=json.loads(self.rfile.read(length))
                    check=bridge.get_connection_checks().start(data)
                    capability=bridge.commands.capability(data['agent_id'])
                    return self.small(202,{'check':check,'capability':capability})
                except ConnectionCheckError as error:return self.small(error.status,{'error':error.code})
                except CommandError as error:return self.small(error.status,{'error':error.code})
                except (ValueError,KeyError,TypeError):return self.small(400,{'error':'invalid_connection_check'})
                except (OSError,sqlite3.Error):return self.small(503,{'error':'connection_check_unavailable'})
            if route in {'/api/office/connections/cursor','/api/office/connections/grok'}:
                if not hmac.compare_digest(self.headers.get('X-Office-Key',''),bridge.config['topic']):
                    self.close_connection=True
                    return self.small(403,{'error':'private_office_required'})
                from connections import ConnectionError
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if self.headers.get('Transfer-Encoding') or self.headers.get_content_type()!='application/json' or not 0<length<=16000:raise ValueError()
                    data=json.loads(self.rfile.read(length))
                    result=bridge.connections.connect_cursor(data) if route.endswith('/cursor') else bridge.connections.connect_grok(data)
                    return self.small(200,result)
                except ConnectionError as error:return self.small(error.status,{'error':error.code})
                except (ValueError,KeyError,TypeError):return self.small(400,{'error':'invalid_connection'})
                except (OSError,sqlite3.Error):return self.small(503,{'error':'connection_unavailable'})
            if route in {'/api/office/commands','/api/office/permissions','/api/office/commands/cancel','/api/office/commands/reconcile'}:
                if not hmac.compare_digest(self.headers.get('X-Office-Key',''),bridge.config['topic']):
                    self.close_connection=True
                    return self.small(403,{'error':'private_office_required'})
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if self.headers.get('Transfer-Encoding') or not 0<length<=150000 or self.headers.get_content_type()!='application/json':raise ValueError()
                    data=json.loads(self.rfile.read(length))
                    if not isinstance(data,dict) or not isinstance(data.get('agent_id'),str):raise ValueError()
                    if data['agent_id'] not in bridge.agents:return self.small(404,{'error':'agent_not_selected'})
                    if route=='/api/office/permissions':return self.small(200,bridge.commands.decide(data))
                    if route=='/api/office/commands/cancel':return self.small(200,bridge.commands.cancel_pending(data))
                    if route=='/api/office/commands/reconcile':
                        with bridge.commands.lock,bridge.history.lock:
                            agent=bridge.definitions.get(data['agent_id'])
                            if not agent:return self.small(404,{'error':'agent_not_selected'})
                            result=bridge.commands.reconcile_grok_terminal(agent,data)
                        return self.small(200,result)
                    return self.small(202,bridge.commands.create(data))
                except CommandError as error:return self.small(error.status,{'error':error.code})
                except (ValueError,TypeError,KeyError):
                    self.close_connection=True
                    return self.small(400,{'error':'invalid_command'})
                except (OSError,sqlite3.Error):return self.small(503,{'error':'commands_unavailable'})
            if route in {'/api/office/invitations','/api/office/invitations/revoke','/api/office/workspaces','/api/office/workspaces/assign','/api/agents/join'}:
                if route.startswith('/api/office/') and not hmac.compare_digest(self.headers.get('X-Office-Key',''),bridge.config['topic']):
                    self.close_connection=True
                    return self.small(403,{'error':'private_office_required'})
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if self.headers.get('Transfer-Encoding') or not 0<length<=4096:raise ValueError('Invalid body')
                    data=json.loads(self.rfile.read(length))
                    if route=='/api/office/invitations':result=bridge.invitations.create(data)
                    elif route=='/api/office/workspaces':
                        if not isinstance(data,dict) or 'path' not in data or set(data)-{'path','name'}:raise ValueError('Invalid workspace')
                        result=bridge.workspaces.create(data['path'],data.get('name'))
                    elif route=='/api/office/workspaces/assign':
                        if not isinstance(data,dict) or not {'agent_id','mode'}<=set(data) or set(data)-{'agent_id','mode','workspace_id'}:raise ValueError('Invalid assignment')
                        if data['agent_id'] not in bridge.agents:return self.small(404,{'error':'agent_not_selected'})
                        result=bridge.workspaces.assign(data['agent_id'],data.get('workspace_id'),data['mode'])
                    elif route=='/api/office/invitations/revoke':
                        if not isinstance(data,dict) or set(data)!={'id'}:raise ValueError('Invalid revoke')
                        result=bridge.invitations.revoke(data['id'])
                    else:
                        result=bridge.invitations.claim(self.headers.get('X-Invitation-Id',''),
                            self.headers.get('Authorization','').removeprefix('Bearer '),data)
                    return self.small(200,result)
                except (InvitationError,ReportingError) as error:return self.small(error.status,{'error':error.code})
                except (ValueError,TypeError,KeyError):
                    self.close_connection=True
                    return self.small(400,{'error':'invalid_request'})
                except (OSError,sqlite3.Error):return self.small(503,{'error':'invitation_unavailable'})
            if route != '/api/agents/events':
                self.close_connection = True
                return self.small(404, {'error':'not_found'})
            agent_id = self.headers.get('X-Agent-Id','')
            agent = bridge.definitions.get(agent_id)
            token = self.headers.get('Authorization','').removeprefix('Bearer ')
            digest = hashlib.sha256(token.encode()).hexdigest()
            if not agent or agent['driver']!='push' or not hmac.compare_digest(digest,agent['token_sha256']):
                self.close_connection = True
                return self.small(403, {'error':'invalid_agent_credential'})
            try:
                length = int(self.headers.get('Content-Length','0'))
                if self.headers.get('Transfer-Encoding') or not 0 < length <= 100000:
                    raise ValueError('Invalid body size')
                event = json.loads(self.rfile.read(length))
                receipt = bridge.accept_agent_event(agent, event)
                return self.small(200, receipt)
            except EventConflict as error:
                return self.small(409, {'error':str(error)})
            except (ValueError, TypeError, KeyError):
                self.close_connection = True
                return self.small(400, {'error':'invalid_event'})

        def do_GET(self):
            if not self.allowed():
                return self.small(403, {'error': 'forbidden'})
            u = urllib.parse.urlsplit(self.path)
            if u.path.startswith('/api/office/'):
                local_origins = {None, 'http://127.0.0.1:%s' % bridge.config['port'], 'http://localhost:%s' % bridge.config['port']}
                key = self.headers.get('X-Office-Key', '')
                if self.headers.get('Origin') not in local_origins or not hmac.compare_digest(key, bridge.config['topic']):
                    return self.small(403, {'error': 'private_office_required'})
                if u.path == '/api/office/connections':
                    return self.small(200,bridge.connections.status())
                if u.path == '/api/office/connections/check':
                    from connection_checks import ConnectionCheckError
                    try:
                        params=urllib.parse.parse_qs(u.query)
                        if set(params)!={'agent'} or len(params['agent'])!=1:raise ValueError()
                        agent=params['agent'][0]
                        if agent not in bridge.definitions:return self.small(404,{'error':'agent_not_selected'})
                        check=bridge.get_connection_checks().status(agent)
                        capability=bridge.commands.capability(agent)
                        return self.small(200,{'check':capability.get('connection_check',check),'capability':capability})
                    except ConnectionCheckError as error:return self.small(error.status,{'error':error.code})
                    except (ValueError,KeyError,TypeError):return self.small(400,{'error':'invalid_connection_check'})
                    except (OSError,sqlite3.Error):return self.small(503,{'error':'connection_check_unavailable'})
                if u.path == '/api/office/seating':
                    return self.small(200,bridge.members.snapshot())
                q = urllib.parse.parse_qs(u.query)
                if u.path == '/api/office/work':
                    return self.small(200, bridge.work_snapshot())
                if u.path == '/api/office/commands':
                    agent=q.get('agent',[''])[0]
                    if agent not in bridge.agents:return self.small(404,{'error':'agent_not_selected'})
                    try:return self.small(200,bridge.commands.list(agent))
                    except (OSError,sqlite3.Error):return self.small(503,{'error':'commands_unavailable'})
                if u.path == '/api/office/workspaces':
                    agent=q.get('agent',[None])[0]
                    if agent and agent not in bridge.agents:return self.small(404,{'error':'agent_not_selected'})
                    return self.small(200,{'workspaces':bridge.workspaces.catalog(),
                        'current':bridge.workspaces.for_agent(agent) if agent else None})
                if u.path == '/api/office/invitations':
                    try:return self.small(200,bridge.invitations.get(q.get('id',[''])[0]))
                    except (InvitationError,ReportingError) as error:return self.small(error.status,{'error':error.code})
                if u.path == '/api/office/messages':
                    agent = q.get('agent', [''])[0]
                    try:
                        after = max(0, int(q.get('after', ['0'])[0]))
                        before = int(q['before'][0]) if 'before' in q else None
                        with bridge.commands.lock,bridge.history.lock:
                            allowed=bridge.history_agents()
                            if agent not in allowed:return self.small(404, {'error': 'agent_not_selected'})
                            rows = bridge.history.messages(allowed, agent, run=q.get('run', [None])[0], after=after, before=before)
                            descriptor=allowed[agent]
                    except ValueError:
                        return self.small(400, {'error': 'invalid_cursor'})
                    return self.small(200, {'agent': descriptor, 'messages': rows,
                                           'cursor': rows[-1]['seq'] if rows else after,
                                           'before': rows[0]['seq'] if len(rows) == 100 else None})
                if u.path == '/api/office/history':
                    agent = q.get('agent', [''])[0]
                    with bridge.commands.lock,bridge.history.lock:
                        allowed=bridge.history_agents()
                        if agent not in allowed:return self.small(404, {'error': 'agent_not_selected'})
                        rows = bridge.history.list(allowed, agent=agent, before=q.get('before', [None])[0])
                        if hasattr(bridge,'children'):rows=bridge.children.decorate_runs(rows)
                        cursor = rows[-1]['started_at'] + '|' + rows[-1]['id'] if len(rows) == 60 else None
                        return self.small(200, {'agent': allowed[agent], 'runs': rows, 'next': cursor})
                if u.path == '/api/office/run':
                    with bridge.commands.lock,bridge.history.lock:
                        item = bridge.history.detail(q.get('id', [''])[0], bridge.history_agents())
                        if item and hasattr(bridge,'children'):item=bridge.children.decorate_runs([item])[0]
                        return self.small(200, item) if item else self.small(404, {'error': 'run_not_found'})
                return self.small(404, {'error': 'not_found'})
            if u.path.startswith(('/office/', '/assets/')):
                public = (ROOT / 'public').resolve()
                target = (public / urllib.parse.unquote(u.path).lstrip('/')).resolve()
                if target.is_dir():
                    target = target / 'index.html'
                if not str(target).startswith(str(public) + '/') or not target.is_file():
                    return self.small(404, {'error': 'not_found'})
                body = target.read_bytes()
                self.send_response(200)
                self.headers_common()
                self.send_header('Content-Type', mimetypes.guess_type(str(target))[0] or 'application/octet-stream')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob:; connect-src 'self' https://cdn.jsdelivr.net; media-src 'self' blob:; worker-src blob:")
                self.end_headers()
                self.wfile.write(body)
                return
            if u.path == '/health':
                return self.small(200, {'ok': not bridge.stop.is_set(),
                                       'local_ok': bridge.local_ready(),
                                       'sync_ok': not (bridge.health.get('last_publish_error') or bridge.health.get('source_lookup_error')
                                                       or time.time() < bridge.limit_until or bridge.outbound.unfinished_tasks),
                                       'source_count': len(bridge.sources), 'published': bridge.counts['published'], 'pid':os.getpid()})
            prefix = '/' + bridge.config['topic']
            if u.path not in {prefix + '/json', prefix + '/sse'}:
                return self.small(404, {'error': 'not_found'})
            q = urllib.parse.parse_qs(u.query)
            if any(k not in {'poll', 'since'} for k in q):
                return self.small(400, {'error': 'invalid_query'})
            since = q.get('since', ['12h'])[0]
            if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', since):
                return self.small(400, {'error': 'invalid_since'})
            stream = u.path.endswith('/sse')
            if not bridge.allowed_names or not bridge.config.get('feed'):
                self.send_response(200)
                self.headers_common()
                self.send_header('Content-Type', 'text/event-stream' if stream else 'application/x-ndjson')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True
                if stream:
                    try:
                        while not bridge.stop.is_set():
                            self.wfile.write(b': empty office; enrollment is explicit\n\n')
                            self.wfile.flush()
                            bridge.stop.wait(15)
                    except (OSError, BrokenPipeError):
                        pass
                return
            upstream = bridge.config['feed'] + ('/sse' if stream else '/json?poll=1&since=' + since)
            try:
                response = urllib.request.urlopen(upstream, timeout=65 if stream else 20)
            except Exception:
                return self.small(502, {'error': 'feed_unavailable'})
            with response:
                self.send_response(200)
                self.headers_common()
                self.send_header('Content-Type', 'text/event-stream' if stream else 'application/x-ndjson')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True
                try:
                    if stream:
                        while not bridge.stop.is_set():
                            line = response.readline(128 * 1024)
                            if not line:
                                break
                            if line.startswith(b'data: '):
                                try:
                                    item = bridge.filter_record(json.loads(line[6:]))
                                except (ValueError, TypeError):
                                    item = None
                                if item:
                                    self.wfile.write(b'data: ' + json.dumps(item, ensure_ascii=False).encode() + b'\n\n')
                                    self.wfile.flush()
                    else:
                        for line in response.read(8 * 1024 * 1024).splitlines():
                            try:
                                item = bridge.filter_record(json.loads(line))
                            except (ValueError, TypeError):
                                item = None
                            if item:
                                self.wfile.write(json.dumps(item, ensure_ascii=False).encode() + b'\n')
                except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                    pass
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default=str(DEFAULT_CONFIG))
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--duration', type=int)
    args = parser.parse_args()
    config_path = Path(args.config).expanduser().resolve()
    if config_path.stat().st_mode & 0o077:
        raise SystemExit('Private configuration must have mode 0600')
    config = load_config(config_path)
    if not config.get('run_dir'):
        config['run_id'] = 'office-' + uuid.uuid4().hex[:12]
        config['run_dir'] = str(Path(config['data_dir']) / 'runs' / config['run_id'])
    Path(config['run_dir']).mkdir(parents=True, exist_ok=True, mode=0o700)
    os.umask(0o077)
    bridge = Bridge(config, args.dry_run)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', config['port']), proxy_handler(bridge))
    server.daemon_threads = True
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: bridge.stop.set())
    atomic_json(bridge.run_dir / 'pid.json', {'pid': os.getpid(), 'stop_target': '/bin/kill -TERM %s' % os.getpid(), 'script': str(Path(__file__).resolve())})
    threads = [threading.Thread(target=f, daemon=True) for f in (bridge.publish_loop, bridge.observe_loop, lambda:bridge.commands.run(bridge.stop), server.serve_forever)]
    for thread in threads:
        thread.start()
    print(json.dumps({'run_id': config['run_id'], 'pid': os.getpid(), 'state': 'running', 'dry_run': args.dry_run}), flush=True)
    started = time.monotonic()
    while not bridge.stop.wait(1):
        if not threads[1].is_alive() or (args.duration and time.monotonic() - started >= args.duration):
            bridge.stop.set()
    server.shutdown()
    for thread in threads[:3]:
        thread.join(timeout=20)
    server.server_close()
    bridge.commands.close()
    bridge.status()
    bridge.history.close()


if __name__ == '__main__':
    main()
