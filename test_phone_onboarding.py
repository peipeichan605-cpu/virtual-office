"""Synthetic native observations and an owned in-memory lifecycle, with no network changes."""
import copy
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mobile_setup
import phone_onboarding as flow
from test_phone_setup_assistant import CONFIG, SETTINGS, network


EXACT = {'TCP': {'443': {'TCPForward':'127.0.0.1:4319', 'TerminateTLS':'office.example-tail.ts.net',
                          'ProxyProtocol':2}}}
TOKEN = 'selection_token'


class Lifecycle:
    def __init__(self, bridge):
        self.bridge = bridge
        self.running = False
        self.configure_calls = 0
        self.extra_calls = 0

    def configure(self, target, *, peer_gate):
        self.configure_calls += 1
        if 'mobile_peer_gate' not in self.bridge.config:
            mobile_setup.mark_assistant_pending(self.bridge.config, target)
        self.bridge.config.update(mobile=dict(target), mobile_peer_gate=copy.deepcopy(peer_gate))
        self.running = True

    def authorize_phone(self, target, *, peer_gate):
        self.extra_calls += 1
        self.bridge.config['mobile_peer_gate'] = copy.deepcopy(peer_gate)

    def listener_ready(self, target, gate):
        c = self.bridge.config
        return self.running and c.get('mobile') == target and c.get('mobile_peer_gate') == gate

    @contextmanager
    def hold_listener(self, target, gate):
        if not self.listener_ready(target, gate):
            raise flow.MobileLifecycleError('mobile_listener_unavailable')
        yield


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = dict(CONFIG, data_dir=self.temp.name)
        self.mobile_lifecycle = Lifecycle(self)
        self.controller = flow.PhoneOnboarding(self)
        self.fake = {'version':{'short':'1.104.7'}, 'status':network(), 'serve':{}, 'funnel':{}}
        self.binary = self.patcher(flow.ts, 'detect_binary', return_value='/fixture/tailscale')
        self.probe = self.patcher(flow.ts, '_probe', side_effect=self.probe_one)
        self.capture = self.patcher(flow.ts, '_capture_cli', side_effect=self.activate_one)

    def patcher(self, obj, name, **kwargs):
        p = patch.object(obj, name, **kwargs)
        self.addCleanup(p.stop)
        return p.start()

    def probe_one(self, binary, name, timeout, platform):
        self.assertLessEqual(timeout, 2)
        return copy.deepcopy(self.fake[name])

    def activate_one(self, binary, argv, timeout, platform, *, include_stderr):
        self.assertEqual(binary, '/fixture/tailscale')
        self.assertEqual(argv, ['serve', '--bg', '--proxy-protocol=2', '--tls-terminated-tcp=443',
                                'tcp://127.0.0.1:4319'])
        self.assertTrue(include_stderr)
        self.fake['serve'] = self.fake['funnel'] = EXACT
        return 0, b'private details omitted'

    def choose(self, ident='phone'):
        return next(p[TOKEN] for p in self.controller.inspect()['phones'] if p['id'] == ident)

    def test_inspect_shows_only_click_targets_and_disclosure(self):
        data = self.controller.inspect()
        self.assertEqual(data['state'], 'select_phone')
        self.assertEqual(data['host'], SETTINGS['public_origin'][8:])
        self.assertEqual([p['id'] for p in data['phones']], ['iPhone','phone'])
        self.assertTrue(all(set(p) == {'id','name',TOKEN,'authorized'} for p in data['phones']))
        text = json.dumps(data)
        for private in (SETTINGS['identity'], '100.64.', 'private', '/fixture', 'SECRET'):
            self.assertNotIn(private,text)
        self.assertNotIn('mobile', self.config)
        self.capture.assert_not_called()

    def test_fresh_activation_and_second_phone_do_not_repeat_native_action(self):
        data = self.controller.activate('phone', self.choose())
        self.assertEqual(data['state'], 'ready')
        self.assertEqual(data['selected_phone_id'], 'phone')
        self.assertEqual([p['id'] for p in data['phones'] if p['authorized']], ['phone'])
        self.assertFalse(mobile_setup.assistant_pending(self.config))
        self.assertEqual(self.controller.activate('phone', self.choose())['state'], 'ready')
        self.assertEqual(self.controller.activate('iPhone', self.choose('iPhone'))['state'], 'ready')
        self.assertEqual(self.config['mobile_peer_gate']['node_ids'], ['iPhone','phone'])
        self.assertEqual(self.capture.call_count, 1)
        self.assertEqual(self.mobile_lifecycle.extra_calls, 1)

    def test_qr_issuance_rechecks_native_mount_and_selected_phone(self):
        from mobile_service import MobileService, MobileServiceError
        from test_mobile_service import A, Bridge
        bridge = Bridge(Path(self.temp.name))
        self.addCleanup(bridge.history.close)
        self.addCleanup(bridge.commands.close)
        del bridge.config['mobile']
        bridge.config.update(self.config)
        self.config = bridge.config
        bridge.phone_onboarding = self.controller
        service = MobileService(bridge)
        self.addCleanup(service.close)
        self.assertEqual(self.controller.activate('phone', self.choose())['state'], 'ready')
        data = {'identity': SETTINGS['identity'], 'label': 'Test phone', 'view_agents': [A], 'send_agents': []}
        self.fake['serve'] = self.fake['funnel'] = {}
        with self.assertRaises(MobileServiceError) as gone:
            service.owner_create_pairing(data)
        self.assertEqual((gone.exception.status, gone.exception.code), (409, 'mobile_setup_pending'))
        self.assertEqual(service.auth.db.execute('SELECT COUNT(*) FROM mobile_pairings').fetchone()[0], 0)
        self.fake['serve'] = self.fake['funnel'] = EXACT
        self.fake['status']['Peer']['phone']['Online'] = False
        with self.assertRaises(MobileServiceError) as offline:
            service.owner_create_pairing(data)
        self.assertEqual(offline.exception.code, 'mobile_setup_pending')
        self.fake['status']['Peer']['phone']['Online'] = True
        self.assertIn('pairing_token', service.owner_create_pairing(data))
        self.assertEqual(self.capture.call_count, 1)

    def test_existing_external_or_managed_tsnet_not_replaced(self):
        self.config['mobile'] = dict(SETTINGS)
        self.assertEqual(self.controller.inspect()['state'], 'existing_entry')
        del self.config['mobile']
        (Path(self.temp.name)/'phone-tsnet').mkdir()
        self.assertEqual(self.controller.inspect()['state'], 'conflict')
        self.probe.assert_not_called()

    def test_legacy_requires_separate_consent_before_discovery(self):
        del self.config['internal_hmac_key']
        del self.config['owner_key']
        self.assertEqual(self.controller.inspect()['state'], 'legacy_required')
        self.probe.assert_not_called()

    def test_only_reusable_unchanged_selection_can_commit(self):
        first = self.choose()
        self.assertEqual(self.controller.activate('iPhone', first)['state'], 'confirm_again')
        self.fake['status']['User']['9']['LoginName'] = 'other@example.test'
        self.assertEqual(self.controller.activate('phone', first)['state'], 'confirm_again')
        self.assertEqual(self.controller.activate('phone', self.choose().upper())['state'], 'confirm_again')
        self.fake['status']['Peer']['phone']['Online'] = False
        self.assertEqual(self.controller.activate('phone', first)['state'], 'confirm_again')
        self.assertEqual(self.mobile_lifecycle.configure_calls, 0)

    def test_expired_offer_and_changed_host_name_fail_without_mutation(self):
        with patch.object(flow.time, 'time', return_value=2000000000):
            first = self.choose()
        with patch.object(flow.time, 'time', return_value=2000000400):
            self.assertEqual(self.controller.activate('phone', first)['state'], 'confirm_again')
        first = self.choose()
        self.fake['status']['Self']['DNSName'] = 'changed.example.ts.net.'
        self.fake['status']['CertDomains'] = ['changed.example.ts.net']
        self.assertEqual(self.controller.activate('phone',first)['state'], 'confirm_again')
        self.capture.assert_not_called()

    def test_empty_network_or_native_prerequisite_offers_a_specific_next_step(self):
        self.binary.return_value = None
        self.assertEqual(self.controller.inspect()['state'], 'install')
        self.binary.return_value = '/fixture/tailscale'
        for native, expected in [('NeedsLogin','login'),('NeedsMachineAuth','device_approval'),('Stopped','connect')]:
            self.fake['status']['BackendState'] = native
            self.assertEqual(self.controller.inspect()['state'], expected)
        self.fake['status']['BackendState'] = 'Running'
        self.fake['status']['Peer'] = {}
        self.assertEqual(self.controller.inspect()['state'], 'no_phone')
        self.fake['status']['CertDomains'] = []
        observed=self.controller.inspect()
        self.assertEqual((observed['state'], observed['host']), ('https_required', SETTINGS['public_origin'][8:]))
        self.fake['status']['CurrentTailnet'] = {'MagicDNSEnabled':False}
        self.assertEqual(self.controller.inspect()['state'], 'dns_required')
        self.fake['version']['short'] = '1.90.0'
        self.assertEqual(self.controller.inspect()['state'], 'update_required')
        self.capture.assert_not_called()

    def test_conflicting_routes_funnel_and_unowned_match_fail_closed(self):
        cases = [({'TCP':{'80':{'HTTP':True}}}, {}), (EXACT,EXACT), ({},EXACT),
                 ({'AllowFunnel':{'host:443':True}},{}), ({'Foreground':{'private':'x'}}, {}),
                 ({'Services':{'other':{}}}, {}), ({'Unknown':{}}, {}), ({'Web':[]}, {})]
        for serve,funnel in cases:
            with self.subTest(serve=serve):
                self.fake.update(serve=serve, funnel=funnel)
                self.assertEqual(self.controller.inspect()['state'], 'conflict')
        self.capture.assert_not_called()

    def test_conflict_after_prebinding_is_never_overwritten(self):
        token = self.choose()
        def change_mount(binary,name,timeout,platform):
            if name == 'serve' and self.mobile_lifecycle.configure_calls:
                return {'Web':{'other.ts.net:443':{}}}
            return self.probe_one(binary,name,timeout,platform)
        self.probe.side_effect = change_mount
        self.assertEqual(self.controller.activate('phone',token)['state'], 'conflict')
        self.assertTrue(mobile_setup.assistant_pending(self.config))
        self.capture.assert_not_called()

    def test_nonzero_command_with_successfully_observed_mount_is_reconciled(self):
        def result(*args, **kwargs):
            self.activate_one(*args,**kwargs)
            return 1, b'access denied'
        self.capture.side_effect = result
        self.assertEqual(self.controller.activate('phone',self.choose())['state'], 'ready')

    def test_uncertain_command_is_not_repeated_and_can_be_reconciled(self):
        self.capture.side_effect = flow.ts.ProbeError('cli_timeout')
        self.assertEqual(self.controller.activate('phone',self.choose())['state'], 'activation_unconfirmed')
        self.assertTrue(mobile_setup.assistant_pending(self.config))
        self.assertEqual(self.controller.activate('phone',self.choose())['state'], 'activation_unconfirmed')
        self.assertEqual(self.capture.call_count, 1)
        self.fake['serve'] = self.fake['funnel'] = EXACT
        self.assertEqual(self.controller.activate('phone',self.choose())['state'], 'ready')
        self.assertEqual(self.capture.call_count, 1)

    def test_lost_readback_recovers_matching_listener_without_second_command(self):
        def lost_read(binary, name, timeout, platform):
            if name == 'serve' and self.capture.called:
                raise flow.ts.ProbeError('cli_timeout')
            return self.probe_one(binary, name, timeout, platform)
        self.probe.side_effect = lost_read
        self.assertEqual(self.controller.activate('phone', self.choose())['state'], 'activation_unconfirmed')
        self.assertTrue(mobile_setup.assistant_pending(self.config))
        self.probe.side_effect = self.probe_one
        self.assertEqual(self.controller.activate('phone', self.choose())['state'], 'ready')
        self.assertEqual(self.capture.call_count, 1)

    def test_native_failure_has_no_raw_output_and_allows_explicit_retry(self):
        self.capture.side_effect = None
        for message,state in [(b'access denied SECRET','permission_required'),
                              (b'flag provided but not defined SECRET','update_required'),
                              (b'unexpected SECRET','activation_unconfirmed')]:
            self.capture.return_value = 1, message
            data = self.controller.activate('phone',self.choose())
            self.assertEqual(data['state'], state)
            self.assertNotIn('SECRET',json.dumps(data))
        self.capture.side_effect = self.activate_one
        self.assertEqual(self.controller.activate('phone',self.choose())['state'], 'ready')

    def test_overlap_never_starts_another_probe_or_action(self):
        with self.controller.lock:
            self.assertEqual(self.controller.inspect()['state'], 'busy')
            self.assertEqual(self.controller.activate('phone','bad')['state'], 'busy')
        self.probe.assert_not_called()
        self.capture.assert_not_called()


if __name__ == '__main__':
    unittest.main()
