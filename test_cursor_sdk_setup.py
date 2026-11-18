import json
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import cursor_sdk_setup as setup


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='office sdk setup ')
        self.base = Path(self.temporary.name)
        self.config = {'data_dir': str(self.base / 'private office')}
        self.root = setup.runtime_root(self.config)

    def tearDown(self):
        self.temporary.cleanup()

    def marker(self):
        environment = self.root / 'environments' / ('env-' + 'a' * 32)
        environment.mkdir(parents=True)
        (environment / 'pyvenv.cfg').write_text('home = /fixture\n')
        marker = {'managed_by': setup.MANAGER, 'sdk_version': setup.SDK_VERSION,
                  'environment': environment.relative_to(self.root).as_posix()}
        (self.root / 'runtime.json').write_text(json.dumps(marker))
        return environment

    def test_core_without_sdk_environment_keeps_original_interpreter_without_probing(self):
        with patch.object(setup, 'probe_interpreter') as probe:
            self.assertEqual(setup.service_python(self.config, '/original/python'), '/original/python')
            probe.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_owned_valid_environment_preserves_venv_path_and_reuses_without_install(self):
        environment = self.marker()
        expected = str(setup.venv_python(environment))
        with patch.object(setup, 'probe_interpreter', return_value={'version':[3,12,0]}), patch.object(setup.subprocess, 'Popen') as launch:
            self.assertEqual(setup.service_python(self.config, '/fallback'), expected)
            result = setup.setup_environment(self.config)
        self.assertTrue(result['reused'])
        self.assertEqual(result['python'], expected)
        launch.assert_not_called()

    def test_broken_optional_env_does_not_prevent_core_launch(self):
        self.marker()
        with patch.object(setup, 'probe_interpreter', return_value=None):
            self.assertEqual(setup.service_python(self.config, '/core/python'), '/core/python')

    def test_external_or_unknown_runtime_metadata_cannot_redirect_launcher_or_be_overwritten(self):
        self.root.mkdir(parents=True)
        marker = self.root / 'runtime.json'
        original = json.dumps({'managed_by':'another-app','environment':'../../external'})
        marker.write_text(original)
        with patch.object(setup, 'probe_interpreter') as probe:
            self.assertEqual(setup.service_python(self.config, '/core'), '/core')
            with self.assertRaisesRegex(ValueError, 'existing files preserved'):
                setup.setup_environment(self.config)
            probe.assert_not_called()
        self.assertEqual(marker.read_text(), original)

    def test_managed_marker_path_escape_and_symlink_environment_are_not_launchable(self):
        env = self.marker()
        marker = self.root / 'runtime.json'
        data = json.loads(marker.read_text());data['environment']='../../outside'
        marker.write_text(json.dumps(data))
        with patch.object(setup, 'probe_interpreter') as probe:
            self.assertEqual(setup.service_python(self.config, '/core'), '/core')
            probe.assert_not_called()
        data['environment']=env.relative_to(self.root).as_posix();marker.write_text(json.dumps(data))
        backup=env.with_name('original');env.rename(backup);env.symlink_to(backup, target_is_directory=True)
        with patch.object(setup, 'probe_interpreter') as probe:
            self.assertEqual(setup.service_python(self.config, '/core'), '/core')
            probe.assert_not_called()

    def runner(self, fail_pip=False):
        commands = []
        state = {'installed':False}
        def probe(binary, sdk=False):
            if sdk:
                return {'version':[3,12,0]} if state['installed'] else None
            return {'version':[3,12,0]} if str(binary).startswith('/healthy/') or 'managed-python' in str(binary) else None
        def launch(command, **kwargs):
            commands.append(command)
            if 'venv' in command:
                env=Path(command[-1]);env.mkdir(parents=True);(env/'pyvenv.cfg').write_text('fixture')
            if 'python' in command and 'install' in command and '--install-dir' in command:
                managed=Path(command[command.index('--install-dir')+1]);p=managed/'cpython-test/bin/python3.12';p.parent.mkdir(parents=True);p.touch()
            code=1 if fail_pip and 'pip' in command else 0
            if 'pip' in command and not code:state['installed']=True
            child=unittest.mock.Mock(pid=424242)
            child.wait.return_value=code
            return child
        return commands, probe, launch

    def test_fresh_install_publishes_only_after_verification_and_never_clears_existing_env(self):
        unrelated=self.base/'unrelated venv';unrelated.mkdir();(unrelated/'keep').write_text('untouched')
        commands,probe,launch=self.runner();starts=[]
        with patch.object(setup, 'probe_interpreter',side_effect=probe), patch.object(setup.subprocess,'Popen',side_effect=launch):
            result=setup.setup_environment(self.config,python='/healthy/python3',on_started=starts.append)
        self.assertEqual(result['state'],'ready');self.assertFalse(result['reused'])
        self.assertTrue(starts[0]['run_id'].startswith('setup-'))
        self.assertEqual((unrelated/'keep').read_text(),'untouched')
        self.assertFalse(any('--clear' in command or '--system' in command for command in commands))
        pip=next(command for command in commands if 'pip' in command)
        self.assertIn('--isolated',pip);self.assertEqual(pip[pip.index('--index-url')+1],'https://pypi.org/simple')
        marker=json.loads((self.root/'runtime.json').read_text())
        self.assertEqual(marker['sdk_version'],'1.3.1')
        self.assertIn(' ',result['python'],'paths with spaces must remain one argv value')
        self.assertEqual((self.root/'runtime.json').stat().st_mode & 0o777,0o600)

    def test_failed_install_preserves_previous_runtime_marker_and_environment(self):
        environment=self.marker();keep=environment/'sentinel';keep.write_text('keep')
        before=(self.root/'runtime.json').read_text()
        commands,probe,launch=self.runner(fail_pip=True)
        with patch.object(setup,'probe_interpreter',side_effect=probe),patch.object(setup.subprocess,'Popen',side_effect=launch):
            with self.assertRaisesRegex(RuntimeError,'setup failed'):
                setup.setup_environment(self.config,python='/healthy/python3')
        self.assertEqual((self.root/'runtime.json').read_text(),before)
        self.assertEqual(keep.read_text(),'keep')
        statuses=list((self.root/'setup-runs').glob('*/status.json'))
        self.assertEqual(json.loads(statuses[-1].read_text())['state'],'failed')

    def test_python_download_is_explicit_and_never_registers_global_executables(self):
        commands,probe,launch=self.runner()
        with patch.object(setup,'probe_interpreter',side_effect=probe),patch.object(setup.subprocess,'Popen',side_effect=launch),patch.object(setup.shutil,'which',side_effect=lambda name:'/fixture/uv' if name=='uv' else None):
            with self.assertRaisesRegex(ValueError,'healthy Python'):
                setup.setup_environment(self.config,python='/broken/python')
            self.assertFalse(commands)
            result=setup.setup_environment(self.config,python='/broken/python',download_python=True)
        self.assertEqual(result['state'],'ready')
        uv=commands[0];self.assertIn('--no-bin',uv);self.assertIn('--no-registry',uv)
        self.assertNotIn('--default',uv);self.assertNotIn('--force',uv)

    def test_health_probe_uses_isolated_imports_without_starting_bridge_or_reading_auth(self):
        fake=subprocess.CompletedProcess([],0,'{"version":[3,12,0],"executable":"/test/python"}\n','')
        with patch.object(setup.subprocess,'run',return_value=fake) as run:
            self.assertTrue(setup.probe_interpreter('/test/python',sdk=True))
        args=run.call_args.args[0]
        self.assertEqual(args[:3],['/test/python','-I','-c'])
        self.assertIn('import importlib.metadata,cursor_sdk',args[3])
        self.assertNotIn('Agent.create',args[3]);self.assertNotIn('launch_bridge',args[3])
        with patch.object(setup.subprocess,'run',return_value=subprocess.CompletedProcess([],1,'','broken import')):
            self.assertIsNone(setup.probe_interpreter('/broken/python'))

    def test_windows_sdk_preflight_uses_interpreter_abi_not_the_host_cpu(self):
        self.assertTrue(setup.sdk_platform_supported('win32', 'win-amd64'))
        self.assertFalse(setup.sdk_platform_supported('win32', 'win-arm64'))
        self.assertFalse(setup.sdk_platform_supported('win32', 'win32'))
        self.assertTrue(setup.sdk_platform_supported('darwin', 'macosx-arm64'))
        for tag, supported in [('win-amd64', True), ('win-arm64', False), ('win32', False)]:
            with self.subTest(tag=tag):
                data = {'version': [3, 12, 1], 'system': 'win32', 'platform': tag}
                fake = subprocess.CompletedProcess([], 0, json.dumps(data), '')
                with patch.object(setup.subprocess, 'run', return_value=fake):
                    self.assertEqual(bool(setup.probe_interpreter('/fixture/python')), supported)

    def test_windows_discovery_includes_existing_launcher_paths_without_auto_install(self):
        current = r'C:\Office Python\python.exe'
        installed = r'C:\User Tools\Python312\python.exe'
        result = subprocess.CompletedProcess([], 0,
            ' -V:3.13 * ' + current + '\n -V:3.12 ' + installed + '\nignored text\n', '')
        binaries = {'python': current, 'py': r'C:\Windows\py.exe'}
        with patch.object(setup.sys, 'platform', 'win32'), patch.object(setup.sys, 'executable', current), \
             patch.object(setup.shutil, 'which', side_effect=binaries.get), \
             patch.dict(os.environ, {'PYLAUNCHER_ALLOW_INSTALL': '1'}), \
             patch.object(setup.subprocess, 'run', return_value=result) as run:
            self.assertEqual(setup.interpreter_candidates(), [current, installed])
        self.assertEqual(run.call_args.args[0], [binaries['py'], '-0p'])
        environment = run.call_args.kwargs['env']
        self.assertEqual(environment['PYTHON_MANAGER_AUTOMATIC_INSTALL'], '0')
        self.assertNotIn('PYLAUNCHER_ALLOW_INSTALL', environment)

    def test_launcher_failure_keeps_existing_candidates_and_explicit_python_skips_discovery(self):
        with patch.object(setup.sys, 'platform', 'win32'), \
             patch.object(setup.shutil, 'which', side_effect=lambda name: '/fixture/py' if name == 'py' else None), \
             patch.object(setup.subprocess, 'run', side_effect=subprocess.TimeoutExpired('py', 15)):
            self.assertEqual(setup.interpreter_candidates(), [sys.executable])
        with patch.object(setup.shutil, 'which') as lookup, patch.object(setup.subprocess, 'run') as run:
            self.assertEqual(setup.interpreter_candidates('/selected/python'), ['/selected/python'])
        lookup.assert_not_called(); run.assert_not_called()

    def test_windows_run_records_offer_a_usable_process_tree_stop_command(self):
        commands, probe, launch = self.runner()
        with patch.object(setup.sys, 'platform', 'win32'), \
             patch.object(setup, 'probe_interpreter', side_effect=probe), \
             patch.object(setup.subprocess, 'Popen', side_effect=launch):
            result = setup.setup_environment(self.config, python='/healthy/python3')
        record = json.loads((self.root / 'setup-runs' / result['run_id'] / 'status.json').read_text())
        self.assertEqual(record['stop_target'], 'taskkill /PID ' + str(os.getpid()) + ' /T /F')
        self.assertEqual(record['child_stop_target'], 'taskkill /PID 424242 /T /F')
        with patch.object(setup.sys, 'platform', 'darwin'):
            self.assertEqual(setup.stop_target(1234), '/bin/kill -TERM 1234')

    def test_serve_launcher_uses_verified_optional_env_and_retains_existing_config_path(self):
        import office
        config_path=(self.base/'private config.json').resolve()
        with patch.object(sys,'argv',['office.py','--config',str(config_path),'serve']),patch.object(office,'load_config',return_value=self.config),patch.object(setup,'service_python',return_value='/private/sdk env/bin/python'),patch.object(office.os,'execv') as execute:
            office.main()
        execute.assert_called_once_with('/private/sdk env/bin/python',['/private/sdk env/bin/python',str(office.ROOT/'dashboard.py'),'--config',str(config_path)])

    def test_start_launcher_preserves_core_default_and_does_not_enroll_or_install(self):
        import office
        config_path=(self.base/'private config.json').resolve()
        child=unittest.mock.Mock(pid=424243);child.poll.return_value=None
        with patch.object(sys,'argv',['office.py','--config',str(config_path),'start']),patch.object(office,'load_config',return_value=self.config),patch.object(office,'save_config') as save,patch.object(office,'request',side_effect=[OSError('not running'),{'agents':[]}]),patch.object(office.subprocess,'Popen',return_value=child) as launch,patch.object(setup,'setup_environment') as install,contextlib.redirect_stdout(io.StringIO()):
            office.main()
        argv=launch.call_args.args[0]
        self.assertEqual(argv,[sys.executable,str(office.ROOT/'dashboard.py'),'--config',str(config_path)])
        self.assertNotIn('agents',self.config)
        install.assert_not_called();save.assert_called_once()


if __name__=='__main__':
    unittest.main()
