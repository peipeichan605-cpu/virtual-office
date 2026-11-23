"""Choose the background dashboard's process options for the current shell."""
import json
from pathlib import Path
import subprocess
import sys


_BREAKAWAY_OK = 0x0800
_SILENT_BREAKAWAY_OK = 0x1000
_KILL_ON_JOB_CLOSE = 0x2000
_FOREGROUND_HINT = (
    "Run Office start from a normal user terminal, or use Office serve in a terminal "
    "that stays open."
)


def _current_job_limits():
    """Return the immediate Windows job's limit flags, or None outside a job."""
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ('PerProcessUserTimeLimit', ctypes.c_int64),
            ('PerJobUserTimeLimit', ctypes.c_int64),
            ('LimitFlags', wintypes.DWORD),
            ('MinimumWorkingSetSize', ctypes.c_size_t),
            ('MaximumWorkingSetSize', ctypes.c_size_t),
            ('ActiveProcessLimit', wintypes.DWORD),
            ('Affinity', ctypes.c_size_t),
            ('PriorityClass', wintypes.DWORD),
            ('SchedulingClass', wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ('BasicLimitInformation', BasicLimits),
            ('IoInfo', ctypes.c_uint64 * 6),  # IO_COUNTERS contains six ULONGLONGs.
            ('ProcessMemoryLimit', ctypes.c_size_t),
            ('JobMemoryLimit', ctypes.c_size_t),
            ('PeakProcessMemoryUsed', ctypes.c_size_t),
            ('PeakJobMemoryUsed', ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.LPBOOL]
    kernel.IsProcessInJob.restype = wintypes.BOOL
    kernel.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.POINTER(ExtendedLimits),
        wintypes.DWORD, wintypes.LPDWORD,
    ]
    kernel.QueryInformationJobObject.restype = wintypes.BOOL

    in_job = wintypes.BOOL()
    if not kernel.IsProcessInJob(kernel.GetCurrentProcess(), None, ctypes.byref(in_job)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not in_job.value:
        return None
    limits = ExtendedLimits()
    # NULL queries the caller's immediate job. No job handle is opened or changed.
    if not kernel.QueryInformationJobObject(None, 9, ctypes.byref(limits), ctypes.sizeof(limits), None):
        raise ctypes.WinError(ctypes.get_last_error())
    return limits.BasicLimitInformation.LimitFlags


def _child_job_limits(flags):
    # Use base Python directly. Running the venv redirector here would wrap the
    # probe in a new job and hide the very supervisor we are trying to detect.
    result = subprocess.run(
        [sys._base_executable, '-I', '-S', str(Path(__file__).resolve()), '--job-limits'],
        creationflags=flags, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, timeout=5, check=True,
    )
    limits = json.loads(result.stdout)
    if limits is not None and (type(limits) is not int or not 0 <= limits <= 0xffffffff):
        raise ValueError("Invalid Windows job limit response")
    return limits


def background_launch_options():
    if sys.platform != 'win32':
        return {'start_new_session': True}
    # The Windows venv redirector passes its console to the actual interpreter.
    # Give it a windowless console independent of the launching terminal.
    flags = subprocess.CREATE_NO_WINDOW
    try:
        limits = _current_job_limits()
        if limits is None or not limits & (_KILL_ON_JOB_CLOSE | _SILENT_BREAKAWAY_OK):
            return {'creationflags': flags}
        explicit = subprocess.CREATE_BREAKAWAY_FROM_JOB
        if not limits & _SILENT_BREAKAWAY_OK:
            if not limits & _BREAKAWAY_OK:
                raise RuntimeError("This Windows job will stop Office when the job closes. " + _FOREGROUND_HINT)
            flags |= explicit
        # A launcher such as py.exe can create a silent-breakaway job inside the
        # shell's job. Confirm what a direct child actually inherits.
        residual = _child_job_limits(flags)
        if residual is not None and residual & _KILL_ON_JOB_CLOSE and residual & _BREAKAWAY_OK and not flags & explicit:
            flags |= explicit
            residual = _child_job_limits(flags)
        # Windows may still assign the child to a job with no termination limit.
        if residual is not None and residual & _KILL_ON_JOB_CLOSE:
            raise RuntimeError("The background process would remain in a terminating Windows supervisor job. "
                               + _FOREGROUND_HINT)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"Could not verify the Windows job for background start ({error}). "
                           + _FOREGROUND_HINT) from error
    return {'creationflags': flags}


if __name__ == '__main__' and sys.platform == 'win32' and sys.argv[1:] == ['--job-limits']:
    print(json.dumps(_current_job_limits()))
