"""Owner UI requests, using a real local HTTP handler and synthetic host actions."""
import unittest
from unittest.mock import Mock, patch

import test_invitations as fixtures


class PhoneOnboardingHTTPTests(unittest.TestCase):
    setUp = fixtures.InvitationTests.setUp
    request = fixtures.InvitationTests.request

    tearDown = fixtures.InvitationTests.tearDown

    def post(self, action, data, headers=None):
        return self.request('/api/office/mobile/' + action, data,
                            headers={'Origin': self.base} if headers is None else headers)

    def test_owner_sees_phones_and_activates_only_the_displayed_selection(self):
        self.bridge.phone_onboarding = flow = Mock()
        self.bridge.tailscale_app = app = Mock()
        flow.inspect.return_value = {'state': 'select_phone', 'phones': []}
        flow.activate.return_value = {'state': 'ready'}
        app.open.return_value = {'state': 'opened'}
        grant = {'phone_id': 'test-phone', 'selection_token': 'opaque-fixture'}
        self.assertEqual(self.post('setup-status', {}), (200, flow.inspect.return_value))
        self.assertEqual(self.post('open-tailscale', {}), (200, app.open.return_value))
        self.assertEqual(self.post('setup-activate', grant), (200, flow.activate.return_value))
        flow.activate.assert_called_once_with('test-phone', 'opaque-fixture')
        for invalid in ({}, {'phone_id': 'test-phone'}, {**grant, 'port': 8080}, {**grant, 'phone_id': []}):
            self.assertEqual(self.post('setup-activate', invalid)[0], 400)
        self.assertEqual(self.post('open-tailscale', {'path': 'test-path'})[0], 400)
        for headers in ({}, {'Origin': 'https://example.invalid'}, {'Origin': self.base, 'X-Office-Key': ''}):
            self.assertEqual(self.post('setup-status', {}, headers)[0], 403)
        app.open.assert_called_once_with()
        flow.inspect.assert_called_once_with()
        self.assertEqual(flow.activate.call_count, 1)

    def test_legacy_owner_can_request_only_an_os_confirmation(self):
        consent = self.bridge.phone_local_consent = Mock()
        consent.request.return_value = {'state': 'pending'}
        consent.status.return_value = {'state': 'idle'}
        with patch.dict(self.bridge.config):
            self.bridge.config.pop('owner_key')
            self.bridge.config.pop('internal_hmac_key')
            status, data = self.request('/api/office/mobile')
            self.assertEqual(status, 200)
            self.assertEqual(data['local_consent'], {'state': 'idle'})
            self.assertEqual(self.post('local-consent', {}), (200, {'state': 'pending'}))
            for action in ('open-tailscale', 'setup-status', 'setup-activate', 'host-assistant'):
                self.assertEqual(self.post(action, {}), (403, {'error': 'mobile_owner_upgrade_required'}))
            self.assertEqual(self.post('local-consent', {'approve': True})[0], 400)
            self.assertEqual(self.post('local-consent', {}, {'Origin': self.base, 'X-Office-Key': ''})[0], 403)
        consent.request.assert_called_once_with()
