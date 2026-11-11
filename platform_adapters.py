"""Portable platform transports; no desktop automation or account scraping.

Grok Bot's routine receipt and Cursor's native run receipt mean accepted, not
completed. Only a scoped callback or the selected native run can finish work.
"""
import base64
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import threading
import time
import urllib.parse
from command_adapters import DeliveryError

CURSOR_AGENT = re.compile(r'bc-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z')
CURSOR_RUN = re.compile(r'run-[A-Za-z0-9_-]{1,160}\Z')
MARKER = re.compile(r'^\[OFFICE:([0-9a-f-]{36})\]\n')


class RemoteError(Exception):
    def __init__(self, code, uncertain=False):
        self.code, self.uncertain = code, uncertain
        super().__init__(code)


def secret_file(path):
    p = Path(path)
    if not p.is_absolute() or not p.is_file() or p.stat().st_size > 16384:
        raise ValueError('platform_credentials_unavailable')
    if os.name != 'nt' and p.stat().st_mode & 0o077:
        raise ValueError('platform_credentials_permissions')
    data = json.loads(p.read_text())
    if not isinstance(data, dict):
        raise ValueError('platform_credentials_unavailable')
    return data


def validate_key(value):
    if not isinstance(value, str) or not 8 <= len(value) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError('invalid_platform_key')
    return value


def webhook_url(value):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) <= 32 for c in value):
        raise ValueError('invalid_webhook_url')
    try:
        u = urllib.parse.urlsplit(value)
        if (u.scheme != 'https' or not u.hostname or u.username or u.password or u.fragment or
                u.port not in (None, 443) or not u.path or u.path == '/'):
            raise ValueError()
        # These are vendor endpoints, not an arbitrary outbound URL executor.
        if not any(u.hostname == host or u.hostname.endswith('.' + host)
                   for host in ('cursor.com', 'cursor.sh', 'grok.com', 'x.ai')):
            raise ValueError()
    except ValueError:
        raise ValueError('invalid_webhook_url')
    return value


class HTTPS:
    """HTTPS only, verified public DNS pinned to the connected socket, no redirects.

    Never passes credentials to another host, proxy, or a redirect destination.
    Errors contain fixed codes, never response bodies, URLs, headers or prompts.
    """
    def open(self, method, url, headers, body=None):
        u = urllib.parse.urlsplit(url)
        if u.scheme != 'https' or not u.hostname or u.username or u.password or u.port not in (None, 443):
            raise RemoteError('invalid_platform_endpoint')
        try:
            records = socket.getaddrinfo(u.hostname, 443, type=socket.SOCK_STREAM)
            addresses = list(dict.fromkeys(item[4][0] for item in records))
            if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
                raise RemoteError('platform_endpoint_not_public')
            raw = socket.create_connection((addresses[0], 443), timeout=15)
            try:
                tls = ssl.create_default_context().wrap_socket(raw, server_hostname=u.hostname)
            except Exception:
                raw.close()
                raise
        except RemoteError:
            raise
        except (OSError, ValueError):
            raise RemoteError('platform_connection_failed')
        connection = http.client.HTTPSConnection(u.hostname, timeout=15)
        connection.sock = tls
        try:
            path = urllib.parse.urlunsplit(('', '', u.path or '/', u.query, ''))
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                status = response.status
                code = {401:'platform_auth_required',403:'platform_access_denied',404:'platform_target_not_found',
                        409:'platform_agent_busy',410:'platform_stream_expired',429:'platform_rate_limited'}.get(status, 'platform_request_failed')
                raise RemoteError(code, method == 'POST' and status >= 500)
            return connection, response
        except RemoteError:
            connection.close()
            raise
        except (OSError, http.client.HTTPException):
            connection.close()
            raise RemoteError('platform_delivery_unknown' if method == 'POST' else 'platform_read_failed', method == 'POST')

    def json(self, method, url, headers, data=None):
        body = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode() if data is not None else None
        connection, response = self.open(method, url, {'Content-Type':'application/json', **headers}, body)
        try:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError()
            value = json.loads(raw) if raw else {}
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, OSError, http.client.HTTPException):
            raise RemoteError('platform_invalid_response', method == 'POST')
        finally:
            connection.close()


