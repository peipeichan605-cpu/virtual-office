"""The distributed UI must install with native Git and system locale defaults."""
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import setup_ui


class PackagePortabilityTests(unittest.TestCase):
    def test_desktop_imports_and_preloads_the_installed_3d_runtime(self):
        root = Path(__file__).with_name('ui-overrides')
        html = (root / 'office/index.html').read_text(encoding='utf-8')
        imports = json.loads(re.search(r'<script type="importmap">(.*?)</script>', html, re.S)[1])['imports']
        self.assertEqual(imports['three'], '/assets/vendor/three/build/three.module.js')
        self.assertIn('rel="modulepreload" href="' + imports['three'] + '"', html)
        self.assertTrue((root / imports['three'].lstrip('/')).is_file())
        self.assertEqual(imports['three/addons/'], '/assets/vendor/three/examples/jsm/')

    def test_download_uses_same_commit_on_verified_fork_and_stays_there(self):
        files = {'one': b'first', 'two': b'second'}
        manifest = {'repository': 'author/ui', 'fallback_repositories': ['fork/ui'],
                    'commit': 'abc', 'overrides': [], 'files': []}
        for name, data in files.items():
            digest = hashlib.sha256(data).hexdigest()
            manifest['files'].append({'source': name, 'destination': name,
                                      'source_sha256': digest, 'installed_sha256': digest})

        def download(url, timeout):
            if '/author/ui/' in url:
                raise urllib.error.HTTPError(url, 404, 'Not found', {}, io.BytesIO())
            return io.BytesIO(files[url.rsplit('/', 1)[-1]])

        with tempfile.TemporaryDirectory() as folder, patch('urllib.request.urlopen', side_effect=download) as opened:
            setup_ui.build(manifest, Path(folder))
            self.assertEqual((Path(folder) / 'two').read_bytes(), b'second')
        prefix = 'https://raw.githubusercontent.com/'
        self.assertEqual([call.args[0] for call in opened.call_args_list],
                         [prefix + path for path in ('author/ui/abc/one', 'fork/ui/abc/one', 'fork/ui/abc/two')])

    def test_download_rejects_modified_bytes_from_the_fork(self):
        item = {'source': 'one', 'source_sha256': hashlib.sha256(b'original').hexdigest()}
        replies = [urllib.error.URLError('unavailable'), io.BytesIO(b'different')]
        with patch('urllib.request.urlopen', side_effect=replies), self.assertRaisesRegex(ValueError, 'checksum'):
            setup_ui.fetch_source(['author/ui', 'fork/ui'], 'abc', item)

    def test_install_reads_packaged_manifest_as_utf8_with_legacy_host_locales(self):
        path = Path(__file__).with_name('ui-manifest.json')
        raw = path.read_bytes()
        expected = json.loads(raw)
        native_read = Path.read_text
        for default in ('cp950', 'cp1252'):
            with self.subTest(default=default), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / path.name).write_bytes(raw)

                def read_using_host_default(file, encoding=None, **kwargs):
                    return native_read(file, encoding=encoding or default, **kwargs)

                with patch.object(Path, 'read_text', read_using_host_default), \
                        patch.object(setup_ui, 'ROOT', root), patch.object(setup_ui, 'build') as build, \
                        redirect_stdout(io.StringIO()):
                    setup_ui.main()
                build.assert_called_once()
                self.assertEqual(build.call_args.args[0], expected)

    @unittest.skipIf(os.name == 'nt', 'POSIX file mode')
    def test_mac_zip_restores_owner_execute_only_on_the_packaged_launcher(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'ui-manifest.json').write_text('{"commit": "test"}', encoding='utf-8')
            launcher = root / 'Connect Phone.command'
            launcher.write_text('#!/bin/sh\n', encoding='utf-8')
            launcher.chmod(0o640)
            with patch.object(setup_ui, 'ROOT', root), patch.object(setup_ui.sys, 'platform', 'darwin'), \
                    patch.object(setup_ui, 'build'), redirect_stdout(io.StringIO()):
                setup_ui.main()
                setup_ui.main()
                self.assertEqual(stat.S_IMODE(launcher.stat().st_mode), 0o740)
                original = root / 'user-file'
                launcher.rename(original)
                original.chmod(0o640)
                launcher.symlink_to(original)
                setup_ui.main()
                self.assertEqual(stat.S_IMODE(original.stat().st_mode), 0o640)

    def test_interrupted_download_retries_only_the_missing_file_and_verifies_it(self):
        content = b'complete file'
        item = {'source': 'asset', 'source_sha256': hashlib.sha256(content).hexdigest()}
        partial = io.BytesIO(content[:2])
        partial.headers = {'Content-Length': str(len(content))}
        for failure in (partial, ConnectionResetError()):
            with self.subTest(failure=type(failure).__name__):
                with patch('urllib.request.urlopen', side_effect=[failure, io.BytesIO(content)]) as get:
                    self.assertEqual(setup_ui.fetch_source(['fork/ui'], 'abc', item), (content, 'fork/ui'))
                    self.assertEqual(get.call_count, 2)
        with patch('urllib.request.urlopen', side_effect=ConnectionResetError()) as get:
            with self.assertRaisesRegex(RuntimeError, 'Pinned UI file unavailable'):
                setup_ui.fetch_source(['fork/ui'], 'abc', item)
            self.assertEqual(get.call_count, 2)

    def test_local_ui_bytes_match_manifest(self):
        root = Path(__file__).resolve().parent
        manifest = json.loads((root / 'ui-manifest.json').read_text(encoding='utf-8'))
        for item in manifest['overrides']:
            with self.subTest(path=item['destination']):
                source = root / 'ui-overrides' / item['destination']
                self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), item['sha256'])


if __name__ == '__main__':
    unittest.main()
