"""An installation can select its profile once; no per-Agent paths are needed."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from configuration import initialize, load_config


class CursorPublicConfigTests(unittest.TestCase):
    def test_default_installation_needs_no_cursor_profile_setting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            config = initialize(path)
            self.assertIsNone(config.get('cursor_public_db'))
            self.assertEqual(config['agents'], [])

    def test_profile_override_rejects_nonlocal_or_invalid_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            initialize(path)
            original = json.loads(path.read_text())
            for value in ('relative/state.vscdb', 'https://example.org/state.vscdb', [], '/tmp/a\nfile'):
                with self.subTest(value=value):
                    path.write_text(json.dumps(dict(original, cursor_public_db=value)))
                    with self.assertRaises(ValueError):
                        load_config(path)

    def test_configure_resolves_one_relative_profile_for_the_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory).resolve()
            config_path = directory / 'config.json'
            initialize(config_path)
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('office.py')),
                '--config', str(config_path), 'configure', '--cursor-public-db',
                'another-profile/state.vscdb'], cwd=directory, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            config = load_config(config_path)
            self.assertEqual(config['cursor_public_db'], str(directory / 'another-profile/state.vscdb'))
            self.assertEqual(config['agents'], [])


if __name__ == '__main__':
    unittest.main()
