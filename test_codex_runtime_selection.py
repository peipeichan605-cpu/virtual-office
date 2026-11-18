"""Runtime discovery is generic and never invokes authentication or inference."""
import os
from pathlib import Path, PureWindowsPath
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import codex_rpc


class RuntimeSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.desktop = self.root / 'ChatGPT.app/Contents/MacOS/codex'
        self.desktop.parent.mkdir(parents=True)
        self.desktop.write_text('fixture')
        self.desktop.chmod(0o700)
        self.cli = self.root / 'codex'
        self.cli.write_text('fixture')
        self.cli.chmod(0o700)
        codex_rpc._RUNTIME_CHECKS.clear()
        self.candidates = patch('codex_rpc._desktop_candidates', return_value=[str(self.desktop)])
        self.which = patch('codex_rpc.shutil.which', return_value=str(self.cli))
        self.candidates.start(); self.which.start()

    def tearDown(self):
        self.candidates.stop(); self.which.stop(); self.temp.cleanup()

    def test_desktop_stdio_runtime_precedes_external_cli_and_probe_is_cached(self):
        with patch('codex_rpc.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='--listen stdio://')) as run:
            self.assertEqual(codex_rpc.resolve_binary(), str(self.desktop))
            self.assertEqual(codex_rpc.resolve_binary(), str(self.desktop))
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0], [str(self.desktop), 'app-server', '--help'])
        self.assertEqual(codex_rpc.runtime_info(str(self.desktop))['source'], 'desktop_app')

    def test_explicit_override_wins_and_invalid_override_never_falls_back(self):
        with patch('codex_rpc.subprocess.run') as run:
            self.assertEqual(codex_rpc.resolve_binary({'binary': str(self.cli)}), str(self.cli))
            with self.assertRaises(codex_rpc.RpcError):
                codex_rpc.resolve_binary({'binary': str(self.root / 'missing')})
            run.assert_not_called()

    def test_incompatible_desktop_falls_back_to_installed_cli(self):
        with patch('codex_rpc.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='old unsupported interface')):
            self.assertEqual(codex_rpc.resolve_binary(), str(self.cli))

    def test_desktop_update_changes_cache_identity_and_rechecks_capability(self):
        with patch('codex_rpc.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='--listen stdio://')) as run:
            self.assertEqual(codex_rpc.resolve_binary(), str(self.desktop))
            before = codex_rpc.runtime_info(str(self.desktop))['identity']
            self.desktop.write_text('updated fixture executable')
            self.assertNotEqual(codex_rpc.runtime_info(str(self.desktop))['identity'], before)
            self.assertEqual(codex_rpc.resolve_binary(), str(self.desktop))
            self.assertEqual(run.call_count, 2)


class WindowsRuntimeSelectionTests(unittest.TestCase):
    def setUp(self):
        self.native = 'C:\\Users\\person\\Apps & Tools\\codex.exe'
        self.shim = 'C:\\Users\\person\\npm\\codex.CMD'
        self.platform = patch('codex_rpc.os', SimpleNamespace(name='nt', access=lambda *args: True, X_OK=os.X_OK))
        self.path = patch('codex_rpc.Path', PureWindowsPath)
        self.which = patch('codex_rpc.shutil.which', side_effect=lambda name: self.native if name == 'codex.exe' else self.shim)
        self.platform.start(); self.path.start(); self.which.start()
        self.addCleanup(self.platform.stop); self.addCleanup(self.path.stop); self.addCleanup(self.which.stop)

    def test_native_path_executable_precedes_npm_shim_after_stdio_check(self):
        with patch('codex_rpc._desktop_server_available', return_value=True) as compatible:
            self.assertEqual(codex_rpc.resolve_binary(deadline=123456789.0), self.native)
        compatible.assert_called_once_with(self.native, 123456789.0)

    def test_explicit_native_override_remains_selected(self):
        selected = 'D:\\Selected Codex\\codex.exe'
        with patch('codex_rpc._desktop_server_available') as compatible:
            self.assertEqual(codex_rpc.resolve_binary({'binary': selected}), selected)
        compatible.assert_not_called()

    def test_explicit_batch_override_does_not_fall_back_to_another_runtime(self):
        with patch('codex_rpc._desktop_server_available') as compatible:
            with self.assertRaises(codex_rpc.RpcError) as error:
                codex_rpc.resolve_binary({'binary': self.shim})
        self.assertEqual(error.exception.code, 'codex_native_executable_required')
        compatible.assert_not_called()

    def test_missing_or_incompatible_native_executable_never_launches_batch_shim(self):
        for native in [None, self.native]:
            with self.subTest(native=native), \
                    patch('codex_rpc.shutil.which', side_effect=lambda name: native if name == 'codex.exe' else self.shim), \
                    patch('codex_rpc._desktop_server_available', return_value=False) as compatible:
                with self.assertRaises(codex_rpc.RpcError) as error:
                    codex_rpc.resolve_binary()
                self.assertEqual(error.exception.code, 'codex_process_unavailable' if native else 'codex_native_executable_required')
                self.assertTrue(all(call.args[0] != self.shim for call in compatible.call_args_list))


if __name__ == '__main__':
    unittest.main()
