"""Private phone HTTPS backend. Only Tailscale Serve may front this loopback listener.

This is a separate application boundary, never a proxy carrying an owner key.
No native transports, login APIs, invitations or arbitrary files are forwarded.
"""
from http.cookies import SimpleCookie
import hmac
import http.client
import http.server
import json
import mimetypes
from pathlib import Path
import re
import secrets
import socket
import sys
import threading
import time
import urllib.parse

COOKIE = '__Host-OfficePhone'
RENEW_COOKIE = '__Host-OfficePhoneRenew'
MAX_BODY = 32768
# Browsers require these MIME types for modules and styles when nosniff is set.
WEB_CODE_TYPES = {'.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8'}


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
    allow_reuse_address = sys.platform != 'win32'
    request_queue_size = 16

    def __init__(self, address, handler, *, limit=16, deadline=10):
        if address[0] != '127.0.0.1':
            raise ValueError('Mobile backend must remain loopback')
        self.slots = threading.BoundedSemaphore(limit)
        self.request_deadline = deadline
        super().__init__(address, handler)

    def server_bind(self):
        if sys.platform == 'win32':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def get_request(self):
        connection, address = super().get_request()
        # A pending read on Windows may outlive shutdown() from the timer thread.
        connection.settimeout(min(5, self.request_deadline))
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


