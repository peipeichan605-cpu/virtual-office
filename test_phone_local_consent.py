"""Local desktop approval tests. No actual prompts or installed settings are used."""
import ctypes
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import office
import phone_local_consent as consent
import phone_setup_assistant as assistant
from configuration import initialize, load_config, save_config
from mobile_lifecycle import MobileLifecycle
from security_keys import owner_key, upgrade_security


class LocalConsentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name).resolve() / 'config.json'
        initialize(self.path)
        cfg = load_config(self.path)
        cfg.pop('owner_key')
        cfg.pop('internal_hmac_key')
        cfg['feed'] = 'https://ntfy.sh/' + cfg['topic']
        save_config(self.path, cfg)
        self.bridge = SimpleNamespace(config=cfg, stop=threading.Event(), health={}, feed_lock=threading.RLock(),
                                      commands=SimpleNamespace(lock=threading.RLock()),
                                      history=SimpleNamespace(lock=threading.RLock(), secrets=()))
        self.bridge.mobile_lifecycle = MobileLifecycle(self.bridge, Path(self.tmp.name))
        self.shown = threading.Event()
        self.answer = threading.Event()
        self.addCleanup(self.answer.set)
        self.decision = 'approved'
        self.opened = Mock()
        self.t = 400
        def prompt():
            self.shown.set()
            if self.answer.wait(2):
                return self.decision
            return 'cancelled'
        self.consent = consent.PhoneLocalConsent(self.bridge, prompt=prompt,
                                                  open_page=self.opened, clock=lambda: self.t)

    def request(self):
        self.assertEqual(self.consent.request(), {'state': 'pending'})
        self.assertTrue(self.shown.wait(1))

    def finish(self, expected):
        self.answer.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = self.consent.status()
            if state['state'] not in ('pending', 'opening'):
                break
            time.sleep(.005)
        self.assertEqual(self.consent.status(), {'state': expected})

    def test_yes_rotates_once_and_only_native_opener_receives_the_path(self):
        old = owner_key(self.bridge.config)
        self.request()
        self.assertEqual(self.consent.request(), {'state': 'pending'})
        self.assertEqual(owner_key(load_config(self.path)), old)
        self.finish('opened')
        fresh = owner_key(load_config(self.path))
        self.assertNotEqual(fresh, old)
        self.opened.assert_called_once_with(self.path, phone_setup=True)
        self.assertEqual(self.consent.request(), {'state': 'already_upgraded'})
        self.assertNotIn(fresh, json.dumps(self.consent.status()))

    def test_decline_preserves_file_and_limits_subsequent_prompts(self):
        before = self.path.read_bytes()
        self.decision = 'cancelled'
        self.request()
        self.finish('cancelled')
        self.assertEqual(self.consent.request(), {'state': 'retry_later'})
        self.assertEqual(before, self.path.read_bytes())
        self.opened.assert_not_called()

    def test_late_yes_is_ignored(self):
        before = self.path.read_bytes()
        self.request()
        self.t += consent.TIMEOUT + 1
        self.finish('cancelled')
        self.assertEqual(before, self.path.read_bytes())

    def test_approval_waiting_for_config_lock_cannot_outlive_its_deadline(self):
        before = self.path.read_bytes()
        self.request()
        guard_entered = threading.Event()
        real_guard = self.bridge.mobile_lifecycle.upgrade_legacy
        def delayed(**kwargs):
            guard_entered.set()
            self.t += consent.TIMEOUT + 1
            return real_guard(**kwargs)
        with patch.object(self.bridge.mobile_lifecycle,'upgrade_legacy',side_effect=delayed):
            self.finish('cancelled')
        self.assertTrue(guard_entered.is_set())
        self.assertEqual(before, self.path.read_bytes())

    def test_changed_owner_target_is_not_approved(self):
        before = self.path.read_bytes()
        self.request()
        self.bridge.config['run_id'] = 'different'
        self.finish('configuration_changed')
        self.assertEqual(before, self.path.read_bytes())

    def test_stopping_core_and_invalid_security_fail_closed(self):
        self.bridge.config['owner_key'] = 'z' * 43
        self.assertEqual(self.consent.request(), {'state': 'unavailable'})
        self.bridge.config.pop('owner_key')
        self.request()
        self.bridge.stop.set()
        self.finish('configuration_changed')
        self.opened.assert_not_called()

    def test_open_failure_exposes_only_state_to_legacy_page(self):
        self.opened.side_effect = OSError('private operating system message')
        self.request()
        self.finish('open_failed')
        self.assertEqual(self.consent.request(), {'state': 'already_upgraded'})

    def test_normal_windows_shortcut_recovers_dashboard_from_a_verified_listener(self):
        save_config(self.path, upgrade_security(self.bridge.config))
        out = io.StringIO()
        with patch.object(sys,'argv',['office.py','--config',str(self.path),'start','--open-migrated']), \
                patch.object(assistant, '_open_owner_page') as opener, \
                patch.object(office, 'request') as request, redirect_stdout(out):
            office.main()
        opener.assert_called_once_with(self.path)
        request.assert_not_called()
        self.assertNotIn(owner_key(load_config(self.path)), out.getvalue())

    def test_verified_core_with_no_browser_remains_running(self):
        save_config(self.path, upgrade_security(self.bridge.config))
        with patch.object(sys,'argv',['office.py','--config',str(self.path),'start','--open-migrated']), \
                patch.object(assistant, '_open_owner_page', side_effect=assistant.SetupNeeded('browser unavailable')), \
                patch.object(office, 'request') as request, patch.object(office, 'require_offline') as offline:
            with self.assertRaisesRegex(ValueError, '預設瀏覽器'):
                office.main()
        request.assert_not_called()
        offline.assert_not_called()

    def test_handoff_targets_mobile_wizard_after_verifying_its_owner_listener(self):
        save_config(self.path, upgrade_security(self.bridge.config))
        with patch.object(sys,'platform','win32'), \
                patch.object(assistant,'_verified_listener',return_value=True) as verified, \
                patch.object(assistant.os,'startfile',create=True) as opener:
            assistant._open_owner_page(self.path, phone_setup=True)
        cfg = load_config(self.path)
        self.assertEqual(verified.call_args.args[2], '/_office/local/owner-proof')
        opener.assert_called_once_with(f"http://127.0.0.1:{cfg['port']}/office/?mobile-setup=1#{cfg['owner_key']}")