class GrokWebhookAdapter:
    mode, serial, same_session = 'grok_webhook', True, False

    def __init__(self, config, emit=None, transport=None):
        self.config, self.emit = config, emit
        self.sessions = config.get('command_adapters', {}).get('grok_webhook', {}).get('sessions', {})
        self.transport, self.reason = transport or HTTPS(), 'webhook_not_configured'

    def credentials(self, sid):
        value = secret_file(self.sessions[sid]['credential_file'])
        webhook_url(value.get('url')); validate_key(value.get('key'))
        return value

    def available(self, sid):
        try:
            self.credentials(sid)
            return True
        except (ValueError, OSError, KeyError, TypeError):
            return False

    def submit(self, sid, text):
        match = MARKER.match(text)
        if not match or not self.available(sid):
            raise DeliveryError('webhook_not_configured')
        value = self.credentials(sid)
        payload = {'office': {'version':1, 'command_id':match[1], 'agent_id':'grok_bot:' + sid},
                   'prompt': {'text':text}}
        try:
            body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
            connection, response = self.transport.open('POST', value['url'],
                {'Content-Type':'application/json', 'Authorization':'Bearer ' + value['key']}, body)
            try:
                # Grok Bot documents HTTP 200. Do not confuse Grok.com's signed
                # automation endpoint (202) with this Bot routine interface.
                if response.status != 200:
                    raise RemoteError('webhook_unrecognized_receipt', True)
            finally:
                connection.close()
        except RemoteError as error:
            raise DeliveryError(error.code, error.uncertain)
        return {'delivery_mode':self.mode, 'attention_code':'awaiting_agent_callback'}


def cursor_headers(key):
    return {'Authorization':'Basic ' + base64.b64encode((validate_key(key) + ':').encode()).decode()}


def cursor_agent_id(value):
    if isinstance(value, str) and value.startswith('https://cursor.com/agents/'):
        u = urllib.parse.urlsplit(value)
        value = u.path.removeprefix('/agents/').rstrip('/')
    if not isinstance(value, str) or not CURSOR_AGENT.fullmatch(value):
        raise ValueError('cursor_cloud_agent_required')
    return value


def sse_events(response, stop, duration=600):
    deadline = time.monotonic() + duration
    kind, data, size = 'message', [], 0
    while not stop.is_set() and time.monotonic() < deadline:
        line = response.readline(1024 * 1024 + 1)
        if not line:
            return
        size += len(line)
        if size > 1024 * 1024:
            raise RemoteError('platform_stream_frame_too_large')
        try:
            line = line.decode('utf-8').rstrip('\r\n')
        except UnicodeDecodeError:
            raise RemoteError('platform_invalid_stream')
        if line == '':
            if data:
                try:
                    value = json.loads('\n'.join(data))
                    if not isinstance(value, dict):raise ValueError()
                except ValueError:
                    raise RemoteError('platform_invalid_stream')
                yield kind, value
            kind, data, size = 'message', [], 0
        elif line.startswith('event:'):
            kind = line[6:].lstrip()
        elif line.startswith('data:'):
            data.append(line[5:].lstrip())


