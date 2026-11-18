"""Native private-file checks for installation-owned configuration and data.

Windows chmod bits do not describe the DACL. Check the opened object's owner
and allow entries instead. Existing directories are never given new permissions.
"""
from functools import lru_cache
import os
from pathlib import Path
import stat


class PrivateFileError(ValueError):
    pass


def _shape(info, directory=False):
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    return (expected(info.st_mode)
            and not getattr(info, 'st_file_attributes', 0) & 0x400)


def _windows_acl_private(owner, current_user, entries):
    """A conservative local-user ACL policy, independent of SID display names."""
    if owner != current_user or entries is None:
        return False
    trusted = {current_user, 'S-1-5-18', 'S-1-5-32-544'}  # SYSTEM / Administrators
    for kind, mask, sid in entries:
        if kind == 1:  # A denial cannot grant additional access.
            continue
        if kind != 0 or (mask and sid not in trusted):
            return False
    return True


class _WindowsSecurity:
    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w
        self.c, self.w = c, w
        self.kernel = c.WinDLL('kernel32', use_last_error=True)
        self.advapi = c.WinDLL('advapi32', use_last_error=True)
        class FileInformation(c.Structure):
            _fields_ = [('attributes', w.DWORD), ('creation', w.FILETIME),
                        ('access', w.FILETIME), ('write', w.FILETIME),
                        ('volume', w.DWORD), ('size_high', w.DWORD),
                        ('size_low', w.DWORD), ('links', w.DWORD),
                        ('index_high', w.DWORD), ('index_low', w.DWORD)]
        self.file_information = FileInformation
        p = c.c_void_p
        pp = c.POINTER(p)
        specs = [
            (self.kernel, 'GetCurrentProcess', [], w.HANDLE),
            (self.kernel, 'CloseHandle', [w.HANDLE], w.BOOL),
            (self.kernel, 'LocalFree', [p], p),
            (self.kernel, 'CreateFileW', [w.LPCWSTR, w.DWORD, w.DWORD, p,
                                        w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
            (self.kernel, 'CreateDirectoryW', [w.LPCWSTR, p], w.BOOL),
            (self.kernel, 'GetFileInformationByHandle', [w.HANDLE, c.POINTER(FileInformation)], w.BOOL),
            (self.advapi, 'OpenProcessToken', [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)], w.BOOL),
            (self.advapi, 'GetTokenInformation', [w.HANDLE, c.c_int, p, w.DWORD,
                                                c.POINTER(w.DWORD)], w.BOOL),
            (self.advapi, 'ConvertSidToStringSidW', [p, c.POINTER(w.LPWSTR)], w.BOOL),
            (self.advapi, 'ConvertStringSecurityDescriptorToSecurityDescriptorW',
             [w.LPCWSTR, w.DWORD, pp, c.POINTER(w.DWORD)], w.BOOL),
            (self.advapi, 'GetSecurityInfo', [w.HANDLE, c.c_int, w.DWORD,
                                             pp, pp, pp, pp, pp], w.DWORD),
            (self.advapi, 'GetAclInformation', [p, p, w.DWORD, c.c_int], w.BOOL),
            (self.advapi, 'GetAce', [p, w.DWORD, pp], w.BOOL),
        ]
        for library, name, arguments, result in specs:
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, result

    def _checked(self, result):
        if not result:
            raise OSError(self.c.get_last_error(), 'Windows permission check failed')
        return result

    def sid_text(self, sid):
        text = self.w.LPWSTR()
        self._checked(self.advapi.ConvertSidToStringSidW(sid, self.c.byref(text)))
        try:
            return text.value
        finally:
            self.kernel.LocalFree(self.c.cast(text, self.c.c_void_p))

    def user_sid(self):
        c, w = self.c, self.w
        token = w.HANDLE()
        self._checked(self.advapi.OpenProcessToken(self.kernel.GetCurrentProcess(), 8, c.byref(token)))
        try:
            size = w.DWORD()
            self.advapi.GetTokenInformation(token, 1, None, 0, c.byref(size))
            if not 0 < size.value <= 65536:
                raise OSError('Invalid Windows account metadata')
            buffer = c.create_string_buffer(size.value)
            self._checked(self.advapi.GetTokenInformation(token, 1, buffer, size, c.byref(size)))
            return self.sid_text(c.cast(buffer, c.POINTER(c.c_void_p))[0])
        finally:
            self.kernel.CloseHandle(token)

    def inspect_handle(self, handle, directory=None):
        c, w = self.c, self.w
        attributes = self.file_information()
        self._checked(self.kernel.GetFileInformationByHandle(handle, c.byref(attributes)))
        if (attributes.attributes & 0x400 or (directory is not None
                and bool(attributes.attributes & 0x10) != directory)):
            raise PrivateFileError('Office private paths must not use Windows reparse points')
        owner, acl, descriptor = c.c_void_p(), c.c_void_p(), c.c_void_p()
        error = self.advapi.GetSecurityInfo(handle, 1, 5, c.byref(owner), None,
                                            c.byref(acl), None, c.byref(descriptor))
        if error:
            raise OSError(error, 'Windows permission check failed')
        try:
            if not owner or not acl:
                raise PrivateFileError('Windows private owner and DACL are required')
            # ACL_SIZE_INFORMATION: AceCount, AclBytesInUse, AclBytesFree.
            size = (w.DWORD * 3)()
            self._checked(self.advapi.GetAclInformation(acl, size, c.sizeof(size), 2))
            if size[0] > 4096:
                raise PrivateFileError('Unsupported Windows permission list')
            entries = []
            for index in range(size[0]):
                ace = c.c_void_p()
                self._checked(self.advapi.GetAce(acl, index, c.byref(ace)))
                header = c.string_at(ace, 4)
                kind, length = header[0], int.from_bytes(header[2:4], 'little')
                if kind not in (0, 1) or length < 20:
                    raise PrivateFileError('Unsupported Windows permission entry')
                mask = w.DWORD.from_address(ace.value + 4).value
                entries.append((kind, mask, self.sid_text(ace.value + 8)))
            if not _windows_acl_private(self.sid_text(owner), self.user_sid(), entries):
                raise PrivateFileError('Windows folder or file must belong to your account and allow only you, SYSTEM and Administrators')
        finally:
            self.kernel.LocalFree(descriptor)

    def inspect_path(self, path, directory=False):
        # READ_CONTROL; shared read/write/delete; OPEN_EXISTING; open the reparse
        # point itself, including directories, instead of following its target.
        handle = self.kernel.CreateFileW(str(path), 0x20000, 7, None, 3, 0x02200000, None)
        if handle == self.c.c_void_p(-1).value:
            raise OSError(self.c.get_last_error(), 'Cannot check Windows file permissions')
        try:
            self.inspect_handle(handle, directory)
        finally:
            self.kernel.CloseHandle(handle)

    def create_directory(self, path):
        c, w = self.c, self.w
        class Attributes(c.Structure):
            _fields_ = [('length', w.DWORD), ('descriptor', c.c_void_p), ('inherit', w.BOOL)]
        # OI/CI preserve this policy on newly created data files and subfolders.
        user = self.user_sid()
        sddl = 'O:' + user + 'D:P' + ''.join('(A;OICI;FA;;;' + sid + ')' for sid in
                                            (user, 'SY', 'BA'))
        descriptor = c.c_void_p()
        self._checked(self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, c.byref(descriptor), None))
        try:
            attributes = Attributes(c.sizeof(Attributes), descriptor, False)
            if not self.kernel.CreateDirectoryW(str(path), c.byref(attributes)):
                error = c.get_last_error()
                if error != 183:  # A concurrent creator still must pass validation.
                    raise OSError(error, 'Cannot create private Office directory')
        finally:
            self.kernel.LocalFree(descriptor)


