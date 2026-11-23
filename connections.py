"""Owner-facing portable setup. Secrets stay in this installation's private files."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import threading
import uuid
from configuration import agent_name, codex_targets, load_config, save_config
from hermes_acp import private_json
from platform_adapters import HTTPS, RemoteError, cursor_agent_id, cursor_headers, secret_file, validate_key, webhook_url
from history import open_native_rollout, timestamp
from private_files import ensure_private_directory, require_private
from workspaces import workspace_path
from security_keys import integrity_fingerprint, matches_integrity
from cursor_local import DEFAULT_MODEL, MODEL, LocalStartError, SdkRuntime, durable_state, lifecycle_path, local_agent_id, tool_restrictions


class ConnectionError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


class Connections:
    def __init__(self, bridge, transport=None, local_runtime=None):
        self.bridge, self.transport = bridge, transport or HTTPS()
        self.root = Path(bridge.config['data_dir']) / 'platform-secrets'
        self.local_runtime = local_runtime or SdkRuntime()
        self._creation_lock, self._creating = threading.RLock(), set()
        with bridge.history.lock, bridge.history.db:
            bridge.history.db.execute('''CREATE TABLE IF NOT EXISTS cursor_sdk_creations (
                request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                state TEXT NOT NULL, response TEXT, reason TEXT, native_id TEXT)''')

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
                native_cwd=Path(workspace_path(row[1]));actual=Path(workspace_path(workspace))
                if actual!=native_cwd and native_cwd not in actual.parents:raise ValueError()
                # Verify the database points to this exact original session.
                with open_native_rollout(row[0]) as source:
                    metadata=json.loads(source.readline(1024*1024))
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

    def _saved_cursor_sdk(self):
        # Cursor Cloud's legacy cursor.json is intentionally read-only. The
        # SDK account settings have their own installation-private file.
        path = self.root / 'cursor-sdk.json'
        if not path.is_file():
            return {}
        try:
            value = secret_file(str(path))
            return value if isinstance(value, dict) else {}
        except (ValueError, OSError, KeyError, TypeError):
            return {}

    @staticmethod
    def _creation_request_id(value):
        try:
            if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ConnectionError('invalid_request_id')
        return value

    def cursor_local_status(self, request_id=None):
        saved = self._saved_cursor_sdk()
        try:
            validate_key(saved.get('api_key'))
            has_key = True
        except ValueError:
            has_key = False
        workspace = saved.get('workspace')
        if not isinstance(workspace, str) or not Path(workspace).is_dir():
            workspace = None
        available = self.local_runtime.available()
        catalog = self.bridge.workspaces.catalog() if hasattr(self.bridge, 'workspaces') else []
        result = {'sdk_available': available,
            'sdk_reason': None if available else getattr(self.local_runtime, 'reason', None) or 'cursor_sdk_required',
            'sdk_version': getattr(self.local_runtime, 'version', None),
            'credential_configured': has_key, 'workspace': workspace,
            'workspaces': [{'name': row['name'], 'path': row['path']} for row in catalog],
            'mode': 'cursor_local', 'scope': 'office_owned_session', 'tool_policy': 'chat_only'}
        if request_id is not None:
            request_id = self._creation_request_id(request_id)
            with self.bridge.history.lock:
                row = self.bridge.history.db.execute('SELECT * FROM cursor_sdk_creations WHERE request_id=?', (request_id,)).fetchone()
            if row:
                with self._creation_lock:
                    active = request_id in self._creating
                state = row['state']
                if state == 'creating':
                    state = 'pending' if active else 'unknown'
                creation = {'request_id': request_id, 'state': state}
                if row['response']:
                    response = json.loads(row['response'])
                    creation.update({key: response[key] for key in ('agent_id', 'name') if key in response})
                    if state == 'completed' and not self._sdk_creation_selected(response):
                        creation.update(state='revoked', reason='agent_membership_changed')
                if row['reason']:
                    creation['reason'] = row['reason']
                result['creation'] = creation
            else:
                result['creation'] = {'request_id':request_id,'state':'not_found'}
        return result

    def _sdk_creation_selected(self, response):
        ident = response.get('agent_id')
        definition = self.bridge.definitions.get(ident)
        return bool(definition and definition.get('driver')=='cursor' and
            self.bridge.commands.bindings.get(ident)=={'adapter':'cursor_local','session_id':definition['source_id']})

    def _new_cursor_workspace(self, request_id):
        # Use a fresh leaf as the SDK cwd. Credentials, history and other chats
        # live outside it. Never adopt an existing folder for a new chat.
        root = self.root.parent / 'cursor-chat-workspaces'
        folder = root / request_id
        try:
            ensure_private_directory(root)
            folder.mkdir(mode=0o700)
            require_private(folder, directory=True)
        except (OSError, ValueError) as error:
            raise LocalStartError('cursor_auto_workspace_unavailable', native_attempted=False) from error
        return folder

    def create_cursor_local(self, data):
        """Create one owner-selected SDK chat; never import other native agents."""
        allowed = {'name', 'workspace', 'api_key', 'model', 'request_id', 'tools', 'disallowed_tools', 'setting_sources', 'create_folder'}
        if not isinstance(data, dict) or set(data) - allowed:
            raise ConnectionError('invalid_connection')
        request_id = self._creation_request_id(data.get('request_id') or str(uuid.uuid4()))
        saved = self._saved_cursor_sdk()
        if 'create_folder' in data and not isinstance(data['create_folder'], bool):
            raise ConnectionError('invalid_workspace')
        creating_folder = data.get('create_folder') is True
        automatic = 'workspace' not in data
        cwd = None
        if automatic and creating_folder:
            raise ConnectionError('invalid_workspace')
        if not automatic:
            try:
                cwd = workspace_path(data['workspace'])
                folder = Path(cwd)
                if not folder.is_dir() and not (creating_folder and not folder.exists() and folder.parent.is_dir()):
                    raise ValueError()
            except (OSError, ValueError, TypeError):
                raise ConnectionError('invalid_workspace')
        try:
            key = validate_key(data.get('api_key') or saved.get('api_key'))
        except (ValueError, OSError, KeyError, TypeError):
            raise ConnectionError('cursor_connection_details_required')
        model = data.get('model') or DEFAULT_MODEL
        if not isinstance(model, str) or not MODEL.fullmatch(model):
            raise ConnectionError('cursor_model_invalid')
        try:
            policy = tool_restrictions(data)
            requested_name = agent_name(data['name']) if 'name' in data else None
        except (ValueError, TypeError):
            raise ConnectionError('invalid_connection')
        if not self.local_runtime.available():
            raise ConnectionError(getattr(self.local_runtime, 'reason', None) or 'cursor_sdk_required', 409)
        fingerprint_payload = json.dumps({
            'name': requested_name, 'cwd': cwd, 'model': model, 'policy': policy,
            'create_folder': creating_folder,
            'key_sha256': hashlib.sha256(key.encode()).hexdigest()}, sort_keys=True).encode()
        fingerprint = integrity_fingerprint(self.bridge.config, fingerprint_payload)
        with self.bridge.commands.lock, self.bridge.history.lock, self.bridge.history.db:
            row = self.bridge.history.db.execute('SELECT * FROM cursor_sdk_creations WHERE request_id=?', (request_id,)).fetchone()
            if row:
                if not matches_integrity(self.bridge.config, fingerprint_payload, row['fingerprint']):
                    raise ConnectionError('request_id_conflict', 409)
                if row['state'] == 'completed' and row['response']:
                    response = json.loads(row['response'])
                    if not self._sdk_creation_selected(response):
                        raise ConnectionError('agent_membership_changed',409)
                    return response
                with self._creation_lock:
                    active = request_id in self._creating
                raise ConnectionError(row['reason'] or ('cursor_creation_pending' if active else 'cursor_creation_unknown'), 409)
            config = load_config(self.bridge.config['config_path'])
            taken = {agent['name'] for agent in config['agents']}
            name = requested_name or 'Cursor SDK 對話'
            if requested_name and name in taken:
                raise ConnectionError('name_in_use', 409)
            number = 2
            while name in taken:
                name = 'Cursor SDK 對話 ' + str(number); number += 1
            self.bridge.history.db.execute('INSERT INTO cursor_sdk_creations VALUES (?,?,?,?,?,?)',
                (request_id, fingerprint, 'creating', None, None, None))
            with self._creation_lock:
                self._creating.add(request_id)
        native_id = None
        managed_folder = None
        try:
            if automatic:
                managed_folder = self._new_cursor_workspace(request_id)
                cwd = str(managed_folder)
            elif creating_folder:
                folder.mkdir(exist_ok=True)
            # Reserve a stable intended ID locally. SDK1.3.1 native creation
            # must wait for the first explicit prompt on the SAME handle.
            native_id = local_agent_id(self.local_runtime.reserve(key, model, cwd, policy, request_id=request_id, name=name))
            with self.bridge.commands.lock, self.bridge.history.lock:
                self.bridge.history.db.execute('UPDATE cursor_sdk_creations SET native_id=? WHERE request_id=?', (native_id, request_id))
                self.bridge.history.db.commit()
                config = load_config(self.bridge.config['config_path'])
                ident = 'cursor:' + native_id
                if any(agent['name'] == name or agent['id'] == ident for agent in config['agents']):
                    raise ConnectionError('cursor_creation_unknown', 409)
                credential = self.root / ('cursor-sdk-' + hashlib.sha256(native_id.encode()).hexdigest()[:24] + '.json')
                private_json(credential, {'api_key': key})
                self.bridge.history.secrets = (*self.bridge.history.secrets, key)
                config['agents'].append({'id': ident, 'source_id': native_id, 'name': name, 'provider': 'cursor', 'driver': 'cursor'})
                config.setdefault('command_bindings', {})[ident] = {'adapter': 'cursor_local', 'session_id': native_id}
                settings = {'credential_file': str(credential), 'cwd': cwd, 'model': model, **policy}
                durable_state(lifecycle_path(settings), {'schema':1,'agent_id':native_id,'cwd':cwd,
                    'phase':'reserved','native_target_verified':False,'name':name})
                config.setdefault('command_adapters', {}).setdefault('cursor_local', {}).setdefault('sessions', {})[native_id] = settings
                self.save(config)
                # Only a user-selected project is reusable in the folder picker.
                defaults = {'api_key': key}
                manual_workspace = self._saved_cursor_sdk().get('workspace') if automatic else cwd
                if manual_workspace:
                    defaults['workspace'] = manual_workspace
                private_json(self.root / 'cursor-sdk.json', defaults)
                if not automatic and hasattr(self.bridge, 'workspaces'):
                    workspace = self.bridge.workspaces.create(cwd)
                    self.bridge.workspaces.observe(ident, cwd, 'cursor_local')
                    self.bridge.workspaces.assign(ident, workspace['id'], 'manual')
                response = {'connected': True, 'agent_id': ident, 'name': name, 'mode': 'cursor_local',
                    'same_session': True, 'native_target_verified': False, 'execution_verified': False,
                    'registration_state':'pending_first_instruction',
                    'scope': 'office_owned_session', 'tool_policy': 'chat_only', 'request_id': request_id}
                self.bridge.history.db.execute('UPDATE cursor_sdk_creations SET state=?,response=?,reason=NULL WHERE request_id=?',
                    ('completed', json.dumps(response), request_id))
                self.bridge.history.db.commit()
                return response
        except Exception as error:
            confirmed = isinstance(error, LocalStartError) and not error.uncertain and native_id is None
            reason = error.code if isinstance(error, LocalStartError) else 'cursor_creation_unknown'
            if confirmed and managed_folder is not None:
                try:
                    managed_folder.rmdir()
                except OSError:
                    pass  # Never remove workspace contents or mask the original failure.
            with self.bridge.history.lock, self.bridge.history.db:
                self.bridge.history.db.execute('UPDATE cursor_sdk_creations SET state=?,reason=? WHERE request_id=?',
                    ('failed' if confirmed else 'unknown', reason, request_id))
            raise ConnectionError(reason, 409) from error
        finally:
            with self._creation_lock:
                self._creating.discard(request_id)

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
