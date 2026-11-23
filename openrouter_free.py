"""Installation-owned, text-only conversations using OpenRouter's free variants."""
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import threading

from command_adapters import DeliveryError
from configuration import OPENROUTER_MODEL, agent_name, load_config, save_config
from connections import ConnectionError
from history import redact
from platform_adapters import HTTPS, MARKER, RemoteError, validate_key
from private_files import require_private

API = 'https://openrouter.ai/api/v1'
LIGHTNING = 'nvidia/nemotron-3.5-lightning:free'
SUPER_VL = 'nvidia/nemotron-3.5-super-vl:free'
ERRORS = {
    'openrouter_model_unavailable': '所選免費模型暫時不在可用清單，請稍後再試。',
    'openrouter_catalog_unavailable': '暫時無法核對免費模型清單，請稍後再試。',
    'openrouter_key_required': '請在電腦的「加入 Agent → OpenRouter」設定 API key。',
    'platform_auth_required': 'OpenRouter 未接受這把 API key，請在電腦更新。',
    'platform_access_denied': 'OpenRouter 或供應商未允許這次請求，請核對帳戶設定。',
    'openrouter_permission_changed': '發送權限已變更，這次要求已取消。',
    'platform_rate_limited': '免費額度或供應商流量已達上限，請稍後再發送。',
    'openrouter_account_limit': 'OpenRouter 帳戶限制了這次要求，請核對免費額度及帳戶設定。',
    'openrouter_incomplete': '模型未交付完整文字回覆。',
    'openrouter_unconfirmed': '未能核對模型的交付結果，OFFICE 已停止這次要求。',
    'openrouter_unavailable': 'OpenRouter 暫時無法處理這次要求。',
}


REQUEST_ERRORS = {401: 'platform_auth_required', 402: 'openrouter_account_limit',
                  403: 'platform_access_denied', 404: 'openrouter_model_unavailable',
                  429: 'platform_rate_limited'}


def zero(value):
    try:
        return Decimal(str(value)).is_zero()
    except (InvalidOperation, ValueError):
        return False


def free_models(transport):
    try:
        items = transport.json('GET', API + '/models', {}).get('data')
    except RemoteError:
        raise DeliveryError('openrouter_catalog_unavailable') from None
    if not isinstance(items, list):
        raise DeliveryError('openrouter_catalog_unavailable')
    models = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        model, prices, architecture = item.get('id'), item.get('pricing'), item.get('architecture')
        if (not isinstance(model, str) or not re.fullmatch(OPENROUTER_MODEL, model)
                or not isinstance(prices, dict) or not {'prompt', 'completion'} <= prices.keys()
                or not all(zero(value) for value in prices.values()) or not isinstance(architecture, dict)
                or not isinstance(architecture.get('input_modalities'), list)
                or 'text' not in architecture['input_modalities']
                or architecture.get('output_modalities') != ['text']):
            continue
        name = item.get('name')
        models[model] = name[:160] if isinstance(name, str) and name.strip() else model
    return models


def key_path(config):
    return Path(config['data_dir']) / 'platform-secrets' / 'openrouter.json'


def read_key(config):
    path = key_path(config)
    try:
        require_private(path.parent, directory=True)
        require_private(path)
        with path.open(encoding='utf-8') as stream:
            require_private(path, fd=stream.fileno())
            if os.fstat(stream.fileno()).st_size > 16384:
                raise ValueError()
            return validate_key(json.load(stream)['api_key'])
    except (ValueError, TypeError, KeyError, OSError):
        raise DeliveryError('openrouter_key_required') from None


