"""Private phone HTTPS backend. Only Tailscale Serve may front this loopback listener.

This is a separate application boundary, never a proxy carrying an owner key.
No native transports, login APIs, invitations or arbitrary files are forwarded.
"""
from http.cookies import SimpleCookie
import http.client
import http.server
import json
import mimetypes
from pathlib import Path
import re
import secrets
import socket
import threading
import urllib.parse

COOKIE = '__Host-OfficePhone'
RENEW_COOKIE = '__Host-OfficePhoneRenew'
MAX_BODY = 32768


def settings(config):
    data = config.get('mobile', {})
    if not isinstance(data, dict) or set(data) - {'enabled', 'port', 'public_origin', 'identity'}:
        raise ValueError('Invalid mobile settings')
    if type(data.get('enabled', False)) is not bool:
        raise ValueError('Invalid mobile enabled setting')
    port = data.get('port', config.get('port', 4318) + 1)
    if type(port) is not int or not 1024 <= port <= 65535 or port == config.get('port'):
        raise ValueError('Mobile port must be a separate loopback port')
    origin = data.get('public_origin', '')
    identity = data.get('identity', '')
    if origin:
        parsed = urllib.parse.urlsplit(origin)
        if (parsed.scheme != 'https' or parsed.path or parsed.query or parsed.fragment
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,220})\.ts\.net', parsed.hostname or '')
                or '..' in (parsed.hostname or '') or parsed.netloc != parsed.hostname):
            raise ValueError('Mobile origin must be an exact HTTPS Tailscale DNS name')
    if not isinstance(identity, str) or len(identity) > 254 or any(ord(c) < 33 for c in identity):
        raise ValueError('Invalid Tailscale login identity')
    if data.get('enabled') and (not origin or not identity):
        raise ValueError('Mobile access needs its exact private origin and login identity')
    return dict(enabled=data.get('enabled', False), port=port, public_origin=origin, identity=identity)


class BoundedHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(self, address, handler, *, limit=16, deadline=10):
        if address[0] != '127.0.0.1':
            raise ValueError('Mobile backend must remain loopback')
        self.slots = threading.BoundedSemaphore(limit)
        self.request_deadline = deadline
        super().__init__(address, handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        # A socket inactivity timeout alone lets a slow client hold a slot by
        # sending a byte periodically. Bound the whole request as well; there
        # can be at most one timer for each of the 16 admitted workers.
        def expire():
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        timer = threading.Timer(self.request_deadline, expire)
        timer.daemon = True
        timer.start()
        try:
            super().process_request_thread(request, client_address)
        finally:
            timer.cancel()
            self.slots.release()

    def handle_error(self, request, client_address):
        pass  # Never emit request paths, headers, cookies or native errors.


class HeaderReader:
    def __init__(self, stream):
        self.stream, self.total = stream, 0

    def readline(self, size=-1):
        line = self.stream.readline(min(8193, size) if size >= 0 else 8193)
        self.total += len(line)
        if len(line) > 8192 or self.total > 32768:
            raise http.client.LineTooLong('mobile headers')
        return line


def handler(service, config, public_root):
    opts = settings(config)
    origin = opts['public_origin']
    host = urllib.parse.urlsplit(origin).hostname
    root = Path(public_root).resolve()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        server_version = 'OfficeMobile'
        sys_version = ''

        def log_message(self, *args):
            pass

        def parse_request(self):
            original = self.rfile
            self.rfile = HeaderReader(original)
            try:
                return super().parse_request()
            finally:
                self.rfile = original

        def send_error(self, code, message=None, explain=None):
            self.respond(code, {'error': 'invalid_request'})

        def respond(self, code, value, *, content_type='application/json; charset=utf-8', cookie=None, nonce=None):
            data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.close_connection = True
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Connection', 'close')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Strict-Transport-Security', 'max-age=31536000')
            self.send_header('Permissions-Policy', 'camera=(), microphone=(), geolocation=(), payment=()')
            script = "'self'" + (" 'nonce-" + nonce + "'" if nonce else '')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src " + script + "; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data: blob:; connect-src 'self' blob:; media-src 'self' blob:; worker-src 'self' blob:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'")
            for value in (cookie if isinstance(cookie, (tuple, list)) else [cookie] if cookie else []):
                self.send_header('Set-Cookie', value)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(data)

        def one(self, name, default=None):
            values = self.headers.get_all(name, [])
            if len(values) > 1:
                raise ValueError('duplicate_header')
            return values[0] if values else default

        def boundary(self, write=False):
            if not service.ready() or settings(config) != opts or self.client_address[0] != '127.0.0.1':
                raise PermissionError()
            if self.one('Host') not in {host, host + ':443'}:
                raise PermissionError()
            if self.one('Tailscale-User-Login') != opts['identity']:
                raise PermissionError()
            if self.one('Origin') not in ({origin} if write else {None, origin}):
                raise PermissionError()
            if len(self.path) > 2048 or '\\' in self.path or self.path.startswith('//'):
                raise ValueError('invalid_path')
            u = urllib.parse.urlsplit(self.path)
            if u.scheme or u.netloc or '%' in u.path or '..' in u.path:
                raise ValueError('invalid_path')
            site = self.one('Sec-Fetch-Site')
            if site not in {None, 'none', 'same-origin'}:
                # QR scanners can open a top-level document from another site.
                # This exact public shell performs no pairing or private read;
                # its form still needs the one-use token and same-origin POST.
                pair_navigation = (not write and self.command == 'GET'
                    and self.path == '/mobile/pair' and site == 'cross-site'
                    and self.one('Sec-Fetch-Mode') == 'navigate'
                    and self.one('Sec-Fetch-Dest') == 'document')
                if not pair_navigation:
                    raise PermissionError()
            return u

        def token(self, name=COOKIE):
            raw = self.one('Cookie', '')
            if len(raw) > 4096 or raw.count(name + '=') > 1:
                raise PermissionError()
            jar = SimpleCookie()
            try:
                jar.load(raw)
            except Exception:
                raise PermissionError()
            return jar[name].value if name in jar else ''

        def cookies(self, paired):
            device = paired['device']
            suffix = '; Path=/; Secure; HttpOnly; SameSite=Strict'
            renewal = paired.get('renewal_token')
            result = [RENEW_COOKIE + '=' + (renewal or '') + suffix + '; Max-Age=' +
                str(max(0, int(device['renewal_expires_at'] - service.auth.clock())) if renewal else 0)]
            access = COOKIE + '=' + paired['session_token'] + suffix
            if device.get('remembered'):
                access += '; Max-Age=' + str(max(0, int(device['expires_at'] - service.auth.clock())))
            result.append(access)
            return result

        def access_cookie(self, renewed):
            return COOKIE + '=' + renewed['session_token'] + '; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=' + \
                str(max(0, int(renewed['device']['expires_at'] - service.auth.clock())))

        def principal(self, write=False):
            return service.auth.authenticate(self.token(), opts['identity'], csrf=self.one('X-Mobile-CSRF'), write=write)

        def body(self):
            raw = self.one('Content-Length', '0')
            self.one('Content-Type')
            if self.one('Transfer-Encoding') is not None or not re.fullmatch(r'[0-9]{1,6}', raw):
                raise ValueError('invalid_body')
            n = int(raw)
            if not 0 < n <= MAX_BODY or self.headers.get_content_type() != 'application/json':
                raise ValueError('invalid_body')
            data = self.rfile.read(n)
            if len(data) != n:
                raise ValueError('invalid_body')
            def unique(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise ValueError('duplicate_json_key')
                    result[key] = value
                return result
            result = json.loads(data, object_pairs_hook=unique)
            if not isinstance(result, dict):
                raise ValueError('invalid_body')
            return result

        def failure(self, error):
            if isinstance(error, PermissionError):
                return self.respond(403, {'error': 'mobile_forbidden'})
            status = getattr(error, 'status', None)
            code = getattr(error, 'code', None)
            if status == 401 and code == 'mobile_auth_required' and self.path != '/api/mobile/renew':
                try:
                    service.auth.renewal_device(self.token(RENEW_COOKIE), opts['identity'])
                except Exception:
                    pass
                else:
                    code = 'mobile_session_renewal_required'
            if isinstance(status, int) and 400 <= status <= 599 and isinstance(code, str) and re.fullmatch(r'[a-z0-9_]{1,80}', code):
                return self.respond(status, {'error': code})
            if isinstance(error, (ValueError, TypeError, KeyError)):
                return self.respond(400, {'error': 'invalid_request'})
            return self.respond(503, {'error': 'mobile_unavailable'})

        def page(self, name):
            text = (root / 'office' / name).read_text()
            nonce = secrets.token_urlsafe(18)
            if 'name="office-access"' not in text:
                text = text.replace('<head>', '<head><meta name="office-access" content="mobile">', 1)
            if '<base ' not in text:
                text = text.replace('<head>', '<head><base href="/office/">', 1)
            # Import maps are trusted local source and get a per-response nonce.
            text = re.sub(r'<script(?=[\s>])', '<script nonce="' + nonce + '"', text)
            return self.respond(200, text.encode(), content_type='text/html; charset=utf-8', nonce=nonce)

        def do_GET(self):
            try:
                u = self.boundary()
                if not u.path.startswith('/api/mobile/'):
                    service.auth.allow_request(opts['identity'], 'asset')
                if u.path in {'/', '/mobile/', '/mobile/pair'}:
                    if u.query:
                        raise ValueError('unexpected_query')
                    return self.page('mobile-pair.html' if u.path.endswith('/pair') else 'mobile.html')
                if u.path == '/mobile/agent':
                    self.principal()
                    return self.page('mobile-agent.html')
                if u.path.startswith('/api/mobile/'):
                    if u.path == '/api/mobile/session':
                        if u.query:
                            raise ValueError('unexpected_query')
                        data = service.auth.session_status(self.token(), opts['identity'])
                        return self.respond(200, dict(data, authenticated=True))
                    principal = self.principal()
                    service.auth.allow_request(principal['device_id'], 'read')
                    query = urllib.parse.parse_qs(u.query, keep_blank_values=True, strict_parsing=True, max_num_fields=8) if u.query else {}
                    return self.respond(200, service.read(u.path, query, principal))
                from mobile_static_manifest import mobile_static_file
                target = mobile_static_file(root, u.path)
                if not target:
                    return self.respond(404, {'error': 'not_found'})
                return self.respond(200, target.read_bytes(), content_type=mimetypes.guess_type(str(target))[0] or 'application/octet-stream')
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                self.close_connection = True
            except Exception as error:
                self.failure(error)

        def do_POST(self):
            try:
                u = self.boundary(write=True)
                if u.query:
                    raise ValueError('unexpected_query')
                service.auth.allow_request(opts['identity'], 'pair' if u.path == '/api/mobile/pair' else 'write')
                if u.path == '/api/mobile/pair':
                    body = self.body()
                    if 'token' not in body or set(body) - {'token', 'remember_device'}:
                        raise ValueError('invalid_pairing')
                    paired = service.auth.pair(body['token'], opts['identity'], remember_device=body.get('remember_device', False))
                    device = paired['device']
                    return self.respond(200, {'authenticated': True, 'device': device, 'csrf_token': paired['csrf_token']}, cookie=self.cookies(paired))
                if u.path == '/api/mobile/renew':
                    if self.one('X-Mobile-Renew') != '1' or self.body():
                        raise ValueError('invalid_renewal')
                    renewed = service.auth.renew(self.token(RENEW_COOKIE), opts['identity'])
                    return self.respond(200, {'authenticated': True, 'device': renewed['device'],
                        'csrf_token': renewed['csrf_token']}, cookie=self.access_cookie(renewed))
                if u.path == '/api/mobile/remember':
                    body = self.body()
                    if set(body) != {'remember_device'}:
                        raise ValueError('invalid_remember_choice')
                    paired = service.auth.remember(self.token(), opts['identity'], self.one('X-Mobile-CSRF'), body['remember_device'])
                    return self.respond(200, {'authenticated': True, 'device': paired['device'],
                        'csrf_token': paired['csrf_token']}, cookie=self.cookies(paired))
                if u.path == '/api/mobile/logout' and self.one('X-Mobile-Renew') == '1':
                    if self.body():
                        raise ValueError('invalid_logout')
                    result = service.logout_renewal(self.token(RENEW_COOKIE), opts['identity'])
                    return self.respond(200, result, cookie=[name + '=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict'
                        for name in (RENEW_COOKIE, COOKIE)])
                principal = self.principal(write=True)
                body = self.body()
                if u.path == '/api/mobile/commands':
                    return self.respond(202, service.send(body, principal))
                if u.path == '/api/mobile/commands/cancel':
                    return self.respond(200, service.cancel(body, principal))
                if u.path == '/api/mobile/logout':
                    if body:
                        raise ValueError('invalid_logout')
                    result = service.logout(principal)
                    return self.respond(200, result, cookie=[name + '=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict'
                        for name in (RENEW_COOKIE, COOKIE)])
                return self.respond(404, {'error': 'not_found'})
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                self.close_connection = True
            except Exception as error:
                self.failure(error)

        def do_OPTIONS(self):
            self.respond(403, {'error': 'cross_origin_not_supported'})

    return Handler


def create_server(bridge, public_root):
    opts = settings(bridge.config)
    if not opts['enabled']:
        return None
    from security_keys import upgraded_security
    if not upgraded_security(bridge.config):
        raise ValueError('Upgrade separate Office credentials before enabling mobile access')
    return BoundedHTTPServer(('127.0.0.1', opts['port']), handler(bridge.mobile, bridge.config, public_root))
