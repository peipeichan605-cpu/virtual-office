"""Least-privilege projections and dispatch for paired phones.

The HTTP gateway authenticates the Serve identity and cookie on each request.
This layer still refreshes the principal, enforces per-agent grants, and never
forwards an arbitrary owner API route or returns native configuration.
"""
import hashlib
import json
import re
from urllib.parse import urlsplit

from history import redact
from mobile_auth import MobileAuth, MobileAuthError
from security_keys import security_secrets, upgraded_security


class MobileServiceError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


def public_origin(config):
    settings = config.get('mobile', {})
    if not isinstance(settings, dict):
        return None
    value = settings.get('public_origin', '')
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == 'https' and parsed.hostname and
                 re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,220})\.ts\.net', parsed.hostname) and
                 '..' not in parsed.hostname and
                 parsed.port is None and not parsed.username and not parsed.password and
                 not parsed.path and not parsed.query and not parsed.fragment and
                 parsed.netloc == parsed.hostname)
    except ValueError:
        valid = False
    return value if valid else None


def _query(values, allowed, required=()):
    if not isinstance(values, dict) or set(values) - set(allowed):
        raise MobileServiceError(400, 'invalid_mobile_query')
    result = {}
    for key, value in values.items():
        if isinstance(value, list):
            if len(value) != 1:
                raise MobileServiceError(400, 'invalid_mobile_query')
            value = value[0]
        if not isinstance(value, str) or not value or len(value) > 512 or any(ord(c) < 32 for c in value):
            raise MobileServiceError(400, 'invalid_mobile_query')
        result[key] = value
    if set(required) - result.keys():
        raise MobileServiceError(400, 'invalid_mobile_query')
    return result