class OpenRouterConnections:
    def __init__(self, bridge, transport=None):
        self.bridge = bridge
        self.transport = transport or HTTPS()

    def status(self):
        try:
            models = free_models(self.transport)
        except DeliveryError as error:
            raise ConnectionError(error.code, 503) from None
        preferred = {LIGHTNING: 0, SUPER_VL: 1}
        ordered = sorted(models, key=lambda model: (preferred.get(model, 2), models[model].casefold()))
        try:
            read_key(self.bridge.config)
            saved = True
        except DeliveryError:
            saved = False
        return {'key_saved': saved, 'models': [{'id': m, 'name': models[m]} for m in ordered]}

    def set_key(self, data):
        if not isinstance(data, dict) or set(data) != {'api_key'}:
            raise ConnectionError('invalid_platform_key')
        try:
            key = validate_key(data['api_key'])
        except ValueError:
            raise ConnectionError('invalid_platform_key') from None
        try:
            identity = self.transport.json('GET', API + '/key', {'Authorization': 'Bearer ' + key}).get('data')
        except RemoteError as error:
            raise ConnectionError(error.code, 422) from None
        if not isinstance(identity, dict) or not identity or identity.get('is_management_key'):
            raise ConnectionError('openrouter_inference_key_required', 422)
        b = self.bridge
        with b.commands.lock, b.history.lock:
            save_config(key_path(b.config), {'api_key': key})
            if key not in b.history.secrets:
                b.history.secrets += (key,)
        return {'key_saved': True}

    def create(self, data):
        if not isinstance(data, dict) or set(data) != {'name', 'model', 'request_id'}:
            raise ConnectionError('invalid_connection')
        try:
            name = agent_name(data['name'])
        except ValueError:
            raise ConnectionError('invalid_name') from None
        rid, model = data['request_id'], data['model']
        if not isinstance(rid, str) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', rid):
            raise ConnectionError('invalid_request_id')
        if not isinstance(model, str) or not re.fullmatch(OPENROUTER_MODEL, model):
            raise ConnectionError('openrouter_model_unavailable')
        sid, ident = 'or-' + rid, 'openrouter:or-' + rid
        b = self.bridge
        result = {'agent_id': ident, 'name': name, 'model': model, 'mode': 'openrouter', 'scope': 'office_owned_session'}
        def duplicate(config):
            if ident in config.get('logged_out_agent_ids', []):
                raise ConnectionError('agent_logged_out', 409)
            existing = next((a for a in config['agents'] if a['id'] == ident), None)
            if existing:
                settings = config['command_adapters']['openrouter']['sessions'][sid]
                if existing['name'] != name or settings['model'] != model:
                    raise ConnectionError('request_id_conflict', 409)
            return existing is not None
        with b.commands.lock:
            if duplicate(b.config):
                return result
        try:
            read_key(b.config)
            if model not in free_models(self.transport):
                raise DeliveryError('openrouter_model_unavailable')
        except DeliveryError as error:
            raise ConnectionError(error.code, 503 if error.code == 'openrouter_catalog_unavailable' else 422) from None
        with b.commands.lock, b.history.lock:
            config = load_config(b.config['config_path'])
            if duplicate(config):
                return result
            if any(a['name'] == name for a in config['agents']):
                raise ConnectionError('name_in_use', 409)
            config['agents'].append({'id': ident, 'provider': 'openrouter', 'source_id': sid, 'driver': 'openrouter', 'name': name})
            config.setdefault('command_bindings', {})[ident] = {'adapter': 'openrouter', 'session_id': sid}
            config.setdefault('command_adapters', {}).setdefault('openrouter', {}).setdefault('sessions', {})[sid] = {'model': model}
            b.connections.save(config)
        return result


