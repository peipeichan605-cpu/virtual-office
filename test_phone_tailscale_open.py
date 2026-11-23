import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import phone_tailscale_open as opener


class TailscaleAppOpenTests(unittest.TestCase):
    def test_owner_action_only_opens_gui_for_login_or_stopped(self):
        for native, want in [('Running', 'ready'), ('NeedsMachineAuth', 'device_approval'),
                             ('Starting', 'manual'), ('other', 'manual'),
                             ('NeedsLogin', 'opened'), ('Stopped', 'opened')]:
            with self.subTest(native=native), patch.object(opener, 'detect_binary', return_value='cli'), \
                    patch.object(opener, '_probe', return_value={'BackendState': native,
                                 'AuthURL': 'https://private.invalid/example'}) as probe, \
                    patch.object(opener, '_open_gui', return_value='opened') as launch:
                self.assertEqual(opener.TailscaleAppOpener().open(), {'state': want})
                probe.assert_called_once_with('cli', 'status', 2.0, opener.sys.platform)
                self.assertEqual(launch.call_count, int(want == 'opened'))

    def test_absent_unavailable_or_repeated_click_no_second_launch(self):
        subject = opener.TailscaleAppOpener()
        with patch.object(opener, 'detect_binary', return_value=None), patch.object(opener, '_probe') as probe:
            self.assertEqual(subject.open(), {'state': 'install'})
            probe.assert_not_called()
        with patch.object(opener, 'detect_binary', return_value='cli'), \
                patch.object(opener, '_probe', side_effect=opener.ProbeError('private')):
            self.assertEqual(subject.open(), {'state': 'manual'})
        with patch.object(opener, 'detect_binary', return_value='cli'), \
                patch.object(opener, '_probe', return_value={'BackendState': 'Stopped'}), \
                patch.object(opener, '_open_gui', return_value='tray') as launch:
            with subject.lock:
                self.assertEqual(subject.open(), {'state': 'busy'})
            self.assertEqual(subject.open(), {'state': 'tray'})
            self.assertEqual(subject.open(), {'state': 'busy'})
            launch.assert_called_once()

    def test_windows_existing_session_tray_or_headless(self):
        for desktop, running, expected in [(False, False, 'manual'), (True, True, 'tray'), (True, None, 'manual')]:
            with self.subTest(expected=expected), patch.object(opener, '_windows_desktop', return_value=desktop), \
                    patch.object(opener, '_windows_tray_running', return_value=running), \
                    patch.object(opener.os, 'startfile', create=True) as start:
                self.assertEqual(opener._open_gui('win32'), expected)
                start.assert_not_called()

    def test_windows_starts_only_installed_gui_with_no_args(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gui = root / 'Tailscale' / 'tailscale-ipn.exe'
            gui.parent.mkdir()
            with patch.dict(os.environ, {'ProgramFiles': str(root)}, clear=True), \
                    patch.object(opener, '_windows_desktop', return_value=True), \
                    patch.object(opener, '_windows_tray_running', return_value=False), \
                    patch.object(opener.os, 'startfile', create=True) as start:
                self.assertEqual(opener._open_gui('win32'), 'manual')
                gui.touch()
                self.assertEqual(opener._open_gui('win32'), 'opened')
                start.assert_called_once_with(str(gui))

    def test_macos_uses_launchservices_and_does_not_create_second_instance(self):
        with patch.object(opener.os, 'stat', return_value=Mock(st_uid=12)), \
                patch.object(opener.os, 'geteuid', return_value=12, create=True), \
                patch.object(Path, 'is_dir', return_value=True), \
                patch.object(opener.subprocess, 'run', return_value=Mock(returncode=0)) as launch:
            self.assertEqual(opener._open_gui('darwin'), 'opened')
            launch.assert_called_once_with(['/usr/bin/open', str(Path(opener.MAC_BINARY).parents[2])],
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, timeout=3, check=False)

    def test_non_gui_platform_offers_manual_instructions(self):
        self.assertEqual(opener._open_gui('linux'), 'manual')


if __name__ == '__main__':
    unittest.main()