class CursorCloudAdapter:
    mode, serial, same_session = 'cursor_cloud', True, True
    base = 'https://api.cursor.com/v1/agents/'

    def __init__(self, config, emit, transport=None):
        self.config, self.emit = config, emit
        self.sessions = config.get('command_adapters', {}).get('cursor_cloud', {}).get('sessions', {})
        self.transport, self.reason = transport or HTTPS(), 'cursor_api_key_required'
        self.lock, self.workers, self.connections = threading.RLock(), {}, {}
        self.stop = threading.Event()

    def headers(self, sid):
        return cursor_headers(secret_file(self.sessions[sid]['credential_file']).get('api_key'))

    def available(self, sid):
        try:
            cursor_agent_id(sid); self.headers(sid)
            return not self.stop.is_set()
        except (ValueError, OSError, KeyError, TypeError):
            return False

    def can_dispatch(self, sid):
        with self.lock:
            return not self.stop.is_set() and not (sid in self.workers and self.workers[sid].is_alive())

    def event(self, sid, command, native, kind, **fields):
        self.emit('cursor:' + sid, {'type':kind, 'adapter':self.mode, 'session_id':sid,
            'run_id':command, 'native_run_id':native, **fields})

    def submit(self, sid, text):
        match = MARKER.match(text)
        if not match or not self.available(sid):
            raise DeliveryError('cursor_api_key_required')
        if not self.can_dispatch(sid):
            raise DeliveryError('platform_agent_busy')
        try:
            value = self.transport.json('POST', self.base + sid + '/runs', self.headers(sid), {'prompt':{'text':text}})
        except RemoteError as error:
            raise DeliveryError(error.code, error.uncertain)
        run = value.get('run', {})
        native = run.get('id')
        if run.get('agentId') != sid or not isinstance(native, str) or not CURSOR_RUN.fullmatch(native):
            raise DeliveryError('cursor_invalid_run_receipt', True)
        command = match[1]
        # Persist the native run before starting observation. A restart can GET
        # this exact run; it must never POST the prompt again.
        self.event(sid, command, native, 'accepted')
        self.resume(sid, command, native, text)
        return {'native_message_id':native, 'delivery_mode':self.mode}

    def resume(self, sid, command, native, text, recovery=False, last_segment=0):
        if not isinstance(native, str) or not CURSOR_RUN.fullmatch(native) or not self.available(sid):
            return
        with self.lock:
            if not self.can_dispatch(sid):return
            worker = threading.Thread(target=self.observe, args=(sid, command, native, text, recovery, last_segment), daemon=True)
            self.workers[sid] = worker
            worker.start()

    def observe(self, sid, command, native, prompt, recovery=False, last_segment=0):
        url = self.base + sid + '/runs/' + native
        started, text, segment = False, '', 0

        def begin():
            nonlocal started
            if not started:
                self.event(sid, command, native, 'started', text=prompt, recovery=recovery)
                started = True

        def flush(final=False):
            if text:
                self.event(sid, command, native, 'text', text=text, segment=segment, final=final)

        def finish(value, key='text'):
            nonlocal text, segment
            if value.get('runId', value.get('id')) != native or value.get('agentId', sid) != sid:
                raise RemoteError('cursor_run_mismatch')
            state = {'FINISHED':'completed','ERROR':'failed','CANCELLED':'cancelled','EXPIRED':'failed'}.get(value.get('status'))
            if not state:return False
            begin()
            # The final authoritative reply supersedes the last streamed segment.
            # After restart, preserve earlier commentary instead of replacing
            # segment zero with the final result from GET.
            if recovery:segment=max(segment,last_segment)
            text = value.get(key) if isinstance(value.get(key), str) else ''
            flush(True)
            self.event(sid, command, native, 'finished', state=state, text=text,
                detail_code=None if text or state != 'completed' else 'completed_without_reply')
            return True

        connection = None
        try:
            # A terminal run can be recovered after stream retention expires.
            if recovery:
                value = self.transport.json('GET', url, self.headers(sid))
                if finish(value, 'result'):return
            try:
                connection, response = self.transport.open('GET', url + '/stream',
                    {**self.headers(sid), 'Accept':'text/event-stream'})
                with self.lock:self.connections[sid] = connection
                for kind, value in sse_events(response, self.stop):
                    if kind == 'status':
                        if value.get('runId') != native:raise RemoteError('cursor_run_mismatch')
                        if value.get('status') in {'RUNNING','WAITING_FOR_BACKGROUND_WORK'}:begin()
                    elif kind == 'assistant' and isinstance(value.get('text'), str):
                        begin(); text = (text + value['text'])[:256000]; flush()
                    elif kind == 'tool_call':
                        begin()
                        if value.get('status') == 'running':
                            flush()
                            if text:segment += 1; text = ''
                            self.event(sid, command, native, 'status', step='Cursor 正在執行工具')
                    elif kind == 'result' and finish(value):return
                    elif kind == 'done':break
                    elif kind == 'error':break
                    # Never save thinking, interaction_update, raw tool args/results.
            except (RemoteError, OSError, http.client.HTTPException):
                pass
            if not self.stop.is_set():
                value = self.transport.json('GET', url, self.headers(sid))
                if finish(value, 'result'):return
            raise RemoteError('cursor_observation_interrupted')
        except Exception as error:
            self.event(sid, command, native, 'interrupted', state='delivery_unknown',
                detail_code=error.code if isinstance(error, RemoteError) else 'cursor_observation_interrupted')
        finally:
            if connection:connection.close()
            with self.lock:self.connections.pop(sid, None)

    def close(self):
        self.stop.set()
        for connection in list(self.connections.values()):connection.close()
        for worker in list(self.workers.values()):
            if worker.is_alive():worker.join(timeout=16)