class OpenRouterAdapter:
    mode = 'openrouter'
    serial = True
    same_session = True  # The conversation and its identity are owned by OFFICE.

    def __init__(self, config, emit, history, transport=None):
        self.config, self.emit, self.history = config, emit, history
        self.transport = transport or HTTPS(response_timeout=90)
        self.sessions = config.get('command_adapters', {}).get('openrouter', {}).get('sessions', {})
        self.guard = threading.RLock()
        self.callbacks = threading.RLock()
        self.authorize = None
        self.closed = threading.Event()
        self.active = False
        self.reason = None

    def available(self, sid):
        ready = sid in self.sessions and key_path(self.config).is_file() and not self.closed.is_set()
        self.reason = None if ready else 'openrouter_key_required'
        return ready

    def can_dispatch(self, sid):
        with self.guard:
            return not self.active and not self.closed.is_set()

    def context(self, agent, prompt):
        with self.history.lock:
            rows = self.history.db.execute(
                "SELECT prompt,delivery FROM runs WHERE agent_id=? AND source_kind='openrouter' "
                "AND status='done' ORDER BY finished_at DESC, rowid DESC LIMIT 12", (agent,)).fetchall()
        turns, remaining = [], 24000
        for row in rows:
            user, assistant = row['prompt'], row['delivery']
            size = len(user) + len(assistant)
            if size > remaining:
                break
            if user and assistant:
                turns.append((user, assistant))
                remaining -= size
        messages = []
        for user, assistant in reversed(turns):
            messages.extend([{'role': 'user', 'content': user}, {'role': 'assistant', 'content': assistant}])
        messages.append({'role': 'user', 'content': redact(prompt, self.history.secrets)})
        return messages

    def submit(self, sid, marked):
        match = MARKER.match(marked)
        if not match or not self.available(sid):
            raise DeliveryError(self.reason or 'invalid_command')
        with self.guard:
            if not self.can_dispatch(sid):
                raise DeliveryError('platform_agent_busy')
            model = self.sessions[sid]['model']
            self.active = True
            worker = threading.Thread(target=self.run, args=(sid, model, match[1], marked, marked[match.end():]), daemon=True)
            try:
                worker.start()
            except RuntimeError:
                self.active = False
                raise DeliveryError('openrouter_unavailable') from None
        return {'delivery_mode': self.mode}

    def run(self, sid, model, run, marked, prompt):
        agent = 'openrouter:' + sid
        def event(kind, **fields):
            with self.callbacks:
                if not self.closed.is_set():
                    self.emit(agent, {'type': kind, 'run_id': run, 'adapter': self.mode, 'session_id': sid, **fields})
        attempted = False
        try:
            event('started', text=marked)
            key = read_key(self.config)
            if model not in free_models(self.transport):
                raise DeliveryError('openrouter_model_unavailable')
            body = {'model': model, 'messages': self.context(agent, prompt), 'stream': False,
                    'max_tokens': 4096, 'plugins': [{'id': 'web', 'enabled': False}],
                    'provider': {'allow_fallbacks': False,
                    'max_price': {'prompt': 0, 'completion': 0, 'request': 0, 'image': 0}}}
            with self.callbacks:
                if self.closed.is_set():
                    return
                if not self.authorize or not self.authorize(agent, sid, run):
                    event('finished', state='cancelled', detail_code='openrouter_permission_changed',
                          text=ERRORS['openrouter_permission_changed'])
                    return
            attempted = True
            reply = self.transport.json('POST', API + '/chat/completions', {'Authorization': 'Bearer ' + key}, body)
            if 'error' in reply:
                failure = reply['error']
                status = failure.get('code') if isinstance(failure, dict) else None
                reason = REQUEST_ERRORS.get(status) if isinstance(status, int) else None
                raise DeliveryError(reason or 'openrouter_unconfirmed', uncertain=reason is None)
            choice = (reply.get('choices') or [{}])[0]
            message = choice.get('message') or {}
            value = message.get('content')
            usage = reply.get('usage') or {}
            reported = reply.get('model')
            if (reported not in {model, model.removesuffix(':free')}
                    or ('cost' in usage and not zero(usage['cost']))
                    or (reported != model and not zero(usage.get('cost')))):
                raise DeliveryError('openrouter_unconfirmed', uncertain=True)
            if (choice.get('finish_reason') != 'stop' or message.get('role') != 'assistant'
                    or message.get('tool_calls') or not isinstance(value, str) or not value.strip()):
                raise DeliveryError('openrouter_incomplete')
            event('text', segment=1, text=value, final=True)
            native = reply.get('id')
            event('finished', state='completed', text=value,
                  native_run_id=native if isinstance(native, str) and re.fullmatch(r'[\w.-]{1,180}', native) else None)
        except (RemoteError, DeliveryError) as error:
            code = REQUEST_ERRORS.get(getattr(error, 'http_status', None), error.code)
            if code not in ERRORS:
                code = 'openrouter_unconfirmed' if error.uncertain else 'openrouter_unavailable'
            event('finished', state='delivery_unknown' if error.uncertain else 'failed', detail_code=code, text=ERRORS[code])
        except Exception:
            code = 'openrouter_unconfirmed' if attempted else 'openrouter_unavailable'
            event('finished', state='delivery_unknown' if attempted else 'failed', detail_code=code, text=ERRORS[code])
        finally:
            with self.guard:
                self.active = False

    def close(self):
        with self.callbacks:
            self.closed.set()
