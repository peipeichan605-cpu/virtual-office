"""Real loopback HTTP tests using isolated fake native agents and temporary data.

These exercise the phone boundary, not Tailscale network reachability. No real
Office configuration, native app, credential file, or external service is used.
"""
import http.client
from http.cookies import SimpleCookie
import json
from pathlib import Path
import re
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from mobile_gateway import BoundedHTTPServer, COOKIE, MAX_BODY, handler, settings
from mobile_service import MobileService
from test_mobile_service import A, B, CHILD, HIDDEN_CHILD, IDENTITY, Bridge


ROOT = Path(__file__).resolve().parent
ORIGIN = 'https://office.example.ts.net'
HOST = 'office.example.ts.net'


class MobileGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bridge = Bridge(Path(self.temp.name))
        self.service = MobileService(self.bridge)
        # Rate-limit policy has its own tests. HTTP cases must be isolated from
        # batching effects while still executing the real rate-limit function.
        self.service.auth.RATE_LIMITS = {kind: (10000, window) for kind, (_, window)
                                         in self.service.auth.RATE_LIMITS.items()}
        self.service.auth.GLOBAL_LIMITS = {kind: 20000 for kind in self.service.auth.GLOBAL_LIMITS}
        self.server = BoundedHTTPServer(('127.0.0.1', 0), handler(self.service, self.bridge.config, ROOT/'public'))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.cookie = None
        self.csrf = None
        self.pairing_token, self.paired, self.pair_headers = self.pair()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.service.close()
        self.bridge.commands.close()
        self.bridge.history.close()
        self.temp.cleanup()

    def request(self, path, *, method='GET', data=None, headers=None, extra_headers=(), raw=None):
        fields = {'Host': HOST, 'Tailscale-User-Login': IDENTITY}
        if self.cookie:
            fields['Cookie'] = self.cookie
        if method == 'POST':
            fields.update({'Origin': ORIGIN, 'Content-Type': 'application/json'})
            if self.csrf:
                fields['X-Mobile-CSRF'] = self.csrf
        fields.update(headers or {})
        body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
        if body is not None and 'Content-Length' not in fields:
            fields['Content-Length'] = str(len(body))
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for key, value in fields.items():
                if value is not None:
                    conn.putheader(key, value)
            for key, value in extra_headers:
                conn.putheader(key, value)
            conn.endheaders(body)
            response = conn.getresponse()
            headers = dict(response.getheaders())
            headers['Set-Cookie-All'] = [value for key, value in response.getheaders() if key.lower() == 'set-cookie']
            return response.status, headers, response.read()
        finally:
            conn.close()

    def json(self, path, **kwargs):
        status, headers, body = self.request(path, **kwargs)
        return status, headers, json.loads(body)

    def pair(self, view=None, send=None):
        invitation = self.service.owner_create_pairing({'identity': IDENTITY, 'label': 'HTTP fixture phone',
                                                       'view_agents': [A] if view is None else view,
                                                       'send_agents': [A] if send is None else send})
        status, headers, paired = self.json('/api/mobile/pair', method='POST', data={'token': invitation['pairing_token']})
        self.assertEqual(status, 200, paired)
        jar = SimpleCookie()
        jar.load(headers['Set-Cookie'])
        self.cookie = COOKIE + '=' + jar[COOKIE].value
        self.csrf = paired['csrf_token']
        return invitation['pairing_token'], paired, headers

    def command(self, agent=A, text='phone fixture instruction'):
        return {'request_id': str(uuid.uuid4()), 'agent_id': agent, 'text': text}

    def test_desktop_assistant_identifies_its_local_gateway_before_setup(self):
        from phone_setup_assistant import gateway_proof
        nonce = '7a' * 32
        port = settings(self.bridge.config)['port']
        headers = {'Host': '127.0.0.1:' + str(port), 'Cookie': None,
                   'Tailscale-User-Login': None, 'X-Office-Setup-Nonce': nonce}
        status, _, body = self.json('/_office/local/gateway-proof', headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(body, {'proof': gateway_proof(self.bridge.config, nonce)})
        for change in ({'Origin': ORIGIN}, {'Host': HOST}, {'Tailscale-User-Login': IDENTITY},
                       {'X-Forwarded-Host': HOST}, {'X-Office-Setup-Nonce': 'invalid'}):
            with self.subTest(headers=change):
                status, _, body = self.json('/_office/local/gateway-proof', headers={**headers, **change})
                self.assertEqual(status, 403)
                self.assertNotIn('proof', body)

    def test_pair_issues_only_host_secure_http_only_strict_cookie(self):
        cookie = self.pair_headers['Set-Cookie']
        self.assertTrue(cookie.startswith(COOKIE + '='))
        for flag in ('; Path=/', '; Secure', '; HttpOnly', '; SameSite=Strict'):
            self.assertIn(flag, cookie)
        self.assertNotIn('Domain=', cookie)
        self.assertNotIn('session_token', self.paired)
        self.assertNotIn(self.cookie.split('=', 1)[1], json.dumps(self.paired))
        self.assertEqual(self.paired['device']['view_agents'], [A])
        self.assertEqual(self.paired['device']['send_agents'], [A])

    def test_pair_token_is_one_use_and_body_cannot_expand_scopes(self):
        status, _, body = self.json('/api/mobile/pair', method='POST', data={'token': self.pairing_token})
        self.assertEqual(status, 401, body)
        status, _, body = self.json('/api/mobile/pair', method='POST', data={'token': self.pairing_token, 'send_agents': [B]})
        self.assertEqual(status, 400, body)

    def test_exact_host_and_serve_identity_required_even_for_shell(self):
        for headers in ({'Host': None}, {'Host': 'evil.example'}, {'Host': HOST+'.evil'},
                        {'Host': HOST+':4319'}, {'Tailscale-User-Login': None},
                        {'Tailscale-User-Login': 'other@example.test'}, {'Tailscale-User-Login': IDENTITY.upper()}):
            with self.subTest(headers=headers):
                self.assertEqual(self.json('/mobile/', headers=headers)[0], 403)
        self.assertEqual(self.request('/mobile/', headers={'Host': HOST+':443'})[0], 200)

    def test_spoofed_forwarding_headers_cannot_replace_serve_identity(self):
        status, _, _ = self.json('/api/mobile/session', headers={'Tailscale-User-Login': None,
                                   'X-Forwarded-Host': HOST, 'X-Forwarded-User': IDENTITY,
                                   'X-Tailscale-User-Login': IDENTITY})
        self.assertEqual(status, 403)

    def test_origin_and_fetch_site_fail_closed(self):
        for headers in ({'Origin': 'https://evil.example'}, {'Origin': 'null'}, {'Origin': ORIGIN+'/'},
                        {'Sec-Fetch-Site': 'cross-site'}, {'Sec-Fetch-Site': 'same-site'}):
            with self.subTest(headers=headers):
                self.assertEqual(self.json('/api/mobile/health', headers=headers)[0], 403)
        for origin in (None, 'null', 'https://evil.example'):
            with self.subTest(post_origin=origin):
                self.assertEqual(self.json('/api/mobile/commands', method='POST', data=self.command(),
                                           headers={'Origin': origin})[0], 403)
        self.assertEqual(self.json('/api/mobile/health', headers={'Origin': ORIGIN, 'Sec-Fetch-Site': 'same-origin'})[0], 200)

    def external_pair_navigation(self, **changes):
        return {'Cookie': None, 'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Dest': 'document', **changes}

    def test_external_qr_top_level_navigation_loads_only_public_pair_shell(self):
        before = self.service.auth.list_devices()
        for activation in (None, '?1'):
            with self.subTest(sec_fetch_user=activation):
                status, headers, raw = self.request('/mobile/pair', headers=self.external_pair_navigation(**{'Sec-Fetch-User': activation}))
                self.assertEqual(status, 200)
                self.assertIn('text/html', headers['Content-Type'])
                self.assertIn('id="mobile-pair-form"', raw.decode())
                self.assertNotIn('Set-Cookie', headers)
                self.assertEqual(headers['X-Frame-Options'], 'DENY')
                self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
                self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
                for value in (self.pairing_token, self.cookie, self.csrf, self.bridge.config['owner_key'], A):
                    self.assertNotIn(value.encode(), raw)
        self.assertEqual(self.service.auth.list_devices(), before)

    def test_external_pair_navigation_still_requires_exact_host_identity_and_origin(self):
        for changes in ({'Host': None}, {'Host': 'evil.example'}, {'Tailscale-User-Login': None},
                        {'Tailscale-User-Login': 'intruder@example.test'}, {'Origin': 'https://evil.example'},
                        {'Origin': 'null'}):
            with self.subTest(changes=changes):
                status = self.json('/mobile/pair', headers=self.external_pair_navigation(**changes))[0]
                self.assertEqual(status, 403)

    def test_external_pair_exception_rejects_fetch_iframe_and_incomplete_metadata(self):
        for changes in ({'Sec-Fetch-Mode': 'cors'}, {'Sec-Fetch-Mode': 'no-cors'},
                        {'Sec-Fetch-Mode': 'same-origin'}, {'Sec-Fetch-Mode': None},
                        {'Sec-Fetch-Dest': 'iframe'}, {'Sec-Fetch-Dest': 'frame'},
                        {'Sec-Fetch-Dest': 'empty'}, {'Sec-Fetch-Dest': ''}, {'Sec-Fetch-Dest': None}):
            with self.subTest(changes=changes):
                status = self.json('/mobile/pair', headers=self.external_pair_navigation(**changes))[0]
                self.assertEqual(status, 403)

    def test_external_pair_exception_does_not_allow_other_routes_or_cross_site_post(self):
        for path in ('/', '/mobile/', '/mobile/agent?agent='+A, '/api/mobile/session', '/api/mobile/work',
                     '/api/mobile/health', '/office/mobile-pair.js', '/api/config', '/api/mobile/pair'):
            with self.subTest(path=path):
                status = self.json(path, headers=self.external_pair_navigation())[0]
                self.assertEqual(status, 403)
        # Even a valid cookie, CSRF value and matching Origin cannot turn the
        # public GET-navigation exception into permission for a cross-site POST.
        for path, data in (('/api/mobile/pair', {'token': self.pairing_token}),
                           ('/api/mobile/commands', self.command()), ('/mobile/pair', {})):
            with self.subTest(post=path):
                status = self.json(path, method='POST', data=data,
                                   headers=self.external_pair_navigation(Cookie=self.cookie, Origin=ORIGIN))[0]
                self.assertEqual(status, 403)
        self.assertEqual(self.bridge.adapter.calls, [])

    def test_external_pair_exception_rejects_duplicate_metadata_and_path_aliases(self):
        for name, value in (('Sec-Fetch-Mode', 'navigate'), ('Sec-Fetch-Dest', 'document')):
            with self.subTest(header=name):
                status = self.json('/mobile/pair', headers=self.external_pair_navigation(),
                                   extra_headers=[(name, value)])[0]
                self.assertEqual(status, 400)
        for path in ('/mobile/pair?token=not-allowed', '/mobile/pair?x=1', '/mobile/%70air',
                     '/mobile/./pair', '/mobile/../mobile/pair', '/mobile\\pair',
                     'https://office.example.ts.net/mobile/pair'):
            with self.subTest(path=path):
                self.assertIn(self.json(path, headers=self.external_pair_navigation())[0], (400, 403))

    def test_external_pair_get_leaves_pending_token_for_same_origin_post(self):
        invitation = self.service.owner_create_pairing({'identity': IDENTITY, 'label': 'QR fixture phone',
                                                       'view_agents': [A], 'send_agents': [A]})
        token = invitation['pairing_token']
        count = len(self.service.auth.list_devices())
        status, headers, raw = self.request('/mobile/pair', headers=self.external_pair_navigation())
        self.assertEqual(status, 200)
        self.assertNotIn('Set-Cookie', headers)
        self.assertNotIn(token.encode(), raw)
        self.assertEqual(len(self.service.auth.list_devices()), count)
        status, _, body = self.json('/api/mobile/pair', method='POST', data={'token': token},
                                   headers={'Cookie': None, 'Sec-Fetch-Site': 'same-origin'})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['authenticated'])
        self.assertEqual(len(self.service.auth.list_devices()), count+1)
        self.assertEqual(self.json('/api/mobile/pair', method='POST', data={'token': token})[0], 401)

    def test_preflight_denies_without_cors_headers(self):
        status, headers, _ = self.json('/api/mobile/commands', method='OPTIONS', headers={'Origin': 'https://evil.example'})
        self.assertEqual(status, 403)
        self.assertFalse(any(key.lower().startswith('access-control-') for key in headers))

    def test_session_restores_csrf_after_reload_without_exposing_cookie(self):
        status, headers, body = self.json('/api/mobile/session')
        self.assertEqual(status, 200)
        self.assertTrue(body['authenticated'])
        self.assertEqual(body['csrf_token'], self.csrf)
        self.assertEqual(body['device']['device_id'], self.paired['device']['device_id'])
        self.assertNotIn(self.cookie.split('=', 1)[1], json.dumps(body))
        self.assertNotIn('Set-Cookie', headers)
        self.assertEqual(self.json('/api/mobile/health')[2], {'ok': True, 'local_ok': True, 'sync_ok': True})

    def test_data_and_agent_page_require_session_cookie(self):
        for path in ('/api/mobile/session', '/api/mobile/health', '/api/mobile/work', '/mobile/agent?agent='+A):
            with self.subTest(path=path):
                self.assertEqual(self.json(path, headers={'Cookie': None})[0], 401)
        self.assertEqual(self.request('/mobile/pair', headers={'Cookie': None})[0], 200)

    def test_csrf_required_and_must_belong_to_current_cookie(self):
        for csrf in (None, '', '0'*64):
            with self.subTest(csrf=csrf):
                status, _, body = self.json('/api/mobile/commands', method='POST', data=self.command(),
                                           headers={'X-Mobile-CSRF': csrf})
                self.assertEqual(status, 403, body)
        old_csrf = self.csrf
        self.pair()
        self.assertEqual(self.json('/api/mobile/commands', method='POST', data=self.command(),
                                   headers={'X-Mobile-CSRF': old_csrf})[0], 403)
        self.assertEqual(self.bridge.adapter.calls, [])

    def test_work_projection_restricts_agents_children_and_private_fields(self):
        status, _, body = self.json('/api/mobile/work')
        self.assertEqual(status, 200)
        self.assertEqual({row['id'] for row in body['agents']}, {A, CHILD})
        text = json.dumps(body)
        for value in (B, HIDDEN_CHILD, '/private/', 'hidden-token', self.bridge.config['topic'],
                      self.bridge.config['owner_key'], self.bridge.config['internal_hmac_key'],
                      'native_url', 'source_id', 'seating_revision'):
            self.assertNotIn(value, text)

    def test_cross_agent_history_messages_commands_and_run_are_hidden(self):
        hidden_run = self.bridge.history.list(self.bridge.history_agents(), agent=B)[0]['id']
        for path in ('/api/mobile/history?agent='+B, '/api/mobile/messages?agent='+HIDDEN_CHILD,
                     '/api/mobile/commands?agent='+B, '/api/mobile/run?id='+hidden_run):
            with self.subTest(path=path):
                self.assertEqual(self.json(path)[0], 404)
        for agent in (B, CHILD):
            with self.subTest(send_agent=agent):
                self.assertEqual(self.json('/api/mobile/commands', method='POST', data=self.command(agent))[0], 403)
        self.assertEqual(self.bridge.adapter.calls, [])

    def test_view_only_device_has_no_send_or_approval_authority(self):
        self.pair(view=[A], send=[])
        status, _, body = self.json('/api/mobile/commands?agent='+A)
        self.assertEqual(status, 200)
        self.assertFalse(body['capability']['can_send'])
        self.assertEqual(body['approvals'], [])
        self.assertEqual(self.json('/api/mobile/commands', method='POST', data=self.command())[0], 403)

    def test_send_receive_delivery_history_roundtrip_uses_fake_native_only(self):
        data = self.command()
        status, _, sent = self.json('/api/mobile/commands', method='POST', data=data)
        self.assertEqual(status, 202)
        self.assertEqual(set(sent), {'command', 'duplicate'})
        self.assertFalse(sent['duplicate'])
        self.assertEqual(sent['command']['state'], 'pending')
        self.assertEqual(self.json('/api/mobile/commands', method='POST', data=data)[2]['duplicate'], True)
        self.bridge.commands.tick()
        self.assertEqual(len(self.bridge.adapter.calls), 1)
        native_text = self.bridge.adapter.calls[0][1]
        self.assertIn(data['text'], native_text)
        self.bridge.commands.observe_prompt(A, 'mobile-http-run', native_text)
        run_id = self.bridge.history.record(A, 'codex', 'Visible', 'mobile-http-run', 'done',
                                           prompt=data['text'], delivery='phone fixture delivery')
        self.bridge.history.message(A, 'mobile-http-run', 'assistant', 'final', 'phone fixture delivery')
        self.bridge.commands.observe_run(A, 'mobile-http-run', 'done')
        rows = self.json('/api/mobile/commands?agent='+A)[2]['commands']
        self.assertEqual(next(row for row in rows if row['id'] == sent['command']['id'])['state'], 'completed')
        messages = self.json('/api/mobile/messages?agent='+A)[2]['messages']
        self.assertIn('phone fixture delivery', [row['text'] for row in messages])
        runs = self.json('/api/mobile/history?agent='+A)[2]['runs']
        run = next(row for row in runs if row['id'] == run_id)
        self.assertEqual(self.json('/api/mobile/run?id='+run['id'])[2]['delivery'], 'phone fixture delivery')

    def test_revoke_blocks_existing_cookie_and_cancels_unsent_command(self):
        sent = self.json('/api/mobile/commands', method='POST', data=self.command())[2]
        result = self.service.owner_revoke(self.paired['device']['device_id'])
        self.assertEqual(result['cancelled'], 1)
        self.bridge.commands.tick()
        self.assertEqual(self.bridge.adapter.calls, [])
        self.assertEqual(self.json('/api/mobile/session')[0], 401)
        self.assertEqual(self.json('/api/mobile/commands', method='POST', data=self.command())[0], 401)
        row = self.bridge.commands.db.execute('SELECT state FROM commands WHERE id=?', (sent['command']['id'],)).fetchone()
        self.assertEqual(row['state'], 'cancelled')

    def test_logout_clears_cookie_revokes_session_and_cancels_pending(self):
        self.json('/api/mobile/commands', method='POST', data=self.command())
        status, headers, body = self.json('/api/mobile/logout', method='POST', data={})
        self.assertEqual(status, 200, body)
        for flag in ('Max-Age=0', 'Secure', 'HttpOnly', 'SameSite=Strict'):
            self.assertIn(flag, headers['Set-Cookie'])
        self.bridge.commands.tick()
        self.assertEqual(self.bridge.adapter.calls, [])
        self.assertEqual(self.json('/api/mobile/session')[0], 401)

    def test_other_device_cannot_cancel_queued_command(self):
        sent = self.json('/api/mobile/commands', method='POST', data=self.command())[2]
        self.pair()
        data = {'agent_id': A, 'command_id': sent['command']['id']}
        self.assertEqual(self.json('/api/mobile/commands/cancel', method='POST', data=data)[0], 404)
        rows = self.json('/api/mobile/commands?agent='+A)[2]['commands']
        self.assertFalse(rows[0]['can_cancel'])

    def test_admin_owner_native_and_legacy_surfaces_are_not_forwarded(self):
        for path in ('/api/health', '/api/work', '/api/config', '/api/agents', '/api/commands',
                     '/api/mobile/devices', '/api/mobile/revoke', '/api/mobile/invitations',
                     '/api/native/codex', '/api/grok-sdk/login', '/api/cursor-cloud/login',
                     '/office/', '/office/index.html', '/office/agent.html', '/office/face/index.html',
                     '/office/ui/harness.html', '/office/mobile-owner.js', '/office/invite.js',
                     '/assets/vendor/three/PROVENANCE.json', '/config.json', '/.git/config'):
            with self.subTest(path=path):
                self.assertEqual(self.json(path)[0], 404)
        for path in ('/api/mobile/agents/logout', '/api/mobile/approve', '/api/mobile/pairing', '/api/config',
                     '/api/mobile/scopes', '/api/office/mobile/scopes'):
            with self.subTest(post=path):
                self.assertEqual(self.json(path, method='POST', data={})[0], 404)

    def test_rate_limited_shell_and_module_probe_give_the_same_remaining_wait(self):
        auth = self.service.auth
        auth.RATE_LIMITS = {**auth.RATE_LIMITS, 'asset': (1, 60)}
        now = auth.clock()
        with patch.object(auth, 'clock', return_value=now) as clock:
            self.assertEqual(self.request('/mobile/')[0], 200)
            clock.return_value = now + 12.1
            for path in ('/', '/mobile/'):
                status, headers, body = self.request(path)
                self.assertEqual(status, 429)
                self.assertEqual(headers['Retry-After'], '48')
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertIn('text/html', headers['Content-Type'])
                self.assertNotIn('Set-Cookie', headers)
                html = body.decode('utf-8')
                self.assertIn('48 秒', html)
                self.assertEqual(re.findall(r'href="([^"]+)"', html), ['/mobile/'])
                self.assertNotIn('<script', html)
                self.assertNotIn(IDENTITY, html)
            for path in ('/office/mobile.css', '/mobile/pair', '/mobile/?unexpected=1'):
                status, headers, body = self.json(path)
                self.assertEqual((status, headers['Retry-After']), (429, '48'))
                self.assertEqual(body, {'error': 'mobile_rate_limited'})
            self.assertEqual(self.request('/api/mobile/session')[0], 200)
            clock.return_value = now + 60
            status, headers, _ = self.request('/mobile/')
            self.assertEqual(status, 200)
            self.assertNotIn('Retry-After', headers)

    def test_rate_limited_pairing_navigation_keeps_same_tab_for_manual_retry(self):
        auth = self.service.auth
        auth.RATE_LIMITS = {**auth.RATE_LIMITS, 'asset': (1, 60)}
        with patch.object(auth, 'clock', return_value=auth.clock()):
            self.assertEqual(self.request('/mobile/pair')[0], 200)
            for fields in (self.external_pair_navigation(), {'Accept': 'text/html,application/xhtml+xml'}):
                status, headers, raw = self.request('/mobile/pair', headers=fields)
                html = raw.decode('utf-8')
                self.assertEqual(status, 429)
                self.assertEqual(headers['Retry-After'], '60')
                self.assertIn('text/html', headers['Content-Type'])
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertNotIn('Set-Cookie', headers)
                self.assertIn('60 秒', html)
                self.assertIn('location.reload()', html)
                self.assertNotIn('href=', html)
                self.assertNotIn('src=', html)
                self.assertNotIn(self.pairing_token, html)
                nonce = re.search(r"'nonce-([^']+)'", headers['Content-Security-Policy']).group(1)
                self.assertIn(f'<script nonce="{nonce}">', html)
            fetch = {'Accept': 'text/html', 'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Dest': 'empty'}
            status, headers, body = self.json('/mobile/pair', headers=fetch)
            self.assertEqual((status, body['error']), (429, 'mobile_rate_limited'))
            self.assertIn('application/json', headers['Content-Type'])

    def test_shell_has_fresh_nonce_local_assets_and_security_policy(self):
        for path in ('/mobile/', '/mobile/pair', '/mobile/agent?agent='+A):
            with self.subTest(path=path):
                status, headers, raw = self.request(path)
                self.assertEqual(status, 200)
                html = raw.decode()
                self.assertIn('name="office-access" content="mobile"', html)
                self.assertIn('<base href="/office/">', html)
                nonce = re.search(r"'nonce-([^']+)'", headers['Content-Security-Policy']).group(1)
                scripts = re.findall(r'<script\b[^>]*>', html)
                self.assertTrue(scripts)
                self.assertTrue(all('nonce="'+nonce+'"' in script for script in scripts))
                self.assertNotIn("'unsafe-inline'", headers['Content-Security-Policy'].split(';')[1])
                self.assertIn("connect-src 'self' blob:;", headers['Content-Security-Policy'])
                download_links = {'https://play.google.com/store/apps/details?id=com.tailscale.ipn',
                                  'https://apps.apple.com/app/tailscale/id1470499037'}
                for tag, attrs in re.findall(r'<([a-z]+)\b([^>]*)>', html):
                    for attribute in re.findall(r'(?:src|href)="([^"#]+)"', attrs):
                        if tag == 'a' and attribute in download_links:
                            self.assertIn('rel="noopener noreferrer"', attrs)
                            continue
                        self.assertTrue(attribute.startswith('/'), attribute)
                        if attribute != '/office/':
                            self.assertEqual(self.request(attribute)[0], 200, attribute)
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
                self.assertEqual(headers['X-Frame-Options'], 'DENY')
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
                self.assertIn('max-age=', headers['Strict-Transport-Security'])
                next_headers = self.request(path)[1]
                self.assertNotEqual(headers['Content-Security-Policy'], next_headers['Content-Security-Policy'])

    def test_mobile_pages_and_imports_are_independent_of_host_text_defaults(self):
        native_read = Path.read_text

        def legacy_text_default(path, encoding=None, **kwargs):
            return native_read(path, encoding=encoding or 'cp1252', **kwargs)

        with patch.object(Path, 'read_text', legacy_text_default):
            for url in ('/mobile/', '/mobile/pair', '/mobile/agent?agent=' + A):
                with self.subTest(url=url):
                    status, _, data = self.request(url)
                    self.assertEqual(status, 200)
                    self.assertIn('lang="zh-Hant"', data.decode('utf-8'))

        with patch('mobile_gateway.mimetypes.guess_type', return_value=('text/plain', None)):
            for url, expected in (('/office/mobile-entry.js', 'text/javascript'),
                                  ('/assets/vendor/three/build/three.module.js', 'text/javascript'),
                                  ('/office/mobile.css', 'text/css')):
                with self.subTest(url=url):
                    status, headers, _ = self.request(url)
                    self.assertEqual(status, 200)
                    self.assertEqual(headers['Content-Type'].split(';')[0], expected)
                    self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')

    def test_disabling_or_reconfiguring_gateway_fails_closed_including_session(self):
        original = dict(self.bridge.config['mobile'])
        for update in ({'enabled': False}, {'identity': 'replacement@example.test'},
                       {'public_origin': 'https://replacement.example.ts.net'}):
            self.bridge.config['mobile'] = dict(original, **update)
            for path in ('/mobile/', '/api/mobile/session', '/api/mobile/work'):
                with self.subTest(update=update, path=path):
                    self.assertIn(self.json(path)[0], (403, 503))
            status = self.json('/api/mobile/pair', method='POST', data={'token': self.pairing_token})[0]
            self.assertIn(status, (403, 503))
        self.bridge.config['mobile'] = original

    def test_removed_credential_separation_fails_closed_for_every_gateway_route(self):
        self.bridge.config.pop('owner_key')
        for path in ('/mobile/', '/api/mobile/session', '/api/mobile/health', '/office/app.js'):
            with self.subTest(path=path):
                self.assertIn(self.json(path)[0], (403, 503))

    def test_duplicate_security_and_framing_headers_are_rejected(self):
        for name, value in (('Host', HOST), ('Tailscale-User-Login', IDENTITY), ('Cookie', self.cookie),
                            ('Origin', ORIGIN), ('Sec-Fetch-Site', 'same-origin'), ('X-Mobile-CSRF', self.csrf),
                            ('Content-Length', '2'), ('Content-Type', 'application/json')):
            with self.subTest(header=name):
                fields = {'Sec-Fetch-Site': 'same-origin'}
                status, _, body = self.json('/api/mobile/commands', method='POST', data=self.command(),
                                           headers=fields, extra_headers=[(name, value)])
                self.assertEqual(status, 400, body)
        self.assertEqual(self.bridge.adapter.calls, [])

    def test_duplicate_cookie_names_and_oversized_cookie_are_rejected(self):
        for cookie in (self.cookie+'; '+self.cookie, COOKIE+'='+'x'*4100):
            with self.subTest(size=len(cookie)):
                self.assertEqual(self.json('/api/mobile/session', headers={'Cookie': cookie})[0], 403)

    def test_malformed_duplicate_and_oversized_json_body_are_rejected(self):
        bodies = [b'{', b'[]', b'null', b'{"token":"a","token":"b"}',
                  b'{"token":{"duplicate":1,"duplicate":2}}', b'{"token":"'+b'x'*MAX_BODY+b'"}']
        for body in bodies:
            with self.subTest(size=len(body)):
                status, _, result = self.json('/api/mobile/pair', method='POST', raw=body)
                self.assertEqual(status, 400, result)
        for fields in ({'Content-Length': '-1'}, {'Content-Length': '999999'},
                       {'Transfer-Encoding': 'chunked'}, {'Content-Type': 'text/plain'}, {'Content-Length': '0'}):
            with self.subTest(headers=fields):
                self.assertEqual(self.json('/api/mobile/pair', method='POST', raw=b'{}', headers=fields)[0], 400)

    def test_oversized_line_and_aggregate_headers_are_rejected(self):
        self.assertEqual(self.json('/api/mobile/session', extra_headers=[('X-Large', 'x'*8193)])[0], 431)
        self.assertEqual(self.json('/api/mobile/session', extra_headers=[('X-Fill-'+str(i), 'x'*8000) for i in range(5)])[0], 431)

    def test_noncanonical_paths_and_bad_queries_are_rejected(self):
        for path in ('/api/mobile/../work', '/api/mobile/%77ork', '/api/mobile\\work',
                     'https://office.example.ts.net/api/mobile/work', '/api/mobile/work?x=1',
                     '/api/mobile/session?x=1', '/mobile/?x=1',
                     '/api/mobile/messages?agent='+A+'&agent='+B,
                     '/api/mobile/messages?agent='+A+'&after=-1', '/'+('x'*2048)):
            with self.subTest(path=path[:100]):
                self.assertEqual(self.json(path)[0], 400)

    def test_asset_and_api_read_limits_use_separate_buckets(self):
        self.service.auth._rates.clear()
        self.service.auth.RATE_LIMITS['asset'] = (1, 60)
        self.service.auth.RATE_LIMITS['read'] = (1, 60)
        self.assertEqual(self.request('/office/mobile.css')[0], 200)
        self.assertEqual(self.request('/office/mobile.css')[0], 429)
        self.assertEqual(self.json('/api/mobile/health')[0], 200)
        status, headers, result = self.json('/api/mobile/health')
        self.assertEqual(status, 429)
        self.assertEqual(result, {'error': 'mobile_rate_limited'})
        self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_device_read_limits_do_not_exhaust_other_devices(self):
        self.service.auth._rates.clear()
        self.service.auth.RATE_LIMITS['read'] = (1, 60)
        self.assertEqual(self.json('/api/mobile/health')[0], 200)
        self.assertEqual(self.json('/api/mobile/health')[0], 429)
        self.pair()
        self.assertEqual(self.json('/api/mobile/health')[0], 200)

    def test_api_errors_do_not_expose_owner_credentials_or_native_trace(self):
        def broken_snapshot():
            raise RuntimeError(self.bridge.config['owner_key'] + ' /private/native-session traceback')
        self.bridge.work_snapshot = broken_snapshot
        status, _, result = self.json('/api/mobile/work')
        self.assertEqual(status, 503)
        self.assertEqual(result, {'error': 'mobile_unavailable'})

    def slow_drip_deadline(self, stage):
        """A byte every 25ms must not extend the absolute 200ms deadline."""
        original_server = self.server
        server = BoundedHTTPServer(('127.0.0.1', 0),
                                   handler(self.service, self.bridge.config, ROOT/'public'),
                                   limit=1, deadline=0.2)
        worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        worker.start()
        self.server = server
        stop = threading.Event()
        writer = None
        connection = None
        try:
            connection = socket.create_connection(('127.0.0.1', server.server_port), timeout=1)
            if stage == 'headers':
                prefix = f'POST /api/mobile/commands HTTP/1.1\r\nHost: {HOST}\r\nX-Slow: '
            else:
                prefix = (f'POST /api/mobile/commands HTTP/1.1\r\nHost: {HOST}\r\n'
                          f'Tailscale-User-Login: {IDENTITY}\r\nOrigin: {ORIGIN}\r\n'
                          f'Cookie: {self.cookie}\r\nX-Mobile-CSRF: {self.csrf}\r\n'
                          'Content-Type: application/json\r\nContent-Length: 1024\r\n\r\n{')
            began = time.monotonic()
            connection.sendall(prefix.encode())
            writes = []

            def drip():
                while not stop.wait(0.025):
                    try:
                        connection.sendall(b' ')
                        writes.append(time.monotonic())
                    except OSError:
                        break

            writer = threading.Thread(target=drip, daemon=True)
            writer.start()
            # A broken deadline implementation times out here after 1s instead
            # of leaving an unbounded slow-drip process behind in the suite.
            try:
                while connection.recv(4096):
                    pass
            except ConnectionResetError:
                # Closing a socket with unread drip bytes may send RST on
                # macOS. It is still a closed connection; keep the deadline
                # and admission-slot assertions below unchanged.
                pass
            elapsed = time.monotonic() - began
            self.assertGreaterEqual(len(writes), 3, 'Request was rejected before slow-drip behavior was exercised')
            self.assertGreater(elapsed, 0.12)
            self.assertLess(elapsed, 0.8, 'Continuous input must not reset the absolute deadline')
            stop.set()
            writer.join(timeout=1)
            self.assertFalse(writer.is_alive())
            # Reclaiming the single admission slot is essential: closing a
            # socket alone must not leave the gateway permanently saturated.
            acquired = server.slots.acquire(timeout=0.5)
            self.assertTrue(acquired, 'Timed-out worker did not release its admission slot')
            if acquired:
                server.slots.release()
            # The next request verifies normal service after the previous
            # worker exited. Give its DB commit the ordinary production budget.
            server.request_deadline = original_server.request_deadline
            status, _, sent = self.json('/api/mobile/commands', method='POST', data=self.command())
            self.assertEqual(status, 202, sent)
            self.assertEqual(sent['command']['state'], 'pending')
            self.assertEqual(self.bridge.adapter.calls, [])
        finally:
            stop.set()
            if connection:
                connection.close()
            if writer:
                writer.join(timeout=1)
            self.server = original_server
            server.shutdown()
            server.server_close()
            worker.join(timeout=1)

    def test_slow_header_drip_hits_deadline_releases_slot_and_accepts_next_post(self):
        self.slow_drip_deadline('headers')

    def test_slow_body_drip_hits_deadline_releases_slot_and_accepts_next_post(self):
        self.slow_drip_deadline('body')


class MobileGatewayConfigurationTests(unittest.TestCase):
    def test_server_cannot_bind_non_loopback(self):
        for address in ('0.0.0.0', '::', '100.64.0.1'):
            with self.subTest(address=address), self.assertRaises(ValueError):
                BoundedHTTPServer((address, 0), http.client.HTTPConnection)

    def test_origin_and_port_configuration_are_strict(self):
        config = {'port': 4318, 'mobile': {'enabled': True, 'identity': IDENTITY, 'public_origin': ORIGIN}}
        self.assertEqual(settings(config)['port'], 4319)
        for origin in ('http://office.example.ts.net', ORIGIN+'/', ORIGIN+':443', ORIGIN+'?x=1',
                       ORIGIN+'#x', 'https://office.example.ts.net.evil', 'https://user@office.example.ts.net',
                       'https://office..example.ts.net', 'https://127.0.0.1'):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                settings(dict(config, mobile=dict(config['mobile'], public_origin=origin)))
        for port in (4318, 80, 65536, True, '4319'):
            with self.subTest(port=port), self.assertRaises(ValueError):
                settings(dict(config, mobile=dict(config['mobile'], port=port)))


if __name__ == '__main__':
    unittest.main()