class NativeDialogs(unittest.TestCase):
    def test_windows_never_approves_a_timeout_or_another_response(self):
        def session(pid, address):
            address._obj.value = 3
            return 1
        for response, expected in ((6, 'approved'), (7, 'cancelled'), (32000, 'cancelled')):
            def modal(server, session_id, title, title_len, body, body_len, flags, timeout, pointer, wait):
                self.assertIsNone(server)
                self.assertEqual(session_id, 3)
                self.assertEqual((title_len, body_len), (len(title.encode('utf-16le')), len(body.encode('utf-16le'))))
                self.assertEqual(flags & 0x100, 0x100)  # Default button is No.
                self.assertTrue(wait)
                self.assertEqual(timeout, consent.TIMEOUT)
                pointer._obj.value = response
                return 1
            libs = [SimpleNamespace(ProcessIdToSessionId=Mock(side_effect=session)),
                    SimpleNamespace(WTSSendMessageW=Mock(side_effect=modal))]
            with self.subTest(response=response), patch.object(consent, '_windows_desktop', return_value=True), \
                    patch.object(ctypes, 'WinDLL', side_effect=libs, create=True):
                self.assertEqual(consent._windows_prompt(), expected)

    def test_session_zero_or_no_desktop_doesnt_display_a_windows_prompt(self):
        with patch.object(consent, '_windows_desktop', return_value=False), \
                patch.object(ctypes, 'WinDLL', create=True) as dll:
            self.assertEqual(consent._windows_prompt(), 'unavailable')
            dll.assert_not_called()
        funcs=SimpleNamespace(ProcessIdToSessionId=Mock(return_value=1))
        modal=Mock()
        with patch.object(consent, '_windows_desktop', return_value=True), \
                patch.object(ctypes,'WinDLL',side_effect=[funcs,modal],create=True):
            self.assertEqual(consent._windows_prompt(), 'unavailable')
            modal.WTSSendMessageW.assert_not_called()

    def test_mac_accepts_only_native_explicit_yes(self):
        for response, expected in ((b'approved\n', 'approved'), (b'cancelled\n', 'cancelled'), (b'', 'cancelled')):
            with patch.object(consent.sys, 'platform', 'darwin'), \
                    patch.object(consent.os, 'stat', return_value=SimpleNamespace(st_uid=1)), \
                    patch.object(consent.os, 'geteuid', return_value=1, create=True), \
                    patch.object(subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=response)) as run:
                self.assertEqual(consent.desktop_confirmation(), expected)
            self.assertEqual(run.call_args.args[0][:2], ['/usr/bin/osascript', '-e'])
            self.assertIn('giving up after', run.call_args.args[0][2])


if __name__ == '__main__':
    unittest.main()