def handler(service, config, public_root, prepared_config=None):
    from phone_peer_gate import PeerGate, read_proxy_v2, validate_peer_gate
    bound_config = config if prepared_config is None else prepared_config
    opts = settings(bound_config)
    gate = None
    if 'mobile_peer_gate' in bound_config:
        gate = PeerGate(validate_peer_gate(bound_config['mobile_peer_gate'])['host_id'], opts)
    origin = opts['public_origin']
    host = urllib.parse.urlsplit(origin).hostname
    root = Path(public_root).resolve()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        server_version = 'OfficeMobile'
        sys_version = ''

        def log_message(self, *args):
            pass

        def handle(self):
            self.peer_node = None
            self.proof_only = False
            if gate is not None:
                try:
                    if self.client_address[0] != '127.0.0.1':
                        return
                    if self.rfile.peek(1)[:1] == b'G':
                        # The local installer can ask only for its signed proof.
                        self.proof_only = True
                    else:
                        source, destination = read_proxy_v2(self.rfile, opts['port'])
                        self.peer_node = gate.resolve(source, destination)
                        if self.peer_node is None or not self.config_matches():
                            return
                except (OSError, ValueError, KeyError):
                    return
            super().handle()

        def config_matches(self):
            if not service.ready() or settings(config) != opts:
                return False
            if gate is None:
                return 'mobile_peer_gate' not in config
            current = validate_peer_gate(config.get('mobile_peer_gate'))
            return (current['host_id'] == gate.host_id and
                    (self.proof_only or self.peer_node in current['node_ids']))

        def parse_request(self):
            if self.proof_only and self.raw_requestline not in (
                    b'GET /_office/local/gateway-proof HTTP/1.1\r\n',
                    b'GET /_office/local/gateway-proof HTTP/1.0\r\n'):
                self.close_connection = True
                return False
            original = self.rfile
            self.rfile = HeaderReader(original)
            try:
                return super().parse_request()
            finally:
                self.rfile = original

        def send_error(self, code, message=None, explain=None):
            self.respond(code, {'error': 'invalid_request'})

        def respond(self, code, value, *, content_type='application/json; charset=utf-8', cookie=None, nonce=None, retry_after=None):
            data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.close_connection = True
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Connection', 'close')
            self.send_header('Cache-Control', 'no-store')
            if retry_after is not None:
                self.send_header('Retry-After', str(retry_after))
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
            if not self.config_matches() or self.proof_only or self.client_address[0] != '127.0.0.1':
                raise PermissionError()
            if self.one('Host') not in {host, host + ':443'}:
                raise PermissionError()
            if gate is not None:
                if any(name.lower() in {'forwarded', 'x-real-ip'} or
                       name.lower().startswith(('tailscale-', 'x-forwarded-')) for name in self.headers):
                    raise PermissionError()
            elif self.one('Tailscale-User-Login') != opts['identity']:
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
            return service.auth.authenticate(self.token(), opts['identity'], csrf=self.one('X-Mobile-CSRF'),
                                             write=write, node_id=self.peer_node)

        def body(self):
            raw = self.one('Content-Length', '0')
            self.one('Content-Type')
            if self.one('Transfer-Encoding') is not None or not re.fullmatch(r'[0-9]{1,6}', raw):
                raise ValueError('invalid_body')
            n = int(raw)
            if not 0 < n <= MAX_BODY or self.headers.get_content_type() != 'application/json':
                raise ValueError('invalid_body')
            data = self.rfile.read(n)
            self.body_consumed = True
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

        def pairing_document(self):
            if self.command != 'GET' or self.path != '/mobile/pair':
                return False
            mode = self.headers.get_all('Sec-Fetch-Mode', [])
            dest = self.headers.get_all('Sec-Fetch-Dest', [])
            if mode or dest:
                return mode == ['navigate'] and dest == ['document']
            # Older browsers negotiate HTML without Fetch Metadata.
            accept = self.headers.get('Accept', '').split(',', 1)[0].split(';', 1)[0]
            return accept.strip().lower() in ('text/html', 'application/xhtml+xml')

        def failure(self, error):
            if isinstance(error, PermissionError):
                return self.respond(403, {'error': 'mobile_forbidden'})
            status = getattr(error, 'status', None)
            code = getattr(error, 'code', None)
            if status == 429 and code == 'mobile_rate_limited':
                delay = getattr(error, 'retry_after', None)
                if type(delay) is int and 1 <= delay <= 300:
                    if self.command == 'GET' and self.path in ('/', '/mobile/'):
                        page = ('<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
                                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                                '<title>稍後開啟 OFFICE</title></head><body><main>'
                                '<h1>OFFICE 載入太頻密</h1>'
                                f'<p>請等 {delay} 秒，再開啟手機版 OFFICE。</p>'
                                '<p><a href="/mobile/">重新開啟 OFFICE</a></p></main></body></html>')
                        return self.respond(429, page.encode('utf-8'),
                                            content_type='text/html; charset=utf-8', retry_after=delay)
                    if self.pairing_document():
                        # A link to /mobile/pair would drop an unused QR fragment.
                        # Reload the same tab only on an explicit user click.
                        nonce = secrets.token_urlsafe(18)
                        page = ('<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
                                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                                '<title>稍後繼續配對</title></head><body><main><h1>稍後繼續配對</h1>'
                                f'<p>請等約 {delay} 秒，然後重新載入這個分頁，繼續使用剛掃描的配對碼。</p>'
                                '<button type="button" id="retry" hidden>重新載入配對頁</button>'
                                '</main>'
                                f'<script nonce="{nonce}">const retry=document.getElementById("retry");'
                                'retry.hidden=false;retry.addEventListener("click",()=>location.reload());'
                                '</script></body></html>')
                        return self.respond(429, page.encode('utf-8'), content_type='text/html; charset=utf-8',
                                            nonce=nonce, retry_after=delay)
                    return self.respond(429, {'error': code}, retry_after=delay)
            if status == 401 and code == 'mobile_auth_required' and self.path != '/api/mobile/renew':
                try:
                    service.auth.renewal_device(self.token(RENEW_COOKIE), opts['identity'], node_id=self.peer_node)
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
            text = (root / 'office' / name).read_text(encoding='utf-8')
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
                if self.path == '/_office/local/gateway-proof':
                    # The desktop setup helper verifies the exact loopback
                    # destination before configuring a private HTTPS proxy.
                    # Proxied requests and requests from a browser cannot use it.
                    from mobile_host import local_setup_nonce
                    nonce = local_setup_nonce(self, opts['port'], signed_request=gate is not None)
                    if nonce is None or self.peer_node is not None or not self.config_matches():
                        raise PermissionError()
                    from phone_setup_assistant import gateway_proof, gateway_proof_request
                    if gate is not None:
                        proof = self.one('X-Office-Setup-Request', '')
                        if (not re.fullmatch('[0-9a-f]{64}', proof) or
                                not hmac.compare_digest(proof, gateway_proof_request(config, nonce))):
                            raise PermissionError()
                    return self.respond(200, {'proof': gateway_proof(config, nonce)})
                u = self.boundary()
                if not u.path.startswith('/api/mobile/'):
                    # The managed transport has already verified the individual
                    # device. Older frontends authenticate only the account.
                    subject = 'node:' + self.peer_node if gate is not None else opts['identity']
                    service.auth.allow_request(subject, 'asset')
                if u.path in {'/', '/mobile/', '/mobile/pair'}:
                    if u.query:
                        raise ValueError('unexpected_query')
                    return self.page('mobile-pair.html' if u.path.endswith('/pair') else 'mobile.html')
                if u.path == '/mobile/agent':
                    from mobile_auth import MobileAuthError
                    try:
                        self.principal()
                    except MobileAuthError as error:
                        if error.code != 'mobile_auth_required' or error.status != 401:
                            raise
                        # A restored history tab needs its static shell before it
                        # can renew. The shell contains no history or Agent data.
                        service.auth.renewal_device(self.token(RENEW_COOKIE), opts['identity'], node_id=self.peer_node)
                    return self.page('mobile-agent.html')
                if u.path.startswith('/api/mobile/'):
                    if u.path == '/api/mobile/session':
                        if u.query:
                            raise ValueError('unexpected_query')
                        data = service.auth.session_status(self.token(), opts['identity'], node_id=self.peer_node)
                        return self.respond(200, dict(data, authenticated=True))
                    principal = self.principal()
                    service.auth.allow_request(principal['device_id'], 'read')
                    query = urllib.parse.parse_qs(u.query, keep_blank_values=True, strict_parsing=True, max_num_fields=8) if u.query else {}
                    return self.respond(200, service.read(u.path, query, principal))
                from mobile_static_manifest import mobile_static_file
                target = mobile_static_file(root, u.path)
                if not target:
                    return self.respond(404, {'error': 'not_found'})
                content_type = WEB_CODE_TYPES.get(target.suffix) or mimetypes.guess_type(str(target))[0]
                return self.respond(200, target.read_bytes(), content_type=content_type or 'application/octet-stream')
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                self.close_connection = True
            except Exception as error:
                self.failure(error)

        def do_POST(self):
            self.body_consumed = False
            try:
                u = self.boundary(write=True)
                if u.query:
                    raise ValueError('unexpected_query')
                service.auth.allow_request(opts['identity'], 'pair' if u.path == '/api/mobile/pair' else 'write')
                if u.path == '/api/mobile/pair':
                    body = self.body()
                    if 'token' not in body or set(body) - {'token', 'remember_device'}:
                        raise ValueError('invalid_pairing')
                    paired = service.auth.pair(body['token'], opts['identity'], remember_device=body.get('remember_device', False),
                                               node_id=self.peer_node)
                    device = paired['device']
                    return self.respond(200, {'authenticated': True, 'device': device, 'csrf_token': paired['csrf_token']}, cookie=self.cookies(paired))
                if u.path == '/api/mobile/renew':
                    if self.one('X-Mobile-Renew') != '1' or self.body():
                        raise ValueError('invalid_renewal')
                    renewed = service.auth.renew(self.token(RENEW_COOKIE), opts['identity'], node_id=self.peer_node)
                    return self.respond(200, {'authenticated': True, 'device': renewed['device'],
                        'csrf_token': renewed['csrf_token']}, cookie=self.access_cookie(renewed))
                if u.path == '/api/mobile/remember':
                    body = self.body()
                    if set(body) != {'remember_device'}:
                        raise ValueError('invalid_remember_choice')
                    paired = service.auth.remember(self.token(), opts['identity'], self.one('X-Mobile-CSRF'),
                                                   body['remember_device'], node_id=self.peer_node)
                    return self.respond(200, {'authenticated': True, 'device': paired['device'],
                        'csrf_token': paired['csrf_token']}, cookie=self.cookies(paired))
                if u.path == '/api/mobile/logout' and self.one('X-Mobile-Renew') == '1':
                    if self.body():
                        raise ValueError('invalid_logout')
                    result = service.logout_renewal(self.token(RENEW_COOKIE), opts['identity'], node_id=self.peer_node)
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
                if not self.body_consumed:
                    self.finish_rejected_post()

        def finish_rejected_post(self):
            # A client may still be sending when its session is rejected. Send
            # the complete response first, then briefly drain before closing so
            # Windows does not reset the connection and discard the response.
            end = time.monotonic() + 0.25
            remaining = MAX_BODY
            try:
                self.connection.shutdown(socket.SHUT_WR)
                while remaining:
                    timeout = end - time.monotonic()
                    if timeout <= 0:
                        break
                    self.connection.settimeout(timeout)
                    chunk = self.rfile.read1(min(8192, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except OSError:
                pass

        def do_OPTIONS(self):
            self.respond(403, {'error': 'cross_origin_not_supported'})

    return Handler


def create_server(bridge, public_root, prepared_config=None):
    target = bridge.config if prepared_config is None else prepared_config
    opts = settings(target)
    if not opts['enabled']:
        return None
    from security_keys import upgraded_security
    if not upgraded_security(target):
        raise ValueError('Upgrade separate Office credentials before enabling mobile access')
    return BoundedHTTPServer(('127.0.0.1', opts['port']), handler(bridge.mobile, bridge.config, public_root, target))
