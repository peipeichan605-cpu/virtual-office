"""Checkout integrity must hold with Windows Git line-ending settings too."""
import hashlib
import json
from pathlib import Path
import unittest


class PackagePortabilityTests(unittest.TestCase):
    def test_local_ui_bytes_match_manifest(self):
        root = Path(__file__).resolve().parent
        manifest = json.loads((root / 'ui-manifest.json').read_text(encoding='utf-8'))
        for item in manifest['overrides']:
            with self.subTest(path=item['destination']):
                source = root / 'ui-overrides' / item['destination']
                self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), item['sha256'])


if __name__ == '__main__':
    unittest.main()