class MobileService:
    def __init__(self, bridge, *, auth=None):
        self.bridge, self.config = bridge, bridge.config
        self.auth = auth or MobileAuth(self.config, selected_agents=self.selected_agents)
        bridge.commands.mobile_authorizer = self.validate_command_origin
        # Startup membership recovery runs before MobileService is constructed.
        # Finish a logout interrupted after config retirement but before the
        # durable phone grant was removed, before the same native ID can rejoin.
        for agent in set(self.config.get('logged_out_agent_ids', [])) - set(bridge.definitions):
            self.invalidate_agent(agent)

    def validate_command_origin(self, origin, agent_id=None):
        # Changing or disabling this installation's remote identity is also a
        # dispatch fence, even while a native Agent remains offline.
        if not self.ready():
            return False
        with self.auth.lock:
            if not self.auth.validate_command_origin(origin, agent_id):
                return False
            return any(device['device_id'] == origin['device_id'] and
                       device['revision'] == origin['revision'] and device['state'] == 'ready' and
                       device['identity'] == self.config['mobile']['identity']
                       for device in self.auth.list_devices())

    def selected_agents(self):
        """Bind a grant to this membership, not a display name or account roster."""
        result = {}
        bindings = self.config.get('command_bindings', {})
        for agent, definition in self.bridge.definitions.items():
            if agent not in self.bridge.agents:
                continue
            binding = bindings.get(agent, {})
            value = {key: definition.get(key) for key in
                     ('id', 'provider', 'source_id', 'driver', 'token_sha256',
                      'native_observe_after', 'joined_at', 'generation')}
            value['binding'] = binding
            value['mobile_origin'] = public_origin(self.config)
            result[agent] = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return result

    def ready(self):
        settings = self.config.get('mobile', {})
        return bool(isinstance(settings, dict) and upgraded_security(self.config) and settings.get('enabled') is True and
                    public_origin(self.config) and isinstance(settings.get('identity'), str) and
                    settings['identity'].strip() == settings['identity'] and settings['identity'])

    def _require_ready(self):
        if not self.ready():
            raise MobileServiceError(503, 'mobile_not_configured')

    def _principal(self, principal):
        self._require_ready()
        principal = self.auth.refresh(principal)
        if principal.get('identity') != self.config['mobile']['identity']:
            raise MobileServiceError(403, 'mobile_identity_mismatch')
        return principal

    def _clean(self, value, limit=24000):
        secrets = (*security_secrets(self.config), *self.bridge.history.secrets)
        return redact(value, secrets, limit)

    def _capability(self, capability, can_send=False):
        allowed = ('connected', 'native_ready', 'same_session', 'persistent', 'requires_active_turn',
                   'receiver_online', 'mode', 'reason', 'conversation_scope', 'tool_policy',
                   'tools_enabled', 'approval_policy', 'native_target_verified', 'execution_verified')
        result = {key: capability[key] for key in allowed if key in capability and
                  isinstance(capability[key], (str, bool, int, type(None)))}
        result['can_send'] = bool(can_send and capability.get('can_send') and capability.get('connected'))
        if not can_send:
            result['reason'] = 'mobile_view_only'
        return result

    def _message(self, row):
        if not isinstance(row, dict):
            return None
        result = {key: row[key] for key in ('seq', 'agent_id', 'run_id', 'role', 'phase', 'at') if key in row}
        result['text'] = self._clean(row.get('text', ''))
        return result

    def _descriptor(self, row, *, selected=None, can_send=False):
        result = {key: row[key] for key in ('id', 'provider', 'state', 'state_changed_at', 'ground_desk_id') if key in row}
        result['name'] = self._clean(row.get('name', ''), 80)
        if selected is not None:
            result['selected'] = bool(selected)
        if row.get('is_subagent') is True:
            result.update(is_subagent=True)
            for key in ('parent_agent_id', 'root_agent_id', 'parent_run_id', 'depth'):
                if key in row:
                    result[key] = row[key]
            if row.get('evidence') in {'native_hook', 'native_metadata', 'parent_reported'}:
                result['evidence'] = row['evidence']
            result['command_capability'] = {'can_send': False, 'enabled': False, 'mode': 'observation', 'reason': 'observed_subagent'}
        else:
            result['command_capability'] = self._capability(row.get('command_capability', {}), can_send)
        if 'step' in row:
            result['step'] = self._clean(row['step'], 600)
        workspace = row.get('workspace')
        if isinstance(workspace, dict):
            result['workspace'] = {key: self._clean(workspace[key], 160) for key in ('id', 'name') if isinstance(workspace.get(key), str)}
        if row.get('latest_message'):
            result['latest_message'] = self._message(row['latest_message'])
        return result

    def _run(self, row):
        fields = ('id', 'agent_id', 'run_id', 'provider', 'status', 'started_at', 'finished_at', 'updated_at',
                  'is_subagent', 'parent_agent_id', 'root_agent_id', 'parent_run_id', 'depth', 'evidence')
        result = {key: row[key] for key in fields if key in row}
        for key in ('name', 'prompt', 'delivery', 'prompt_preview', 'delivery_preview', 'sync_warning'):
            if key in row:
                result[key] = self._clean(row[key])
        return result

    def _allowed(self, principal):
        roots = set()
        for agent in principal.get('view_agents', []):
            try:
                self.auth.authorize(principal, agent, 'view')
                roots.add(agent)
            except MobileAuthError:
                continue
        descriptors = self.bridge.history_agents()
        allowed = {agent: row for agent, row in descriptors.items() if
                   (agent in roots and agent in self.bridge.agents) or
                   (row.get('is_subagent') is True and row.get('root_agent_id') in roots)}
        return allowed, roots

    def _send_scope(self, principal, agent):
        # A GET may display an existing send grant without possessing a CSRF
        # proof. The mutation path separately calls authorize(..., 'send').
        if agent not in self.bridge.agents or agent not in principal.get('send_agents', []):
            return False
        try:
            self.auth.authorize(principal, agent, 'view')
            return True
        except MobileAuthError:
            return False

    def _agent(self, agent, allowed):
        if agent not in allowed:
            raise MobileServiceError(404, 'agent_not_found')
        return allowed[agent]

    def _decorate(self, rows):
        children = getattr(self.bridge, 'children', None)
        if children:
            rows = children.decorate_runs(rows)
        observer = getattr(self.bridge, 'grok_sdk_sync', None)
        if observer:
            rows = [observer.decorate_run(row) for row in rows]
        return rows

    def _health(self, roots):
        """Office observation health, never proof that native Agents are online.

        The phone reads this local projection directly; an unrelated legacy
        publisher outage does not interrupt it. Missing sources are checked
        only for granted roots, and no global roster counts or errors escape.
        State-change timestamps cannot prove freshness: an idle Agent can keep
        the same state for hours while its observer remains healthy.
        """
        stop = getattr(self.bridge, 'stop', None)
        running = stop is not None and not stop.is_set()
        health = getattr(self.bridge, 'health', {})
        sources = getattr(self.bridge, 'sources', {})
        observed = all(self.bridge.agents[agent]['name'] in sources for agent in roots)
        healthy = bool(running and observed and not any(health.get(key) for key in
                       ('source_lookup_error', 'history_error', 'subagent_lookup_error', 'child_observer_error')))
        return {'ok': running, 'local_ok': healthy, 'sync_ok': healthy}

    def read(self, route, query, principal):
        principal = self._principal(principal)
        route = route.removeprefix('/api/mobile/')
        with self.bridge.commands.lock, self.bridge.history.lock:
            allowed, roots = self._allowed(principal)
            if route == 'health':
                _query(query, ())
                return self._health(roots)
            if route == 'work':
                _query(query, ())
                snapshot = self.bridge.work_snapshot()
                agents = [self._descriptor(row, can_send=self._send_scope(principal, row['id']))
                          for row in snapshot['agents'] if row.get('id') in allowed]
                for row in agents:
                    if not row.get('is_subagent'):
                        row['child_count'] = sum(child.get('root_agent_id') == row['id'] for child in agents if child.get('is_subagent'))
                finished = [self._run(row) for row in snapshot.get('finished', []) if row.get('agent_id') in allowed]
                return {'agents': agents, 'finished': finished, 'feed_enabled': False,
                        'brand': self._clean(snapshot.get('brand', 'VIRTUAL OFFICE'), 120),
                        'subagent_count': sum(row.get('is_subagent') is True for row in agents),
                        'subagents_ok': snapshot.get('subagents_ok', True), 'history_ok': snapshot.get('history_ok', True),
                        'grouping': 'workspace', 'local_ok': self._health(roots)['local_ok']}
            if route == 'messages':
                q = _query(query, ('agent', 'run', 'after', 'before'), ('agent',))
                descriptor = self._agent(q['agent'], allowed)
                try:
                    after = int(q.get('after', '0')); before = int(q['before']) if 'before' in q else None
                    if after < 0 or after > 2**63-1 or before is not None and not 0 < before <= 2**63-1:
                        raise ValueError()
                except ValueError:
                    raise MobileServiceError(400, 'invalid_cursor')
                rows = self.bridge.history.messages(allowed, q['agent'], run=q.get('run'), after=after, before=before)
                return {'agent': self._descriptor(descriptor, selected=q['agent'] in roots),
                        'messages': [self._message(row) for row in rows], 'cursor': rows[-1]['seq'] if rows else after,
                        'before': rows[0]['seq'] if len(rows) == 100 else None}
            if route == 'history':
                q = _query(query, ('agent', 'before'), ('agent',))
                descriptor = self._agent(q['agent'], allowed)
                rows = self._decorate(self.bridge.history.list(allowed, agent=q['agent'], before=q.get('before')))
                return {'agent': self._descriptor(descriptor, selected=q['agent'] in roots),
                        'runs': [self._run(row) for row in rows],
                        'next': rows[-1]['started_at'] + '|' + rows[-1]['id'] if len(rows) == 60 else None}
            if route == 'run':
                q = _query(query, ('id',), ('id',))
                if not re.fullmatch(r'[a-f0-9]{32}', q['id']):
                    raise MobileServiceError(404, 'run_not_found')
                row = self.bridge.history.detail(q['id'], allowed)
                if not row:
                    raise MobileServiceError(404, 'run_not_found')
                row = self._decorate([row])[0]
                observer = getattr(self.bridge, 'cursor_cloud_sync', None)
                if observer:
                    row = observer.decorate_run(row)
                return self._run(row)
            if route == 'commands':
                q = _query(query, ('agent',), ('agent',))
                self._agent(q['agent'], allowed)
                can_send = self._send_scope(principal, q['agent'])
                if q['agent'] not in roots:
                    return {'capability': {'can_send': False, 'connected': False, 'mode': 'observation'}, 'commands': [], 'approvals': []}
                rows = self.bridge.commands.list_mobile_view(q['agent'], principal['device_id'] if can_send else None,
                                                             revision=principal['revision'])
                return {'capability': self._capability(rows['capability'], can_send),
                        'commands': [self._command(row) for row in rows['commands']], 'approvals': []}
        raise MobileServiceError(404, 'not_found')

    def _command(self, row):
        result = {key: row[key] for key in ('id', 'agent_id', 'state', 'run_id', 'created_at', 'updated_at', 'detail_code', 'can_cancel') if key in row}
        result['preview'] = self._clean(row.get('preview', ''), 6000)
        return result

    def send(self, data, principal):
        principal = self._principal(principal)
        if not isinstance(data, dict) or set(data) != {'request_id', 'agent_id', 'text'}:
            raise MobileServiceError(400, 'invalid_command')
        agent = data['agent_id']
        with self.bridge.commands.lock:
            self.auth.authorize(principal, agent, 'send')
            if agent not in self.bridge.agents or not self.bridge.commands.capability(agent).get('connected'):
                raise MobileServiceError(409, 'not_connected')
            origin = self.auth.command_origin(principal, agent)
            result = self.bridge.commands.create(data, origin=origin)
            return {'command': self._command(result['command']), 'duplicate': result['duplicate']}

    def cancel(self, data, principal):
        principal = self._principal(principal)
        if not isinstance(data, dict) or set(data) != {'agent_id', 'command_id'}:
            raise MobileServiceError(400, 'invalid_command')
        with self.bridge.commands.lock:
            self.auth.authorize(principal, data['agent_id'], 'send')
            result = self.bridge.commands.cancel_pending(data, origin=self.auth.command_origin(principal, data['agent_id']))
            return {'command': self._command(result['command']), 'duplicate': result['duplicate']}

    def owner_status(self):
        from mobile_setup import connection_setup, host_setup_prompt
        settings = self.config.get('mobile', {})
        setup = connection_setup(self.config, configured=self.ready())
        setup['setup_prompt'] = host_setup_prompt(self.config)
        return {'configured': self.ready(), 'enabled': settings.get('enabled') is True,
                'setup': setup,
                'credentials_separated': upgraded_security(self.config), 'public_origin': public_origin(self.config),
                'identity': settings.get('identity') or None, 'devices': self.auth.list_devices(),
                'agents': [{'id': row['id'], 'name': self._clean(row.get('name', ''), 80), 'provider': row.get('provider'),
                            'connected': bool(self.bridge.commands.capability(row['id']).get('connected'))}
                           for row in self.bridge.agents.values()]}

    def owner_create_pairing(self, data):
        self._require_ready()
        required = {'identity', 'label', 'view_agents', 'send_agents'}
        if (not isinstance(data, dict) or not required <= set(data)
                or set(data) - required - {'view_mode', 'send_mode'}):
            raise MobileServiceError(400, 'invalid_pairing')
        if data['identity'] != self.config['mobile']['identity']:
            raise MobileServiceError(403, 'mobile_identity_mismatch')
        with self.bridge.commands.lock:
            if not isinstance(data['send_agents'], list):
                raise MobileServiceError(400, 'invalid_pairing')
            _, selected_sends = self.auth._scopes(data['view_agents'], data['send_agents'],
                                                  data.get('view_mode', 'selected'), data.get('send_mode', 'selected'))
            for agent in selected_sends:
                if not isinstance(agent, str) or agent not in self.bridge.agents or not self.bridge.commands.capability(agent).get('connected'):
                    raise MobileServiceError(400, 'mobile_agent_not_connected')
            result = self.auth.create_pairing(**data)
        result['public_origin'] = public_origin(self.config)
        result['pair_url'] = public_origin(self.config) + '/mobile/pair#pair=' + result['pairing_token']
        return result

    def owner_update_scopes(self, data):
        self._require_ready()
        required = {'device_id', 'view_agents', 'send_agents', 'view_mode', 'send_mode'}
        if not isinstance(data, dict) or set(data) != required:
            raise MobileServiceError(400, 'invalid_mobile_scope')
        with self.bridge.commands.lock:
            result = self.auth.update_scopes(**data)
            if result['changed']:
                result.update(self.bridge.commands.revoke_mobile_pending(data['device_id']))
            return result

    def owner_revoke(self, device_id):
        with self.bridge.commands.lock:
            device = self.auth.revoke(device_id)
            stopped = self.bridge.commands.revoke_mobile_pending(device_id)
        return {'device': device, **stopped}

    def logout(self, principal):
        principal = self._principal(principal)
        return self.owner_revoke(principal['device_id'])

    def logout_renewal(self, token, identity):
        """Revoke a remembered phone even after its short session expires.

        The gateway separately requires a same-origin, explicit renewal header.
        Keep authorization and cancellation under the normal dispatch lock.
        """
        self._require_ready()
        if identity != self.config['mobile']['identity']:
            raise MobileServiceError(403, 'mobile_identity_mismatch')
        with self.bridge.commands.lock:
            device = self.auth.renewal_device(token, identity)
            return self.owner_revoke(device['device_id'])

    def invalidate_agent(self, agent_id):
        with self.bridge.commands.lock:
            return self.auth.invalidate_agent(agent_id)

    def close(self):
        self.auth.close()