@lru_cache(maxsize=1)
def _windows_security():
    return _WindowsSecurity()


def require_private(path, *, directory=False, fd=None):
    """Validate a private native object. fd pins regular-file reads to its identity."""
    path = Path(path)
    expected = path.lstat()
    if not _shape(expected, directory):
        raise PrivateFileError('Office private paths must be regular files or directories without links')
    info = os.fstat(fd) if fd is not None else expected
    if not _shape(info, directory) or not os.path.samestat(expected, info):
        raise PrivateFileError('Office private file identity changed')
    if os.name == 'nt':
        try:
            security = _windows_security()
            if fd is not None:
                import msvcrt
                security.inspect_handle(msvcrt.get_osfhandle(fd), directory)
            else:
                security.inspect_path(path, directory)
            if not os.path.samestat(expected, path.lstat()):
                raise PrivateFileError('Office private path changed')
        except OSError as error:
            raise PrivateFileError('Cannot verify Windows private permissions; use an account-owned NTFS folder with access limited to you, SYSTEM and Administrators') from error
    elif info.st_mode & 0o077 or (hasattr(os, 'getuid') and info.st_uid != os.getuid()):
        raise PrivateFileError('Office private path must be owned by this account with mode 0700 (directory) or 0600 (file)')
    return path


def ensure_private_directory(path):
    path = Path(path)
    if os.name == 'nt':
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            _windows_security().create_directory(path)
    else:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return require_private(path, directory=True)


def prepare_private_file(path, fd):
    """Check Windows inheritance, or set POSIX mode, before writing any secrets."""
    if os.name != 'nt':
        os.fchmod(fd, 0o600)
    require_private(path, fd=fd)
