"""One fixed local desktop confirmation for upgrading a legacy OFFICE owner.

The browser can request attention. Only the interactive desktop confirms. The
successor credential is delivered through the local OS, never through the
requester's HTTP session.
"""
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from mobile_host import _windows_desktop
from mobile_lifecycle import MobileLifecycleError
from phone_setup_assistant import SetupNeeded, _open_owner_page
from security_keys import upgraded_security, validate_security_keys


TIMEOUT = 90
COOLDOWN = 30
TITLE = 'Virtual Office · 手機接入'
MESSAGE = ('你剛在這部電腦的 OFFICE 按了「在電腦確認」嗎？\n\n'
           '確認後，OFFICE 會更換管理書籤、停用舊的外部動態通知，'
           '再於本機瀏覽器開啟手機設定頁。現有 Agents 和工作紀錄會保留。')


def _windows_prompt(message=MESSAGE):
    """Ask the session that actually owns this OFFICE, including an RDP desktop."""
    import ctypes
    from ctypes import wintypes as w

    if not _windows_desktop():
        return 'unavailable'
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    wts = ctypes.WinDLL('wtsapi32', use_last_error=True)
    session, answer = w.DWORD(), w.DWORD()
    kernel.ProcessIdToSessionId.argtypes = [w.DWORD, ctypes.POINTER(w.DWORD)]
    kernel.ProcessIdToSessionId.restype = w.BOOL
    if not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)) or not session.value:
        return 'unavailable'
    show = wts.WTSSendMessageW
    show.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, w.DWORD, w.LPWSTR, w.DWORD,
                    w.DWORD, w.DWORD, ctypes.POINTER(w.DWORD), w.BOOL]
    show.restype = w.BOOL
    # MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2; IDTIMEOUT is not IDYES.
    ok = show(None, session.value, TITLE, len(TITLE.encode('utf-16le')),
              message, len(message.encode('utf-16le')), 0x124, TIMEOUT,
              ctypes.byref(answer), True)
    if not ok:
        return 'unavailable'
    return 'approved' if answer.value == 6 else 'cancelled'


def desktop_confirmation(*, port=None):
    message = MESSAGE
    if port is not None:
        if type(port) is not int or not 1024 <= port <= 65535:
            return 'unavailable'
        message += f'\n\n請核對你開著的 OFFICE 網址包含「:{port}/office/」。'
    if sys.platform == 'win32':
        return _windows_prompt(message)
    if sys.platform == 'darwin':
        if os.stat('/dev/console').st_uid != os.geteuid():
            return 'unavailable'
        script = ('on run argv\n'
                  'try\n'
                  'set resultBox to display dialog (item 2 of argv) with title (item 1 of argv) '
                  'buttons {"取消", "確認升級"} default button "取消" cancel button "取消" '
                  'giving up after (item 3 of argv as integer)\n'
                  'if (gave up of resultBox) then return "cancelled"\n'
                  'if (button returned of resultBox) is "確認升級" then return "approved"\n'
                  'on error number -128\nreturn "cancelled"\nend try\nend run')
        cmd = ['/usr/bin/osascript', '-e', script, TITLE, message, str(TIMEOUT)]
        result = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=TIMEOUT + 3, check=False)
        if result.returncode:
            return 'unavailable'
        return 'approved' if result.stdout.strip() == b'approved' else 'cancelled'
    if (sys.platform.startswith('linux') and (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))
            and os.access('/usr/bin/zenity', os.X_OK)):
        cmd = ['/usr/bin/zenity', '--question', '--default-cancel', '--no-markup',
               '--title=' + TITLE, '--text=' + message, '--timeout=' + str(TIMEOUT)]
        result = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=TIMEOUT + 3, check=False)
        return 'approved' if result.returncode == 0 else 'cancelled'
    return 'unavailable'


def _legacy_target(config):
    validate_security_keys(config)
    if upgraded_security(config) or 'mobile' in config and config['mobile'].get('enabled') is True:
        return None
    path = config.get('config_path')
    if not path or not Path(path).is_absolute() or config.get('host') != '127.0.0.1':
        return None
    # Bind consent to this instance and to the feed whose retirement was shown.
    return (path, config.get('run_id'), config['host'], config['port'], config['topic'],
            config.get('feed'), config.get('data_dir'))


class PhoneLocalConsent:
    def __init__(self, bridge, *, prompt=None, open_page=_open_owner_page, clock=time.monotonic):
        self.bridge, self.prompt, self.open_page, self.clock = bridge, prompt, open_page, clock
        self.lock = threading.Lock()
        self.state = 'idle'
        self.retry_at = 0

    def status(self):
        with self.lock:
            return {'state': self.state}

    def request(self):
        with self.lock:
            if self.state in ('pending', 'opening'):
                return {'state': self.state}
            if upgraded_security(self.bridge.config):
                return {'state': 'already_upgraded'}
            if self.bridge.stop.is_set():
                return {'state': 'unavailable'}
            try:
                target = _legacy_target(self.bridge.config)
            except (TypeError, ValueError, KeyError, AttributeError):
                target = None
            if target is None:
                return {'state': 'unavailable'}
            if self.clock() < self.retry_at:
                return {'state': 'retry_later'}
            started = self.clock()
            self.state = 'pending'
            worker = threading.Thread(target=self._approve, args=(target, started), daemon=True,
                                      name='office-local-phone-consent')
            try:
                worker.start()
            except RuntimeError:
                self.state = 'unavailable'
                self.retry_at = self.clock() + COOLDOWN
            return {'state': self.state}

    def _approve(self, target, started):
        outcome = 'failed'
        try:
            answer = self.prompt() if self.prompt is not None else desktop_confirmation(port=target[3])
            if answer != 'approved' or self.clock() - started >= TIMEOUT:
                outcome = 'unavailable' if answer == 'unavailable' else 'cancelled'
                return
            # Never hold service locks while presenting a native dialog.
            lifecycle = self.bridge.mobile_lifecycle
            with lifecycle.lock:
                if self.bridge.stop.is_set() or _legacy_target(self.bridge.config) != target:
                    outcome = 'configuration_changed'
                    return
                def still_approved():
                    return (self.clock() - started < TIMEOUT and not self.bridge.stop.is_set()
                            and _legacy_target(self.bridge.config) == target)
                result = lifecycle.upgrade_legacy(consent_valid=still_approved)
                if result.get('credentials_rotated') is not True:
                    outcome = 'configuration_changed'
                    return
            with self.lock:
                self.state = 'opening'
            outcome = 'open_failed'
            self.open_page(Path(target[0]), phone_setup=True)
            outcome = 'opened'
        except subprocess.TimeoutExpired:
            outcome = 'cancelled'
        except MobileLifecycleError as error:
            outcome = 'cancelled' if error.code == 'local_consent_expired' else 'configuration_changed'
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, AttributeError, SetupNeeded):
            pass  # Never return native errors, configuration, or credentials to the old browser.
        finally:
            with self.lock:
                self.state = outcome
                self.retry_at = self.clock() + COOLDOWN
