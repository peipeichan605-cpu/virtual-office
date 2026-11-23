"""Open the installed Tailscale app on an explicit local owner action."""
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from mobile_host import _windows_desktop
from tailscale_mobile import MAC_BINARY, ProbeError, _probe, detect_binary


def _windows_tray_running():
    """Check just the current session. Return None if the OS cannot tell."""
    import ctypes
    from ctypes import wintypes

    class ProcessInfo(ctypes.Structure):
        _fields_ = [('session', wintypes.DWORD), ('pid', wintypes.DWORD),
                    ('name', wintypes.LPWSTR), ('sid', ctypes.c_void_p)]

    session = wintypes.DWORD()
    session_for_pid = ctypes.WinDLL('kernel32', use_last_error=True).ProcessIdToSessionId
    session_for_pid.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    session_for_pid.restype = wintypes.BOOL
    if not session_for_pid(os.getpid(), ctypes.byref(session)):
        return None

    library = ctypes.WinDLL('wtsapi32', use_last_error=True)
    buffer = ctypes.c_void_p()
    level = wintypes.DWORD(0)  # WTS_PROCESS_INFOW
    count = wintypes.DWORD()
    enumerate_processes = library.WTSEnumerateProcessesExW
    enumerate_processes.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
                                    ctypes.POINTER(ctypes.c_void_p),
                                    ctypes.POINTER(wintypes.DWORD)]
    enumerate_processes.restype = wintypes.BOOL
    library.WTSFreeMemoryExW.argtypes = [ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    library.WTSFreeMemoryExW.restype = wintypes.BOOL
    if not enumerate_processes(None, ctypes.byref(level), session.value, ctypes.byref(buffer),
                               ctypes.byref(count)):
        return None
    try:
        if not buffer.value or not count.value:
            return None
        items = ctypes.cast(buffer, ctypes.POINTER(ProcessInfo))
        return any((items[i].name or '').casefold() == 'tailscale-ipn.exe' for i in range(count.value))
    finally:
        library.WTSFreeMemoryExW(0, buffer, count.value)  # WTSTypeProcessInfoLevel0


def _open_gui(platform):
    if platform == 'win32':
        if not _windows_desktop():
            return 'manual'
        running = _windows_tray_running()
        if running is True:
            return 'tray'
        if running is None:
            return 'manual'
        for key in ('ProgramW6432', 'ProgramFiles', 'ProgramFiles(x86)'):
            root = Path(os.environ.get(key, ''))
            gui = root / 'Tailscale' / 'tailscale-ipn.exe'
            if root.is_absolute() and gui.is_file():
                os.startfile(str(gui))
                return 'opened'
    elif platform == 'darwin':
        app = Path(MAC_BINARY).parents[2]
        if app.is_dir() and os.stat('/dev/console').st_uid == os.geteuid():
            # LaunchServices reuses an open app. No -n or CLI mode here.
            result = subprocess.run(['/usr/bin/open', str(app)], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=3, check=False)
            if result.returncode == 0:
                return 'opened'
    return 'manual'


class TailscaleAppOpener:
    def __init__(self):
        self.lock = threading.Lock()
        self.next_open = 0.0

    def open(self):
        if not self.lock.acquire(blocking=False):
            return {'state': 'busy'}
        try:
            if time.monotonic() < self.next_open:
                return {'state': 'busy'}
            binary = detect_binary()
            if binary is None:
                return {'state': 'install'}
            native = _probe(binary, 'status', 2.0, sys.platform).get('BackendState')
            if native == 'Running':
                return {'state': 'ready'}
            if native == 'NeedsMachineAuth':
                return {'state': 'device_approval'}
            if native not in ('NeedsLogin', 'Stopped'):
                return {'state': 'manual'}
            self.next_open = time.monotonic() + 6
            return {'state': _open_gui(sys.platform)}
        except (OSError, ValueError, ProbeError, subprocess.TimeoutExpired):
            return {'state': 'manual'}
        finally:
            self.lock.release()
