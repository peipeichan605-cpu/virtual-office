"""Portable file/queue fixtures; no live Office, native queue or account access."""
import io
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from command_adapters import CodexAppAdapter, DeliveryError
from dashboard import bounded_json, proxy_handler
from history import open_native_rollout
from workspaces import workspace_path


class SourceFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / 'selected.json'
        self.path.write_bytes(b'{"state":"ready"}\r\n')

    def tearDown(self):
        self.temp.cleanup()

    def test_regular_file_keeps_bytes_when_nofollow_is_unavailable(self):
        with patch('history.os.O_NOFOLLOW', 0, create=True):
            with open_native_rollout(self.path) as source:
                self.assertEqual(source.read(), b'{"state":"ready"}\r\n')
            self.assertTrue(source.closed)
            self.assertEqual(bounded_json(self.path), {'state': 'ready'})
        with self.assertRaises(ValueError):
            bounded_json(self.path, limit=2)

    def test_windows_reparse_and_nonregular_files_rejected_before_open(self):
        reparse = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
        for info in (SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=reparse),
                     SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0)):
            with patch.object(Path, 'lstat', return_value=info), patch('history.os.open') as opened:
                with self.assertRaises(ValueError):
                    with open_native_rollout(self.path):
                        self.fail('Invalid source was opened')
                opened.assert_not_called()

    def test_replaced_file_is_rejected_without_reading_it(self):
        replacement = self.root / 'replacement.json'
        replacement.write_bytes(b'{"state":"different"}')
        real_open = os.open

        def replace_before_open(path, flags):
            os.replace(replacement, self.path)
            return real_open(path, flags)

        with patch('history.os.open', side_effect=replace_before_open):
            with self.assertRaisesRegex(ValueError, 'identity changed'):
                with open_native_rollout(self.path):
                    self.fail('Replacement source was returned')

    def test_symlink_is_rejected_without_nofollow_support(self):
        link = self.root / 'link.json'
        try:
            link.symlink_to(self.path)
        except OSError:
            self.skipTest('Creating symlinks requires host permission')
        with patch('history.os.O_NOFOLLOW', 0, create=True):
            with self.assertRaises(ValueError):
                with open_native_rollout(link):
                    self.fail('Symlink source was returned')


class NativeWindowsQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.binary = Path(self.temp.name).resolve() / 'codex.exe'
        self.binary.write_bytes(b'fixture only')
        self.binary.chmod(0o700)
        self.session = '00000000-0000-4000-8000-000000000079'

    def tearDown(self):
        self.temp.cleanup()

    def test_windows_requires_native_queue_capability_and_uses_no_window(self):
        receipt = '00000000-0000-4000-8000-000000000080'
        replies = [subprocess.CompletedProcess([], 0, 'Queue a message for an existing session --thread', ''),
                   subprocess.CompletedProcess([], 0, 'Queued message ' + receipt + ' for thread ' + self.session + '.\n', '')]
        body = 'Literal $(example) `example`\nSecond line'
        adapter = CodexAppAdapter()
        with patch('command_adapters.sys.platform', 'win32'), \
                patch('command_adapters.shutil.which', return_value=str(self.binary)) as which, \
                patch('command_adapters.subprocess.CREATE_NO_WINDOW', 0x08000000, create=True), \
                patch('command_adapters.subprocess.run', side_effect=replies) as run:
            self.assertTrue(adapter.available())
            result = adapter.submit(self.session, body)
        which.assert_called_once_with('codex.exe')
        self.assertEqual(result['native_message_id'], receipt)
        self.assertNotIn('completed', result)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1].args[0],
                         [str(self.binary), 'queue', '--thread', self.session, '--message', body])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs['creationflags'], 0x08000000)
            self.assertNotIn('shell', call.kwargs)

    def test_explicit_binary_does_not_fall_back_or_ignore_missing_queue(self):
        adapter = CodexAppAdapter({'binary': str(self.binary)})
        with patch('command_adapters.sys.platform', 'win32'), \
                patch('command_adapters.shutil.which') as which, \
                patch('command_adapters.subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'Other command', '')):
            self.assertFalse(adapter.available())
            with self.assertRaises(DeliveryError):
                adapter.submit(self.session, 'Do not send')
        which.assert_not_called()
        self.assertEqual(adapter.reason, 'codex_queue_unavailable')

    def test_windows_batch_shim_cannot_receive_user_text(self):
        shim = self.binary.with_suffix('.cmd')
        shim.write_bytes(b'fixture only')
        shim.chmod(0o700)
        adapter = CodexAppAdapter({'binary': str(shim)})
        with patch('command_adapters.sys.platform', 'win32'), \
                patch('command_adapters.subprocess.run') as run:
            self.assertFalse(adapter.available())
        run.assert_not_called()


class PortableStaticFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.office = self.root / 'public' / 'office'
        self.office.mkdir(parents=True)
        (self.office / 'index.html').write_bytes(b'fixture office')
        (self.root / 'outside.txt').write_bytes(b'not public')

    def tearDown(self):
        self.temp.cleanup()

    def request(self, path):
        handler = object.__new__(proxy_handler(SimpleNamespace(config={'port': 4318})))
        handler.path = path
        handler.headers = {'Host': '127.0.0.1:4318'}
        handler.wfile = io.BytesIO()
        handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
        with patch('dashboard.ROOT', self.root):
            handler.do_GET()
        return handler.send_response.call_args.args[0], handler.wfile.getvalue()

    def test_directory_index_is_served_on_native_path_separator(self):
        self.assertEqual(self.request('/office/'), (200, b'fixture office'))

    def test_parent_boundary_is_enforced_after_resolution(self):
        self.assertEqual(self.request('/office/../../outside.txt')[0], 404)
        self.assertEqual(self.request('/office/%2e%2e/%2e%2e/outside.txt')[0], 404)

    def test_index_symlink_cannot_escape_public_root(self):
        (self.office / 'index.html').unlink()
        try:
            (self.office / 'index.html').symlink_to(self.root / 'outside.txt')
        except OSError:
            self.skipTest('Creating symlinks requires host permission')
        self.assertEqual(self.request('/office/')[0], 404)


class WindowsWorkspaceTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Native Windows path identity test')
    def test_existing_extended_drive_path_and_plain_path_share_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            ordinary = str(Path(folder).resolve())
            if ordinary.startswith('\\\\'):
                self.skipTest('Fixture needs a local drive')
            (Path(folder) / 'child').mkdir()
            extended = '\\\\?\\' + ordinary
            self.assertEqual(workspace_path(extended), workspace_path(ordinary))
            self.assertEqual(workspace_path(extended + '\\child'), workspace_path(ordinary + '\\child'))


if __name__ == '__main__':
    unittest.main()
