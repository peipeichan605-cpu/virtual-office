"""Owner-facing portable setup. Secrets stay in this installation's private files."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import uuid
from configuration import agent_name, codex_targets, load_config, save_config
from hermes_acp import private_json
from platform_adapters import HTTPS, RemoteError, cursor_agent_id, cursor_headers, secret_file, validate_key, webhook_url
from history import timestamp


class ConnectionError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


class Connections:
    def __init__(self, bridge, transport=None):
        self.bridge, self.transport = bridge, transport or HTTPS()
        self.root = Path(bridge.config['data_dir']) / 'platform-secrets'

    def status(self):
        cursor_sessions=self.bridge.config.get('command_adapters',{}).get('cursor_cloud',{}).get('sessions',{})
        saved=(self.root / 'cursor.json').is_file() or any(Path(value['credential_file']).is_file() for value in cursor_sessions.values())
        return {'cursor_key_saved':saved,
                'cursor_desktop_cloud':True, 'cursor_desktop_local_commands':False,
                'grok_webhook':True, 'grok_reply_requires_local_execution':True}

    def save(self, config):
        path = Path(config['config_path'])
        check = path.with_name('platform-check-' + uuid.uuid4().hex + '.json')
        try:
            save_config(check, config); load_config(check)
        finally:
            check.unlink(missing_ok=True)
        save_config(path, config)
        self.bridge.refresh_connections(config)

    def connect_agent(self, agent_id, data):
        """An enrolled agent may attach only its own supported native route."""
        if not isinstance(data,dict):raise ConnectionError('invalid_connection')
        if data.get('adapter')=='codex_app' and set(data)=={'adapter','session_id','workspace_path'}:
            result=self.connect_codex(agent_id,data['session_id'],data['workspace_path'])
        elif data.get('adapter')=='grok_webhook' and set(data)=={'adapter','url','key'}:
            with self.bridge.commands.lock,self.bridge.history.lock:
                existing=self.bridge.commands.bindings.get(agent_id)
                if existing:
                    if existing['adapter']!='grok_webhook':raise ConnectionError('native_binding_conflict',409)
                    settings=self.bridge.config.get('command_adapters',{}).get('grok_webhook',{}).get('sessions',{}).get(existing['session_id'],{})
                    previous=secret_file(settings['credential_file'])
                    if not isinstance(data['url'],str) or not isinstance(data['key'],str) or not hmac.compare_digest(previous['url'],data['url']) or not hmac.compare_digest(previous['key'],data['key']):
                        raise ConnectionError('native_binding_conflict',409)
                result=self.connect_grok({'agent_id':agent_id,'url':data['url'],'key':data['key']})
        else:raise ConnectionError('native_setup_required',409)
        result['conversation']=self.bridge.inbox.ensure(agent_id)
        return result

    def connect_codex(self, agent_id, session_id, workspace):
        try:
            if str(uuid.UUID(session_id))!=session_id or not isinstance(workspace,str) or not Path(workspace).is_absolute():raise ValueError()
        except (ValueError,TypeError,AttributeError):raise ConnectionError('invalid_native_session')
        adapter=self.bridge.commands.adapters.get('codex_app')
        if not adapter or not adapter.available():raise ConnectionError('codex_queue_unavailable',409)
        with self.bridge.commands.lock,self.bridge.history.lock:
            config=load_config(self.bridge.config['config_path'])
            agent=next((a for a in config['agents'] if a['id']==agent_id and a['provider']=='codex'),None)
            if not agent:raise ConnectionError('agent_not_selected',404)
            if agent_id in config.get('disabled_command_agents',[]):raise ConnectionError('dispatch_disabled',409)
            binding={'adapter':'codex_app','session_id':session_id}
            previous=config.get('command_bindings',{}).get(agent_id)
            if previous and previous!=binding:raise ConnectionError('native_binding_conflict',409)
            other=codex_targets(config).get(session_id)
            if other and other['id']!=agent_id:raise ConnectionError('native_session_already_selected',409)
            expected=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))/'state_5.sqlite'
            if Path(config['codex_db']).resolve()!=expected.resolve():raise ConnectionError('codex_home_mismatch',409)
            try:
                with sqlite3.connect(Path(config['codex_db']).resolve().as_uri()+'?mode=ro',uri=True,timeout=2) as db:
                    row=db.execute('SELECT rollout_path,cwd,archived FROM threads WHERE id=?',(session_id,)).fetchone()
                if not row or row[2] or not row[1]:raise ValueError()
                native_cwd=Path(row[1]).resolve();actual=Path(workspace).resolve()
                if actual!=native_cwd and native_cwd not in actual.parents:raise ValueError()
                # Verify the database points to this exact original session.
                path=Path(row[0])
                if path.is_symlink():raise ValueError()
                with path.open('rb') as source:metadata=json.loads(source.readline(1024*1024))
                if metadata.get('type')!='session_meta' or metadata.get('payload',{}).get('id')!=session_id:raise ValueError()
            except (OSError,sqlite3.Error,ValueError,TypeError):raise ConnectionError('native_session_not_verified',409)
            if not previous:
                active=self.bridge.commands.db.execute("SELECT 1 FROM commands WHERE agent_id=? AND (state IN ('sending','queued','running','waiting_user','delivery_unknown') OR (state='pending' AND (native_message_id IS NOT NULL OR run_id IS NOT NULL))) LIMIT 1",(agent_id,)).fetchone()
                if active:raise ConnectionError('connection_has_active_commands',409)
            config.setdefault('command_bindings',{})[agent_id]=binding
            config.setdefault('command_adapters',{}).setdefault('codex_app',{})
            if agent['driver']=='push':agent.setdefault('native_observe_after',timestamp())
            self.save(config)
        return {'connected':True,'agent_id':agent_id,'mode':'background_queue','same_session':True,
                'persistent':True,'native_target_verified':True,'execution_verified':False}

    def connect_cursor(self, data):
        if not isinstance(data, dict) or set(data)-{'name','session_id','api_key'} or not {'name','session_id'} <= set(data):
            raise ConnectionError('invalid_connection')
        try:
            name, sid = agent_name(data['name']), cursor_agent_id(data['session_id'])
        except (ValueError, TypeError):
            raise ConnectionError('cursor_connection_details_required')
        ident = 'cursor:' + sid
        with self.bridge.commands.lock, self.bridge.history.lock:
            config=load_config(self.bridge.config['config_path'])
            self._cursor_target(config,ident,name)
            membership=self._cursor_membership(config,ident,sid)
            settings=config.get('command_adapters',{}).get('cursor_cloud',{}).get('sessions',{}).get(sid,{})
            try:
                # Read an existing target's key first. The shared legacy file
                # is a fallback for old installations, never a write target.
                key=data.get('api_key') or secret_file(settings.get('credential_file') or str(self.root/'cursor.json')).get('api_key')
                validate_key(key)
            except (ValueError,OSError,KeyError,TypeError):
                raise ConnectionError('cursor_connection_details_required')
            self._cursor_change_allowed(config,ident,sid,key)
        try:
            native = self.transport.json('GET', 'https://api.cursor.com/v1/agents/' + sid, cursor_headers(key))
        except RemoteError as error:
            raise ConnectionError(error.code, 409)
        if native.get('id') != sid or native.get('status') not in {'ACTIVE','IDLE'}:
            raise ConnectionError('cursor_agent_not_available', 409)
        with self.bridge.commands.lock, self.bridge.history.lock:
            config = load_config(self.bridge.config['config_path'])
            if membership!=self._cursor_membership(config,ident,sid):
                raise ConnectionError('agent_membership_changed',409)
            agent=self._cursor_target(config,ident,name)
            unchanged=self._cursor_change_allowed(config,ident,sid,key)
            if unchanged:
                return {'connected':True,'agent_id':ident,'mode':'cursor_cloud','same_session':True,
                        'native_target_verified':True,'execution_verified':False}
            if not agent:
                config['agents'].append({'id':ident,'source_id':sid,'name':name,'provider':'cursor','driver':'cursor'})
            # New credentials get a target-scoped file. A failed config save
            # cannot rotate a currently selected Agent's working credential,
            # and another legacy binding can keep using its old shared file.
            credential=self.root/('cursor-'+hashlib.sha256(sid.encode()).hexdigest()[:24]+'-'+uuid.uuid4().hex[:12]+'.json')
            config.setdefault('command_bindings',{})[ident]={'adapter':'cursor_cloud','session_id':sid}
            config.setdefault('command_adapters',{}).setdefault('cursor_cloud',{}).setdefault('sessions',{})[sid]={'credential_file':str(credential)}
            private_json(credential, {'api_key':key})
            self.bridge.history.secrets = (*self.bridge.history.secrets, key)
            self.save(config)
        return {'connected':True,'agent_id':ident,'mode':'cursor_cloud','same_session':True,
                'native_target_verified':True,'execution_verified':False}

    def _cursor_target(self, config, ident, name):
        if ident in config.get('disabled_command_agents',[]):raise ConnectionError('dispatch_disabled',409)
        agent=next((a for a in config['agents'] if a['id']==ident),None)
        if agent and (agent['name']!=name or agent['driver']!='cursor'):
            raise ConnectionError('agent_already_selected',409)
        if any(a['name']==name and a['id']!=ident for a in config['agents']):
            raise ConnectionError('name_in_use',409)
        return agent

    def _cursor_membership(self, config, ident, sid):
        # Metadata lookup runs without lifecycle locks. Scope its eventual
        # result to this target's original membership and route, including an
        # ABA logout/rejoin whose visible Agent descriptor looks identical.
        event=self.bridge.history.db.execute('SELECT event_id FROM office_membership_events '
            'WHERE agent_id=? ORDER BY rowid DESC LIMIT 1',(ident,)).fetchone()
        value={'agent':next((a for a in config['agents'] if a['id']==ident),None),
            'binding':config.get('command_bindings',{}).get(ident),
            'settings':config.get('command_adapters',{}).get('cursor_cloud',{}).get('sessions',{}).get(sid),
            'retired':ident in config.get('logged_out_agent_ids',[]),
            'disabled':ident in config.get('disabled_command_agents',[]),
            'membership_event':event['event_id'] if event else None}
        return json.dumps(value,sort_keys=True,separators=(',',':'))

    def _cursor_change_allowed(self, config, ident, sid, key):
        unchanged=False
        binding=config.get('command_bindings',{}).get(ident)
        if binding=={'adapter':'cursor_cloud','session_id':sid}:
            try:
                settings=config['command_adapters']['cursor_cloud']['sessions'][sid]
                previous=secret_file(settings['credential_file']).get('api_key')
                unchanged=isinstance(previous,str) and hmac.compare_digest(previous,key)
            except (OSError,ValueError,KeyError,TypeError):pass
        if not unchanged:
            active=self.bridge.commands.db.execute("SELECT 1 FROM commands WHERE agent_id=? AND (state IN ('sending','queued','running','waiting_user','delivery_unknown') OR (state='pending' AND (native_message_id IS NOT NULL OR run_id IS NOT NULL))) LIMIT 1",(ident,)).fetchone()
            if active:raise ConnectionError('connection_has_active_commands',409)
        return unchanged

    def connect_grok(self, data):
        if not isinstance(data,dict) or set(data)!={'agent_id','url','key'}:
            raise ConnectionError('invalid_connection')
        try:
            webhook_url(data['url']);validate_key(data['key'])
        except (ValueError, TypeError):raise ConnectionError('invalid_webhook_details')
        with self.bridge.commands.lock, self.bridge.history.lock:
            config = load_config(self.bridge.config['config_path'])
            agent = next((a for a in config['agents'] if a['id']==data['agent_id'] and a['provider']=='grok_bot' and a['driver']=='push'), None)
            if not agent:raise ConnectionError('agent_not_selected',404)
            if agent['id'] in config.get('disabled_command_agents',[]):raise ConnectionError('dispatch_disabled',409)
            # The caller must first prove the return route via the scoped agent
            # credential. A saved webhook alone never proves two-way delivery.
            seen = self.bridge.history.db.execute('SELECT 1 FROM event_receipts WHERE agent_id=? LIMIT 1',(agent['id'],)).fetchone()
            if not seen:raise ConnectionError('grok_return_route_required',409)
            active=self.bridge.commands.db.execute("SELECT 1 FROM commands WHERE agent_id=? AND (state IN ('sending','queued','running','waiting_user','delivery_unknown') OR (state='pending' AND (native_message_id IS NOT NULL OR run_id IS NOT NULL)))",(agent['id'],)).fetchone()
            if active or self.bridge.commands.reported_work_in_progress(agent['id']):
                raise ConnectionError('connection_has_active_commands',409)
            credential = self.root / ('grok-' + hashlib.sha256(agent['id'].encode()).hexdigest()[:24] + '.json')
            previous_binding=config.get('command_bindings',{}).get(agent['id'])
            unchanged=False
            if previous_binding=={'adapter':'grok_webhook','session_id':agent['source_id']}:
                try:
                    old_settings=config['command_adapters']['grok_webhook']['sessions'][agent['source_id']]
                    previous=secret_file(old_settings['credential_file'])
                    unchanged=(hmac.compare_digest(previous.get('url',''),data['url']) and
                               hmac.compare_digest(previous.get('key',''),data['key']))
                except (KeyError,OSError,ValueError,TypeError):pass
            if not unchanged:
                # Invalidate old proof before any credential/config write. A
                # crash can require another test, never preserve stale success.
                self.bridge.get_connection_checks().route_changed(agent['id'])
            private_json(credential, {'url':data['url'],'key':data['key']})
            config.setdefault('command_bindings',{})[agent['id']]={'adapter':'grok_webhook','session_id':agent['source_id']}
            config.setdefault('command_adapters',{}).setdefault('grok_webhook',{}).setdefault('sessions',{})[agent['source_id']]={'credential_file':str(credential)}
            self.bridge.history.secrets = (*self.bridge.history.secrets, data['key'], data['url'])
            self.save(config)
        return {'connected':True,'agent_id':agent['id'],'mode':'grok_webhook','same_session':False,
                'return_route_verified':True,'execution_verified':False}
