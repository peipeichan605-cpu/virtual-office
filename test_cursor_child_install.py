import importlib.util
import json
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('cursor_install',Path(__file__).parent/'adapters/install_cursor_children.py')
install=importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)


class CursorChildInstallTests(unittest.TestCase):
    def test_preserves_existing_security_hooks_and_installation_is_idempotent(self):
        old={'version':1,'custom':'keep','hooks':{'subagentStart':[{'command':'existing-policy','failClosed':True}],
             'afterAgentThought':[{'command':'existing-private-policy'}]}}
        value=install.merged(old,'/tmp/fixture-office/observers',python='/usr/bin/python3',client='/tmp/fixture-office/cursor_children.py')
        self.assertEqual(value['hooks']['subagentStart'][0],old['hooks']['subagentStart'][0])
        self.assertEqual(value['hooks']['afterAgentThought'],old['hooks']['afterAgentThought'])
        self.assertEqual(value['custom'],'keep')
        self.assertEqual(install.merged(value,'/tmp/fixture-office/observers',python='/usr/bin/python3',client='/tmp/fixture-office/cursor_children.py'),value)
        self.assertEqual(len(old['hooks']['subagentStart']),1)

    def test_one_installation_hook_observes_directory_instead_of_hardcoded_session(self):
        value=install.merged({'version':1,'hooks':{}},'/tmp/Office with space/observers')
        self.assertEqual(set(value['hooks']),set(install.EVENTS))
        for rows in value['hooks'].values():
            self.assertEqual(len(rows),1)
            self.assertIn('--observer-dir',rows[0]['command'])
            argv=shlex.split(rows[0]['command'])
            self.assertEqual(argv[argv.index('--observer-dir')+1],str(Path('/tmp/Office with space/observers').resolve()))
            self.assertFalse(rows[0]['failClosed'])
            self.assertNotIn('token',rows[0]['command'])

    def test_unknown_hook_schema_rejected_without_replacing_configuration(self):
        for value in ({'version':2,'hooks':{}},{'version':1,'hooks':[]},{'version':1,'hooks':{'stop':{}}}):
            with self.assertRaises(ValueError):install.merged(value,'/tmp/fixture')

    def test_upgrade_removes_only_exact_own_legacy_observer_without_reading_credentials(self):
        legacy=Path(install.__file__).with_name('cursor_hook.py')
        own='/tmp/selected parent.json'
        same={'command':shlex.join(['/usr/bin/python3',str(legacy),'--credential',own])}
        other={'command':shlex.join(['/usr/bin/python3',str(legacy),'--credential','/tmp/other.json'])}
        policy={'command':'security-policy','failClosed':True}
        wrapper={'command':same['command']+' && security-policy'}
        policy_wrapper={'command':shlex.join(['/path/security-wrapper',str(legacy),'--credential',own]),'failClosed':True}
        existing={'version':1,'hooks':{'afterAgentResponse':[same,other,policy,wrapper,policy_wrapper]}}
        value=install.merged(existing,'/tmp/observer',legacy_credential_path=own)
        rows=value['hooks']['afterAgentResponse']
        self.assertNotIn(same,rows)
        for preserved in (other,policy,wrapper,policy_wrapper):self.assertIn(preserved,rows)
        self.assertEqual(existing['hooks']['afterAgentResponse'],[same,other,policy,wrapper,policy_wrapper])
        self.assertEqual(install.merged(value,'/tmp/observer',legacy_credential_path=own),value)

    def test_concurrent_settings_change_is_preserved_and_install_aborts(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            observer=root/'observer.json'
            observer.write_text(json.dumps({'provider':'cursor','observer_id':'fixture'}))
            hooks=root/'hooks.json'
            hooks.write_text(json.dumps({'version':1,'hooks':{}}))
            changed={'version':1,'hooks':{'preToolUse':[{'command':'new-policy','failClosed':True}]}}
            merge=install.merged
            def concurrent_change(*args,**kwargs):
                hooks.write_text(json.dumps(changed))
                return merge(*args,**kwargs)
            with patch.object(install,'merged',side_effect=concurrent_change):
                with self.assertRaisesRegex(ValueError,'configuration_changed'):
                    install.install(observer,hooks)
            self.assertEqual(json.loads(hooks.read_text()),changed)
            self.assertEqual(list(root.glob('*.office-tmp-*')),[])


if __name__=='__main__':unittest.main()
