"""Offline security and dependency checks for the phone's static surface."""
import hashlib
import json
from pathlib import Path
import posixpath
import re
import tempfile
import unittest

from mobile_static_manifest import OFFICE_FILES, THREE_FILES, mobile_static_file


ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / 'public'
THREE = PUBLIC / 'assets/vendor/three'
IMPORTS = re.compile(r'''^\s*(?:import|export)\s+(?:\{[^}]*\}|\*\s+as\s+[A-Za-z0-9_$]+|[A-Za-z0-9_$]+)\s+from\s*["']([^"'\n]+)["']''', re.M)


class MobileStaticBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'public'
        self.root.mkdir()

    def file(self, name, content='fixture'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def test_approved_runtime_and_binary_asset(self):
        for name in ['office/app.js', 'office/mobile-access.js', 'office/platform-icons/codex.svg',
                     'office/audio/music-loop.m4a', 'assets/office/office.glb']:
            with self.subTest(name=name):
                path = self.file(name)
                self.assertEqual(mobile_static_file(self.root, '/' + name), path.resolve())

    def test_owner_legacy_and_unlisted_pages_stay_denied_even_if_present(self):
        for name in ['office/index.html', 'office/agent.html', 'office/mobile.html',
                     'office/mobile-owner.js', 'office/mobile-owner.css', 'office/invite.js',
                     'office/face/face.js', 'office/data/demo-state.json', 'office/harness.js',
                     'office/new-unreviewed-module.js', 'assets/surprise.js', 'assets/page.html',
                     'assets/vendor/three/PROVENANCE.json', 'assets/vendor/three/other.js']:
            with self.subTest(name=name):
                self.file(name)
                self.assertIsNone(mobile_static_file(self.root, '/' + name))

    def test_noncanonical_paths_are_rejected_without_normalization(self):
        self.file('office/app.js')
        for path in ['/office/../office/app.js', '/office/./app.js', '//office/app.js',
                     '/office//app.js', '/office/%61pp.js', '/office/%2e%2e/app.js',
                     '/office/%252e%252e/app.js', '/office\\app.js', '/office/app.js?x=1',
                     '/office/app.js#x', 'office/app.js', '/office/app.js\x00.png',
                     '/assets/.private/key.png', '/office/' + 'x' * 401 + '.js', None]:
            with self.subTest(path=path):
                self.assertIsNone(mobile_static_file(self.root, path))

    def test_missing_files_and_directories_are_denied(self):
        self.assertIsNone(mobile_static_file(self.root, '/office/app.js'))
        (self.root / 'office/app.js').mkdir(parents=True)
        self.assertIsNone(mobile_static_file(self.root, '/office/app.js'))

    def test_file_symlink_is_denied_even_to_inside_root(self):
        target = self.file('office/commands.js')
        (self.root / 'office/app.js').symlink_to(target)
        self.assertIsNone(mobile_static_file(self.root, '/office/app.js'))

    def test_directory_symlink_is_denied(self):
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        (outside / 'office.glb').write_bytes(b'fixture')
        (self.root / 'assets').mkdir()
        (self.root / 'assets/office').symlink_to(outside, target_is_directory=True)
        self.assertIsNone(mobile_static_file(self.root, '/assets/office/office.glb'))

    def test_public_root_symlink_is_denied(self):
        self.file('office/app.js')
        link = Path(self.temp.name) / 'public-link'
        link.symlink_to(self.root, target_is_directory=True)
        self.assertIsNone(mobile_static_file(link, '/office/app.js'))


class VendoredThreeTests(unittest.TestCase):
    def test_pinned_registry_provenance_and_all_file_hashes(self):
        meta = json.loads((THREE / 'PROVENANCE.json').read_text())
        self.assertEqual(meta['package'], 'three')
        self.assertEqual(meta['version'], '0.170.0')
        self.assertEqual(meta['metadata_url'], 'https://registry.npmjs.org/three/0.170.0')
        self.assertTrue(meta['npm_integrity'].startswith('sha512-'))
        self.assertEqual(THREE_FILES, {'assets/vendor/three/' + name for name in meta['files']})
        for name, receipt in meta['files'].items():
            with self.subTest(name=name):
                path = THREE / name
                self.assertFalse(path.is_symlink())
                data = path.read_bytes()
                self.assertEqual(len(data), receipt['bytes'])
                self.assertEqual(hashlib.sha256(data).hexdigest(), receipt['sha256'])
                self.assertEqual(mobile_static_file(PUBLIC, '/assets/vendor/three/' + name), path.resolve())
        self.assertIn('MIT License', (THREE / 'LICENSE').read_text())

    def test_vendored_import_graph_resolves_without_network(self):
        meta = json.loads((THREE / 'PROVENANCE.json').read_text())
        for name in meta['files']:
            if not name.endswith('.js'):
                continue
            for specifier in IMPORTS.findall((THREE / name).read_text()):
                with self.subTest(module=name, specifier=specifier):
                    if specifier == 'three':
                        target = 'build/three.module.js'
                    else:
                        self.assertTrue(specifier.startswith('.'), 'Unexpected external/bare import')
                        target = posixpath.normpath(posixpath.join(posixpath.dirname(name), specifier))
                    self.assertIn(target, meta['files'])

    def test_all_application_three_imports_are_vendored(self):
        # Inspect declarations only. This test neither imports JS nor accesses
        # package registries, CDNs or existing OFFICE services.
        for path in (PUBLIC / 'office').rglob('*.js'):
            if 'face' in path.parts:
                continue
            for specifier in IMPORTS.findall(path.read_text()):
                if specifier == 'three':
                    target = 'build/three.module.js'
                elif specifier.startswith('three/addons/'):
                    target = 'examples/jsm/' + specifier.removeprefix('three/addons/')
                else:
                    continue
                with self.subTest(module=str(path.relative_to(PUBLIC)), specifier=specifier):
                    self.assertIsNotNone(mobile_static_file(PUBLIC, '/assets/vendor/three/' + target))

    def test_current_glb_assets_have_no_external_buffers_or_images(self):
        for path in [PUBLIC / 'assets/office/office.glb', PUBLIC / 'assets/blobs/grok-bot.glb']:
            with self.subTest(asset=path.name):
                data = path.read_bytes()
                self.assertEqual(data[:4], b'glTF')
                length = int.from_bytes(data[12:16], 'little')
                self.assertEqual(data[16:20], b'JSON')
                obj = json.loads(data[20:20 + length])
                for item in obj.get('buffers', []) + obj.get('images', []):
                    self.assertNotIn('uri', item)


if __name__ == '__main__':
    unittest.main()
