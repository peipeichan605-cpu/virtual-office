"""Real loopback HTTP with temporary fixtures; no native Agents or secrets."""
import copy
import http.client
import json
import unittest

from security_keys import owner_key, integrity_key, security_secrets, upgrade_security
import test_invitations as fixtures


class SeparatedOwnerHttpTests(unittest.TestCase):
    setUp = fixtures.InvitationTests.setUp
    request = fixtures.InvitationTests.request

    def tearDown(self):
        self.bridge.stop.set()
        self.bridge.commands.close()
        fixtures.InvitationTests.tearDown(self)

    def test_feed_and_integrity_keys_cannot_authorize_owner_read_or_write(self):
        self.assertEqual(self.request('/api/office/work')[0], 200)
        body = {'name': 'Never created', 'provider': 'generic', 'request_id': 'forbidden-fixture'}
        for key in (self.bridge.config['topic'], integrity_key(self.bridge.config), ''):
            for path, data in (('/api/office/work', None), ('/api/office/invitations', body)):
                with self.subTest(path=path):
                    self.assertEqual(self.request(path, data, headers={'X-Office-Key': key})[0], 403)
        self.assertEqual(self.bridge.history.db.execute('SELECT COUNT(*) FROM invitations').fetchone()[0], 0)

    def test_explicit_migration_removes_legacy_owner_authority(self):
        legacy = copy.deepcopy(self.bridge.config)
        for key in ('owner_key', 'internal_hmac_key', 'legacy_integrity_keys'):
            legacy.pop(key, None)
        self.bridge.config.clear(); self.bridge.config.update(legacy)
        old = owner_key(self.bridge.config)
        self.assertEqual(self.request('/api/office/work', headers={'X-Office-Key': old})[0], 200)
        migrated = upgrade_security(self.bridge.config)
        self.bridge.config.clear(); self.bridge.config.update(migrated)
        self.assertEqual(self.request('/api/office/work', headers={'X-Office-Key': old})[0], 403)
        self.assertEqual(self.request('/api/office/work')[0], 200)

    def test_owner_projection_does_not_export_separated_keys(self):
        status, data = self.request('/api/office/work')
        self.assertEqual(status, 200)
        rendered = json.dumps(data)
        for private in security_secrets(self.bridge.config):
            self.assertNotIn(private, rendered)

    def test_non_ascii_owner_headers_fail_closed_on_read_write_and_mobile_management(self):
        for path, body in (('/api/office/work', None), ('/api/office/mobile', None),
                           ('/api/office/invitations', {}), ('/api/office/mobile/pairings', {}),
                           ('/api/office/mobile/revoke', {'device_id': 'fixture'}),
                           ('/api/office/mobile/scopes', {})):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, body, headers={'X-Office-Key': '\u00e9'})[0], 403)

    def test_scope_update_is_local_owner_only_and_preserves_phone_session(self):
        self.bridge.config['mobile'] = {'enabled': True, 'public_origin': 'https://office.example.ts.net',
                                        'identity': 'fixture@example.test'}
        auth = self.bridge.mobile.auth
        invitation = auth.create_pairing('fixture@example.test', 'Scope fixture', [], [],
                                          view_mode='enrolled', send_mode='enrolled')
        phone = auth.pair(invitation['pairing_token'], 'fixture@example.test')
        body = {'device_id': phone['device']['device_id'], 'view_agents': [], 'send_agents': [],
                'view_mode': 'enrolled', 'send_mode': 'selected'}
        path = '/api/office/mobile/scopes'
        for headers in ({'X-Office-Key': ''}, {'X-Office-Key': phone['session_token']},
                        {'X-Office-Key': self.bridge.config['topic']}, {'Origin': 'https://attacker.example'}):
            self.assertEqual(self.request(path, body, headers=headers)[0], 403)
        self.assertEqual(auth.list_devices()[0]['revision'], 1)
        status, result = self.request(path, body)
        self.assertEqual(status, 200)
        self.assertTrue(result['changed'])
        self.assertEqual(result['device']['revision'], 2)
        principal = auth.authenticate(phone['session_token'], 'fixture@example.test', csrf=phone['csrf_token'], write=True)
        self.assertEqual(principal['send_mode'], 'selected')
        self.assertEqual(principal['revision'], 2)

    def test_duplicate_owner_headers_are_rejected_even_with_one_valid_key(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            connection.putrequest('GET', '/api/office/mobile')
            connection.putheader('X-Office-Key', owner_key(self.bridge.config))
            connection.putheader('X-Office-Key', 'wrong')
            connection.endheaders()
            response = connection.getresponse(); response.read()
            self.assertEqual(response.status, 403)
        finally:
            connection.close()


if __name__ == '__main__':
    unittest.main()
