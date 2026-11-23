import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import office_launch


class BackgroundLaunchTests(unittest.TestCase):
    def windows_options(self, limits, residuals=()):
        with mock.patch.object(sys, 'platform', 'win32'), mock.patch.multiple(
            subprocess, CREATE_NO_WINDOW=0x08000000, CREATE_BREAKAWAY_FROM_JOB=0x01000000, create=True
        ), mock.patch.object(office_launch, '_current_job_limits', return_value=limits), mock.patch.object(
            office_launch, '_child_job_limits', side_effect=residuals
        ) as probe:
            result = office_launch.background_launch_options()
            self.assertEqual(probe.call_count, len(residuals))
            return result

    def test_posix_starts_new_session_without_querying_job(self):
        with mock.patch.object(sys, 'platform', 'darwin'), mock.patch.object(office_launch, '_current_job_limits') as query:
            self.assertEqual(office_launch.background_launch_options(), {'start_new_session': True})
        query.assert_not_called()

    def test_windows_no_job_or_unrelated_limits(self):
        for limits in (None, 0, 0x0008, office_launch._BREAKAWAY_OK):
            with self.subTest(limits=limits):
                self.assertEqual(self.windows_options(limits), {'creationflags': 0x08000000})

    def test_windows_permitted_explicit_breakaway(self):
        limits = office_launch._KILL_ON_JOB_CLOSE | office_launch._BREAKAWAY_OK
        self.assertEqual(self.windows_options(limits, [None]), {'creationflags': 0x09000000})

    def test_windows_silent_breakaway(self):
        limits = office_launch._KILL_ON_JOB_CLOSE | office_launch._SILENT_BREAKAWAY_OK
        for extra in (0, office_launch._BREAKAWAY_OK):
            with self.subTest(extra=extra):
                self.assertEqual(self.windows_options(limits | extra, [None]), {'creationflags': 0x08000000})

    def test_launcher_inside_outer_job_permitting_explicit_release(self):
        inner = office_launch._KILL_ON_JOB_CLOSE | office_launch._SILENT_BREAKAWAY_OK
        outer = office_launch._KILL_ON_JOB_CLOSE | office_launch._BREAKAWAY_OK
        self.assertEqual(self.windows_options(inner, [outer, None]), {'creationflags': 0x09000000})

    def test_residual_kill_on_close_job_refuses(self):
        inner = office_launch._KILL_ON_JOB_CLOSE | office_launch._SILENT_BREAKAWAY_OK
        for residuals in ([office_launch._KILL_ON_JOB_CLOSE], [(office_launch._KILL_ON_JOB_CLOSE | office_launch._BREAKAWAY_OK), office_launch._KILL_ON_JOB_CLOSE]):
            with self.subTest(residuals=residuals), self.assertRaisesRegex(RuntimeError, 'supervisor job.*Office serve'):
                self.windows_options(inner, residuals)

    def test_outer_job_with_no_termination_limit_can_remain(self):
        inner = office_launch._KILL_ON_JOB_CLOSE | office_launch._SILENT_BREAKAWAY_OK
        outer = office_launch._KILL_ON_JOB_CLOSE | office_launch._BREAKAWAY_OK
        self.assertEqual(self.windows_options(inner, [outer, 0]), {'creationflags': 0x09000000})
        self.assertEqual(self.windows_options(inner, [0]), {'creationflags': 0x08000000})

    def test_probe_uses_base_python_and_reports_only_job_limits(self):
        base = 'base python.exe'
        with mock.patch.object(sys, '_base_executable', base), mock.patch.object(subprocess, 'run') as run:
            run.return_value.stdout = b'null\n'
            self.assertIsNone(office_launch._child_job_limits(123))
            args, kwargs = run.call_args
            self.assertEqual(args[0], [base, '-I', '-S', str(Path(office_launch.__file__).resolve()), '--job-limits'])
            self.assertEqual(kwargs['creationflags'], 123)
            self.assertLessEqual(kwargs['timeout'], 5)
            run.return_value.stdout = b'true'
            with self.assertRaises(ValueError):
                office_launch._child_job_limits(123)

    def test_windows_strict_job_refuses_before_launch(self):
        with self.assertRaisesRegex(RuntimeError, 'Windows job.*normal user terminal.*Office serve'):
            self.windows_options(office_launch._KILL_ON_JOB_CLOSE)

    def test_windows_query_failure_has_foreground_guidance(self):
        with mock.patch.object(sys, 'platform', 'win32'), mock.patch.object(
            subprocess, 'CREATE_NO_WINDOW', 0x08000000, create=True
        ), mock.patch.object(
            office_launch, '_current_job_limits', side_effect=OSError('access denied')
        ), self.assertRaisesRegex(RuntimeError, 'verify.*access denied.*Office serve'):
            office_launch.background_launch_options()

    def test_child_runs_after_launching_interpreter_exits(self):
        # Some Windows test runners deliberately forbid independent processes.
        if os.name == 'nt':
            limits = office_launch._current_job_limits()
            if limits is not None and limits & office_launch._KILL_ON_JOB_CLOSE and not limits & (
                office_launch._BREAKAWAY_OK | office_launch._SILENT_BREAKAWAY_OK
            ):
                with self.assertRaisesRegex(RuntimeError, 'Windows job'):
                    office_launch.background_launch_options()
                return
        child_script = """
from pathlib import Path
import sys
import time
gate, result = map(Path, sys.argv[1:])
end = time.monotonic() + 5
while not gate.exists() and time.monotonic() < end:
    time.sleep(.02)
if gate.exists():
    result.touch()
"""
        launcher_script = """
import subprocess
import sys
from office_launch import background_launch_options
subprocess.Popen([sys.executable, '-c', *sys.argv[1:]], stdin=subprocess.DEVNULL,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **background_launch_options())
"""
        with tempfile.TemporaryDirectory() as tmp:
            gate, result = Path(tmp) / 'launcher-exited', Path(tmp) / 'child-ran'
            subprocess.run(
                [sys.executable, '-c', launcher_script, child_script, str(gate), str(result)],
                cwd=Path(__file__).resolve().parent, check=True, timeout=10,
            )
            gate.touch()
            end = time.monotonic() + 5
            while not result.exists() and time.monotonic() < end:
                time.sleep(.02)
            self.assertTrue(result.exists(), 'child did not run after its launcher exited')


if __name__ == '__main__':
    unittest.main()
