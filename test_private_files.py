"""Native private-data portability, using only temporary test directories."""
import os
from pathlib import Path
import stat
import tempfile
import unittest

from configuration import initialize, load_config, save_config
from mobile_auth import MobileAuth
from private_files import (PrivateFileError, _shape, _windows_acl_private,
                           ensure_private_directory, prepare_private_file, require_private)


class WindowsAclPolicyTests(unittest.TestCase):
    USER = 'S-1-5-21-101-102-103-1001'

    def test_allows_only_owner_and_os_administrators(self):
        entries = [(0, 0x1f01ff, sid) for sid in (self.USER, 'S-1-5-18', 'S-1-5-32-544')]
        self.assertTrue(_windows_acl_private(self.USER, self.USER, entries))
        for sid in ('S-1-1-0', 'S-1-5-11', 'S-1-5-32-545', 'S-1-5-21-101-102-103-1002'):
            with self.subTest(sid=sid):
                self.assertFalse(_windows_acl_private(self.USER, self.USER, entries + [(0, 1, sid)]))

    def test_null_unknown_and_foreign_owner_fail_closed(self):
        self.assertFalse(_windows_acl_private(self.USER, self.USER, None))
        self.assertFalse(_windows_acl_private('S-1-5-21-101-102-103-1002', self.USER, []))
        self.assertFalse(_windows_acl_private(self.USER, self.USER, [(5, 1, self.USER)]))
        self.assertTrue(_windows_acl_private(self.USER, self.USER, [(1, 1, 'S-1-1-0')]))

    def test_elevated_default_owner_keeps_the_same_private_dacl_boundary(self):
        # Default object ownership can be an OS principal even when all access
        # was inherited from a folder explicitly owned by the current user.
        entries = [(0, 0x1f01ff, sid) for sid in (self.USER, 'S-1-5-18', 'S-1-5-32-544')]
        for owner in (self.USER, 'S-1-5-18', 'S-1-5-32-544'):
            with self.subTest(owner=owner):
                self.assertTrue(_windows_acl_private(owner, self.USER, entries))
                self.assertFalse(_windows_acl_private(owner, self.USER, None))
                for outsider in ('S-1-1-0', 'S-1-5-11', 'S-1-5-32-545',
                                 'S-1-5-21-101-102-103-1002'):
                    self.assertFalse(_windows_acl_private(owner, self.USER,
                                                         entries + [(0, 1, outsider)]))
        for owner in ('S-1-1-0', 'S-1-5-11', 'S-1-5-32-545',
                      'S-1-5-21-101-102-103-1002'):
            self.assertFalse(_windows_acl_private(owner, self.USER, entries))

    def test_windows_reparse_attributes_are_not_treated_as_regular_storage(self):
        from types import SimpleNamespace
        self.assertFalse(_shape(SimpleNamespace(st_mode=stat.S_IFREG | 0o600,
                                               st_file_attributes=0x400)))
        self.assertFalse(_shape(SimpleNamespace(st_mode=stat.S_IFDIR | 0o700,
                                               st_file_attributes=0x400), directory=True))


class NativePrivateFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'private-data'

    def test_new_config_unicode_atomic_save_and_mobile_database(self):
        # Runs the real WinAPI path on Windows, POSIX mode checks elsewhere.
        config_path = self.root / 'config.json'
        config = initialize(config_path)
        config['brand'] = '通用辦公室 · 中文'
        save_config(config_path, config)
        self.assertEqual(load_config(config_path)['brand'], config['brand'])
        self.assertIn(config['brand'], config_path.read_text(encoding='utf-8'))
        require_private(self.root, directory=True)
        require_private(config_path)
        auth = MobileAuth(config)
        auth.close()
        require_private(self.root / 'mobile-auth.sqlite3')
        self.assertEqual(list(self.root.glob('.config.json-*.tmp')), [])
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)

    def test_file_identity_is_checked_against_open_descriptor(self):
        ensure_private_directory(self.root)
        first, second = self.root / 'first', self.root / 'second'
        first.write_text('first'); second.write_text('second')
        first.chmod(0o600); second.chmod(0o600)
        with first.open('rb') as source:
            with self.assertRaises(PrivateFileError):
                require_private(second, fd=source.fileno())

    def test_symlinks_are_rejected(self):
        ensure_private_directory(self.root)
        target = self.root / 'target'; target.write_text('fixture'); target.chmod(0o600)
        link = self.root / 'link'
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest('Host requires approval to create a test symlink')
        with self.assertRaises(PrivateFileError): require_private(link)
        alias = Path(self.temp.name) / 'directory-link'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(PrivateFileError): ensure_private_directory(alias)

    @unittest.skipIf(os.name == 'nt', 'POSIX permission bits are not Windows ACLs')
    def test_existing_public_directory_is_rejected_without_chmod(self):
        self.root.mkdir(mode=0o755); self.root.chmod(0o755)
        with self.assertRaises(PrivateFileError): ensure_private_directory(self.root)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o755)
        self.assertEqual(list(self.root.iterdir()), [])

    @unittest.skipUnless(os.name == 'nt', 'Native Windows inherited ACL check')
    def test_new_children_inherit_private_acl(self):
        ensure_private_directory(self.root)
        # Native elevated Windows runners may assign Administrators ownership
        # here; the private inherited DACL still has exactly the trusted scope.
        child = self.root / 'child'; child.mkdir()
        require_private(child, directory=True)
        path = child / 'record.json'
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            prepare_private_file(path, fd)
            os.write(fd, b'{}')
        finally:
            os.close(fd)
        require_private(path)


if __name__ == '__main__':
    unittest.main()
