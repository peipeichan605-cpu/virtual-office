"""Native process, shell and lock operations used by OFFICE and its local helpers."""
import errno
import os
import shlex
import sys


def shell_command(argv):
    if sys.platform == 'win32':
        return '& ' + ' '.join("'" + arg.replace("'", "''") + "'" for arg in argv)
    return shlex.join(argv)


def process_exists(pid):
    if type(pid) is not int or not 1 <= pid <= 0x7fffffff:
        return False
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        kernel.OpenProcess.restype = w.HANDLE
        kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        kernel.WaitForSingleObject.restype = w.DWORD
        kernel.CloseHandle.argtypes = [w.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
        if not handle:
            code = ctypes.get_last_error()
            if code == 87:  # ERROR_INVALID_PARAMETER for a missing PID
                return False
            if code == 5:   # Access denied; do not reclaim another process's lock.
                return True
            raise ctypes.WinError(code)
        try:
            result = kernel.WaitForSingleObject(handle, 0)
            if result not in (0, 258):
                raise ctypes.WinError(ctypes.get_last_error())
            return result == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def lock_exclusive(fd, *, blocking):
    """Lock the first byte on Windows or the file on POSIX until fd closes."""
    if os.name == 'nt':
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            raise BlockingIOError('OFFICE lock is in use') from error
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
