"""Owner desktop actions for first-time phone setup. No action opens a network listener."""
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

import tailscale_mobile as tailscale

ROOT = Path(__file__).resolve().parent
CHECK_TTL = 2.0
LAUNCH_COOLDOWN = 10.0
PLATFORMS = {'win32': 'windows', 'darwin': 'macos', 'linux': 'linux'}


def local_setup_nonce(request, port, *, signed_request=False):
    """Accept only the direct loopback request made by the desktop assistant."""
    allowed = {'host', 'connection', 'accept-encoding', 'x-office-setup-nonce'}
    if signed_request:
        allowed.add('x-office-setup-request')
    nonces = request.headers.get_all('X-Office-Setup-Nonce', [])
    if (request.client_address[0] != '127.0.0.1' or
            request.headers.get_all('Host', []) != ['127.0.0.1:' + str(port)] or
            any(name.lower() not in allowed for name in request.headers) or
            len(nonces) != 1 or not re.fullmatch('[0-9a-f]{64}', nonces[0])):
        return None
    return nonces[0]


def host_hint(config):
    """Public desktop labels only; never inspect or launch a host program here."""
    path = config.get('config_path')
    return {'platform': PLATFORMS.get(sys.platform, 'other'),
            'custom_config': not isinstance(path, str) or Path(path) != ROOT / '.office' / 'config.json'}


def _windows_desktop():
    """Detached OFFICE in the user's desktop session is fine; a service station is not."""
    import ctypes
    from ctypes import wintypes

    class UserObjectFlags(ctypes.Structure):
        _fields_ = [('inherit', wintypes.BOOL), ('reserved', wintypes.BOOL),
                    ('flags', wintypes.DWORD)]

    user = ctypes.WinDLL('user32', use_last_error=True)
    user.GetProcessWindowStation.argtypes = []
    user.GetProcessWindowStation.restype = wintypes.HANDLE
    user.GetUserObjectInformationW.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    user.GetUserObjectInformationW.restype = wintypes.BOOL
    flags, size = UserObjectFlags(), wintypes.DWORD()
    ok = user.GetUserObjectInformationW(user.GetProcessWindowStation(), 1,
                                        ctypes.byref(flags), ctypes.sizeof(flags),
                                        ctypes.byref(size))
    return bool(ok and flags.flags & 1)  # WSF_VISIBLE


def _launch_command(config):
    config_path = config.get('config_path')
    helper = ROOT / 'phone_setup_assistant.py'
    if not isinstance(config_path, str) or not Path(config_path).is_absolute() or not helper.is_file():
        return None
    python = Path(sys.executable)
    if sys.platform == 'win32' and python.name.lower() == 'pythonw.exe':
        python = python.with_name('python.exe')
    if not python.is_absolute() or not python.is_file():
        return None
    args = [str(python), str(helper), '--config', config_path, '--desktop']
    if sys.platform == 'win32' and _windows_desktop():
        return args, {'creationflags': subprocess.CREATE_NEW_CONSOLE}
    if sys.platform == 'darwin' and os.stat('/dev/console').st_uid == os.geteuid():
        launcher = ROOT / 'Connect Phone.command'
        for app_root in ('/System/Applications/Utilities', '/Applications/Utilities'):
            terminal = Path(app_root) / 'Terminal.app'
            if terminal.is_dir() and os.access(launcher, os.X_OK):
                # Terminal receives this installation's environment in a new
                # instance. Waiting for the whole app would block later retries
                # when its last window closes but Terminal stays in the dock.
                return (['/usr/bin/open', '-n', '-a', str(terminal),
                         '--env', 'OFFICE_CONFIG=' + config_path,
                         '--env', 'OFFICE_PHONE_PYTHON=' + str(python), str(launcher)],
                        {'start_new_session': True})
    if sys.platform == 'linux' and (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        for prefix in (['/usr/bin/gnome-terminal', '--wait', '--'],
                       ['/usr/bin/konsole', '--nofork', '-e'], ['/usr/bin/xterm', '-e']):
            if os.access(prefix[0], os.X_OK):
                return prefix + args, {'start_new_session': True}
    return None


class MobileHostAssistant:
    def __init__(self, config):
        self.config = config
        self.check_lock = threading.Lock()
        self.launch_lock = threading.Lock()
        self.check_expires = self.next_launch = 0.0
        self.last_check = None
        self.window = None
        self.mac_launch_accepted = False

    def launch_command(self):
        try:
            return _launch_command(self.config)
        except (OSError, ValueError):
            return None

    def check(self):
        with self.check_lock:
            if self.last_check is not None and time.monotonic() < self.check_expires:
                return self.last_check.copy()
            state = 'unknown'
            try:
                binary = tailscale.detect_binary()
                if binary:
                    status = tailscale._probe(binary, 'status', 2.0, sys.platform)
                    state = {'Running': 'running', 'NeedsLogin': 'login',
                             'NeedsMachineAuth': 'approve', 'Stopped': 'connect'}.get(
                                 status.get('BackendState'), 'unknown')
                else:
                    state = 'install'
            except (tailscale.ProbeError, OSError, TypeError, ValueError):
                pass
            self.last_check = {**host_hint(self.config), 'state': state,
                               'launch_available': self.launch_command() is not None}
            self.check_expires = time.monotonic() + CHECK_TTL
            return self.last_check.copy()

    def open(self):
        # A successful request means the OS accepted a local launch. Only the
        # interactive helper can obtain consent and configure the private host.
        result = {**host_hint(self.config), 'state': 'manual'}
        platform = result['platform']
        with self.launch_lock:
            if self.window is not None and self.window.poll() is None:
                return {**result, 'state': 'opened'}
            if time.monotonic() < self.next_launch:
                if platform == 'macos' and self.mac_launch_accepted and self.window.returncode == 0:
                    result['state'] = 'opened'
                return result
            self.mac_launch_accepted = False
            self.next_launch = time.monotonic() + LAUNCH_COOLDOWN
            command = self.launch_command()
            if command is None:
                return result
            argv, options = command
            try:
                # OFFICE normally sends its own stdout/stderr to a private
                # log. A new Windows console supplies its own handles; POSIX
                # terminal emulators allocate a pty for the interactive child.
                if platform != 'windows':
                    options = {**options, 'stdin': subprocess.DEVNULL,
                               'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
                self.window = subprocess.Popen(argv, cwd=ROOT, **options)
                code = self.window.wait(timeout=.2)
                if platform == 'macos' and code == 0:
                    self.mac_launch_accepted = True
                    result['state'] = 'opened'
            except subprocess.TimeoutExpired:
                self.mac_launch_accepted = platform == 'macos'
                result['state'] = 'opened'
            except (OSError, ValueError):
                pass
        return result
