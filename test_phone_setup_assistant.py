"""The host setup tests use mocked Tailscale, input, config I/O and activation."""
import copy
from contextlib import nullcontext
from tempfile import TemporaryDirectory
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import phone_setup_assistant as assistant
from test_tailscale_mobile import fixture, ORIGIN, IDENTITY, PORT
from private_files import ensure_private_directory


def node(node_id, ip, os_name='android', **extra):
    return {'ID': node_id, 'HostName': node_id, 'OS': os_name, 'TailscaleIPs': [ip],
            'UserID': 9, 'Online': True, **extra}


def network():
    status = fixture()['status']
    status['Self'].update(node('computer', '100.64.0.1', 'windows'))
    status['CertDomains'] = [ORIGIN[8:]]
    status['Peer'] = {'phone': node('phone', '100.64.0.2'),
                      'ios': node('iPhone', '100.64.0.3', 'iOS')}
    return status


SETTINGS = {'enabled': True, 'port': PORT, 'identity': IDENTITY, 'public_origin': ORIGIN}
CONFIG = {'port': PORT-1, 'topic': 't'*32, 'owner_key': 'o'*32, 'internal_hmac_key': 's'*32,
          'data_dir': '/fixture', 'config_path': '/fixture/config.json'}


class HostSetupTests(unittest.TestCase):
    def discover(self, data):
        with patch.object(assistant.ts, '_probe', return_value=data):
            return assistant.discover('/fixture/tailscale', PORT)

    def test_only_owned_active_untagged_unexpired_phones(self):
        data = network()
        for index, diff in enumerate([{'UserID': 8}, {'Tags': ['tag:s']}, {'AltSharerUserID': 8},
                                     {'Expired': True}, {'KeyExpiry': '2020-01-01T00:00:00Z'},
                                     {'KeyExpiry': 'invalid'}, {'Online': False}, {'OS': 'linux'},
                                     {'ShareeNode': True}, {'TailscaleIPs': ['10.1.1.1']},
                                     {'HostName': '\nspoof'}, {'HostName': '\u202etest'}]):
            data['Peer'][f'noise{index}'] = node(f'bad{index}', f'100.64.2.{index+1}', **diff)
        found = self.discover(data)
        self.assertEqual({v.node_id for v in found.phones}, {'phone', 'iPhone'})
        self.assertEqual((found.identity, found.origin), (IDENTITY, ORIGIN))

    def test_network_identity_and_https_must_be_known(self):
        for key,value in [('CertDomains', []), ('CertDomains', ORIGIN[8:]), ('BackendState','NeedsLogin'),
                          ('User',{}), ('Self', None)]:
            with self.subTest(key=key), self.assertRaises(assistant.SetupNeeded):
                self.discover(dict(network(), **{key:value}))

    def test_conflicting_mount_and_unrecognized_shape_rejected(self):
        n = self.discover(network())
        ready = {'issues':['serve_setup_or_conflict_review_required'], 'transport_ready':False,
                 'checks':{'host_identity_matches': True}}
        for state in [{'TCP': {'22': {}}}, {'unknown': {}}, {'AllowFunnel': {'x': True}},
                      {'Foreground': {'one': {}}}, {'Web': []}]:
            with self.subTest(state=state), patch.object(assistant.ts, 'inspect_tailscale', return_value=ready), patch.object(assistant.ts, '_probe', side_effect=[{}, state]), self.assertRaises(assistant.SetupNeeded):
                assistant._mount_state('ts', n, PORT)
        with patch.object(assistant.ts, 'inspect_tailscale', return_value=ready), patch.object(assistant.ts, '_probe', return_value={}):
            self.assertEqual(assistant._mount_state('ts', n, PORT), 'empty')

    def test_two_installation_helpers_cannot_enter_simultaneously(self):
        with TemporaryDirectory() as temp:
            config = dict(CONFIG, data_dir=str(ensure_private_directory(Path(temp) / 'private')))
            with assistant._setup_lock(config):
                with self.assertRaises(assistant.SetupNeeded), assistant._setup_lock(config):
                    pass
            with assistant._setup_lock(config):
                pass

    def test_backend_challenge_is_bound_to_config_and_nonce(self):
        first = dict(CONFIG, mobile=SETTINGS)
        token = 'a'*64
        proof = assistant.gateway_proof(first, token)
        self.assertRegex(proof, r'^[a-f0-9]{64}$')
        for changed in [dict(first, internal_hmac_key='x'*32), dict(first, mobile=dict(SETTINGS, port=4390))]:
            self.assertNotEqual(proof, assistant.gateway_proof(changed, token))
        self.assertNotEqual(proof, assistant.gateway_proof(first,'b'*64))
        with self.assertRaises(ValueError):
            assistant.gateway_proof(first,'not-a-nonce')

    def test_wrong_listener_receives_no_credentials_and_cannot_answer(self):
        config = dict(CONFIG, mobile=SETTINGS)
        with patch.object(assistant.http.client, 'HTTPConnection') as make:
            client = make.return_value
            reply = client.getresponse.return_value
            reply.status = 200
            reply.getheader.return_value = 'application/json'
            reply.read.return_value = json.dumps({'proof': '0'*64}).encode()
            self.assertFalse(assistant._verified_backend(config))
            make.assert_called_once_with('127.0.0.1', PORT, timeout=2)
            self.assertEqual(set(client.request.call_args.kwargs['headers']), {'X-Office-Setup-Nonce'})

    def flow(self, *, config=None, mounts=None, proofs=None, answers=None, offline=None,
             networks=None, on_answer=None):
        config = copy.deepcopy(config if config is not None else CONFIG)
        self.store = [config]
        if networks is None:
            networks = [self.discover(network())]*4
        services = {'_installation': Mock(side_effect=lambda path: copy.deepcopy(self.store[0])),
                    '_mount_state': Mock(side_effect=mounts or ['empty','empty','empty','existing']),
                    '_verified_backend': Mock(side_effect=proofs if proofs is not None else [False,True,True]),
                    'save_config': Mock(side_effect=lambda path,c: self.store.__setitem__(0,copy.deepcopy(c))),
                    'require_offline': Mock(side_effect=offline), 'discover': Mock(side_effect=networks),
                    '_activate': Mock()}
        self.services = services
        self.messages = []
        with patch.multiple(assistant, **services), patch.object(assistant, '_setup_lock', return_value=nullcontext()), patch.object(assistant.mobile_setup, 'mark_assistant_pending') as mark, patch.object(assistant.mobile_setup, 'clear_assistant_pending') as clear, patch.object(assistant.ts,'detect_binary',return_value='ts'):
            self.mark = mark
            self.clear = clear
            responses = iter(answers or ['1','Y',''])
            def answer(prompt):
                if on_answer:
                    on_answer(prompt, self.store)
                return next(responses)
            assistant.run_setup(Path('/fixture/config.json'), ask=answer, show=self.messages.append)

    def test_fresh_installer_resumes_after_normal_stop_and_start(self):
        self.flow(offline=[ValueError('busy'),None], answers=['1','Y','',''])
        self.services['save_config'].assert_called_once()
        self.services['_activate'].assert_called_once_with('ts', PORT)
        self.assertEqual(self.store[0]['mobile'], SETTINGS)
        self.assertIn('掃碼', self.messages[-1])
        self.mark.assert_called()
        self.clear.assert_called_once()

    def test_normal_start_may_change_only_launcher_run_metadata(self):
        def change_run(prompt, store):
            if '已自行啟動' in prompt:
                store[0].update(run_id='office-new', run_dir='/fixture/runs/office-new')
        self.flow(on_answer=change_run)
        self.services['_activate'].assert_called_once()

        def change_config(prompt, store):
            if '已自行啟動' in prompt:
                store[0]['owner_key'] = 'd'*32
        with self.assertRaises(assistant.SetupNeeded):
            self.flow(on_answer=change_config)
        self.services['_activate'].assert_not_called()

    def test_existing_exact_mount_is_idempotent(self):
        self.flow(config=dict(CONFIG,mobile=SETTINGS), mounts=['existing']*3,
                  proofs=[True]*4, answers=['1','Y'])
        self.services['save_config'].assert_not_called()
        self.services['_activate'].assert_not_called()

    def test_decline_busy_changed_phone_or_wrong_listener_fail_closed(self):
        other = network()
        other['Self']['TailscaleIPs'] = ['100.64.0.98']
        for args, expected_writes in [
                ({'answers':['1','n']},0),
                ({'offline':[ValueError(),ValueError()], 'answers':['1','Y','']},0),
                ({'networks':[self.discover(network()),self.discover(other)]},0),
                ({'proofs':[False,False]},1),
                ({'mounts':['existing']},0),
                ({'config':dict(CONFIG,mobile=dict(SETTINGS,enabled=False))},0)]:
            with self.subTest(args=list(args)), self.assertRaises(assistant.SetupNeeded):
                self.flow(**args)
            self.assertEqual(self.services['save_config'].call_count,expected_writes)
            self.clear.assert_not_called()
            self.services['_activate'].assert_not_called()

    def legacy_config(self):
        legacy = {k:v for k,v in CONFIG.items() if k not in ('owner_key','internal_hmac_key')}
        legacy['feed'] = 'https://ntfy.sh/' + legacy['topic']
        legacy['agents'] = [{'provider':'generic','name':'Desktop registration'}]
        return legacy

    def test_valid_legacy_survives_decline_or_any_offline_check_failure(self):
        original = self.legacy_config()
        for replies, offline in [
                (['1','n'], None), (['1','y','n'], None),
                (['1','y','y','q'], [ValueError('busy')]),
                (['1','y','y',''], [ValueError('busy'),ValueError('busy')])]:
            with self.subTest(replies=replies), self.assertRaises(assistant.SetupNeeded):
                self.flow(config=original, answers=replies, offline=offline)
            self.assertEqual(self.store[0], original)
            self.mark.assert_not_called()
            self.services['save_config'].assert_not_called()
            self.services['_activate'].assert_not_called()

        def concurrent_change(prompt, store):
            if '一併升級' in prompt:
                store[0]['agents'].append({'name':'another agent'})
        with self.assertRaises(assistant.SetupNeeded):
            self.flow(config=original, answers=['1','y','y'], on_answer=concurrent_change)
        self.mark.assert_not_called()
        self.services['save_config'].assert_not_called()

    def test_upgrade_saves_both_settings_once_and_preserves_agents_receipts_and_secrets(self):
        original = self.legacy_config()
        receipt = assistant.hmac.new(original['topic'].encode(), b'delivery', assistant.hashlib.sha256).hexdigest()
        for source in [original,dict(original,mobile={})]:
            self.flow(config=source, answers=['1','y','y','',''])
            self.services['save_config'].assert_called_once()
            saved = self.store[0]
            self.assertEqual(saved['mobile'], SETTINGS)
            self.assertTrue(assistant.upgraded_security(saved))
            self.assertEqual(saved['agents'], original['agents'])
            self.assertEqual(saved['topic'], original['topic'])
            self.assertIsNone(saved['feed'])
            from security_keys import matches_integrity
            self.assertTrue(matches_integrity(saved,b'delivery',receipt))
            text = ' '.join(self.messages)
            for key in (saved['owner_key'], saved['internal_hmac_key'], saved['topic']):
                self.assertNotIn(key,text)
            self.assertIn('外部動態來源',text)
            self.assertIn('新書籤',text)

    def test_legacy_mobile_conflict_and_nonempty_external_mount_protect_bookmark(self):
        source = self.legacy_config()
        for config, mounts in [(dict(source, mobile={'enabled':False}), ['empty']), (source,['existing'])]:
            with self.subTest(mounts=mounts), self.assertRaises(assistant.SetupNeeded):
                self.flow(config=config, mounts=mounts)
            self.assertEqual(self.store[0],config)
            self.services['save_config'].assert_not_called()
            self.mark.assert_not_called()

    def test_repeat_setup_recovers_bookmark_before_tailscale_and_never_rotates_again(self):
        self.flow(config=self.legacy_config(),answers=['1','y','y','',''])
        saved = self.store[0]
        with patch.object(assistant,'_open_owner_page') as open_page:
            self.flow(config=saved,answers=['O'])
        open_page.assert_called_once_with(Path('/fixture/config.json'))
        self.services['discover'].assert_not_called()
        self.services['save_config'].assert_not_called()
        self.assertEqual(self.store[0],saved)

    def test_owner_listener_proofs_distinguish_new_owner_and_installation(self):
        config = dict(CONFIG, host='127.0.0.1', mobile=SETTINGS)
        nonce = 'a'*64
        proof = assistant.owner_listener_proof(config,nonce)
        self.assertNotEqual(proof,assistant.gateway_proof(config,nonce))
        self.assertNotEqual(assistant.owner_listener_proof(config,'b'*64),proof)
        for changes in [{'port':PORT+1}, {'config_path':'/else/config.json'},{'owner_key':'f'*32}]:
            self.assertNotEqual(assistant.owner_listener_proof(dict(config,**changes),nonce),proof)
        for changes in [{'host':'0.0.0.0'},{'port':True}]:
            with self.assertRaises(ValueError):
                assistant.owner_listener_proof(dict(config,**changes),nonce)

    def test_bookmark_open_requires_real_owner_proof_and_never_discloses_key_on_failure(self):
        path = Path('/fixture/config.json')
        config = dict(CONFIG,host='127.0.0.1')
        with patch.object(assistant,'load_config',return_value=config), patch.object(assistant,'_verified_listener',return_value=False), patch.object(assistant.subprocess,'run') as opened, self.assertRaises(assistant.SetupNeeded) as exc:
            assistant._open_owner_page(path)
        opened.assert_not_called()
        self.assertNotIn(config['owner_key'],str(exc.exception))

        with patch.object(assistant,'load_config',return_value=config), patch.object(assistant,'_verified_listener',return_value=True) as verify, patch.object(assistant.sys,'platform','darwin'), patch.object(assistant.subprocess,'run') as opened:
            opened.return_value.returncode=0
            assistant._open_owner_page(path)
            verify.assert_called_once_with(config, config['port'], '/_office/local/owner-proof', assistant.owner_listener_proof)
            self.assertEqual(opened.call_args.args[0], ['/usr/bin/osascript', '-l', 'JavaScript', '-'])
            self.assertNotIn(config['owner_key'], str(opened.call_args.args))
            self.assertIn(f"http://127.0.0.1:{config['port']}/office/#{config['owner_key']}".encode(),opened.call_args.kwargs['input'])
            self.assertEqual(opened.call_args.kwargs['stdout'], assistant.subprocess.DEVNULL)
            self.assertEqual(opened.call_args.kwargs['stderr'], assistant.subprocess.DEVNULL)

    def test_partial_credentials_or_tsnet_state_do_not_discover_or_write(self):
        legacy = copy.deepcopy(CONFIG)
        del legacy['owner_key']
        for config, receipt in [(legacy,False),(CONFIG,True)]:
            with self.subTest(receipt=receipt), patch.object(assistant,'load_config',return_value=config), patch.object(assistant.os.path,'lexists',return_value=receipt), patch.object(assistant.ts,'detect_binary') as detect, self.assertRaises(assistant.SetupNeeded):
                assistant.run_setup(Path('fixture'))
            detect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
