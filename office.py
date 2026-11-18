#!/usr/bin/env python3
"""Portable, explicit enrollment and lifecycle CLI. Python standard library only."""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from configuration import DEFAULT_CONFIG, ROOT, PROVIDERS, initialize, load_config, save_config
from security_keys import owner_key, upgrade_security, upgraded_security


def request(config,path,body=None):
    req=urllib.request.Request('http://127.0.0.1:'+str(config['port'])+path,data=body,
                               headers={'X-Office-Key':owner_key(config),'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=3) as response:return json.load(response)


def require_offline(config):
    """Fail closed before security-sensitive config changes to a live service.

    Probe only this installation's configured loopback listeners. This check
    requires exclusive operator control of startup; it is not a lifecycle lock.
    """
    ports = {config['port']}
    mobile = config.get('mobile', {})
    if isinstance(mobile, dict) and mobile.get('port') is not None:
        ports.add(mobile['port'])
    for port in ports:
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError('Invalid Office listener port')
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
                connection.settimeout(.5)
                result = connection.connect_ex(('127.0.0.1', port))
        except OSError:
            raise ValueError('Could not verify Office is stopped; no configuration changed') from None
        if result != errno.ECONNREFUSED:
            raise ValueError('Stop Office before changing security or mobile settings; no configuration changed')


def main():
    parser=argparse.ArgumentParser(description='Local 3D office for explicitly selected agents')
    parser.add_argument('--config',default=str(DEFAULT_CONFIG))
    sub=parser.add_subparsers(dest='command',required=True)
    init=sub.add_parser('init');init.add_argument('--port',type=int,default=4318)
    for action in ('start','stop','status','url','serve'):sub.add_parser(action)
    add=sub.add_parser('enroll');add.add_argument('--provider',choices=sorted(PROVIDERS),required=True)
    add.add_argument('--source',required=True);add.add_argument('--name',required=True);add.add_argument('--credential-out')
    settings=sub.add_parser('configure');settings.add_argument('--codex-db');settings.add_argument('--cursor-public-db');settings.add_argument('--crouton-root');settings.add_argument('--crouton-workspace-root');settings.add_argument('--brand')
    hook=sub.add_parser('cursor-hooks');hook.add_argument('--credential',required=True);hook.add_argument('--output',required=True)
    command=sub.add_parser('enable-commands');command.add_argument('--agent',required=True);command.add_argument('--adapter',choices=['codex_app','agent_inbox'],default='codex_app');command.add_argument('--binary')
    disable=sub.add_parser('disable-commands');disable.add_argument('--agent',required=True)
    hermes=sub.add_parser('connect-hermes',help='Create or load one native Hermes ACP session and enroll it')
    hermes.add_argument('--name',required=True);hermes.add_argument('--workspace',required=True)
    hermes.add_argument('--session');hermes.add_argument('--hermes-home');hermes.add_argument('--binary')
    hermes.add_argument('--launch-file',help='Optional local JSON argv for an existing Hermes ACP launcher')
    sdk=sub.add_parser('setup-cursor-sdk',help='Install the optional pinned Cursor SDK in this installation private environment')
    sdk.add_argument('--python',help='Existing healthy Python 3.10+ executable')
    sdk.add_argument('--download-python',action='store_true',help='Use installed uv to download an isolated Python if none is usable')
    platforms=sub.add_parser('setup-platforms',help='Prepare and verify all supported platform runtimes before first start; never logs in or creates Agents')
    platforms.add_argument('--check',action='store_true',help='Read-only runtime verification; do not install anything')
    platforms.add_argument('--python',help='Existing healthy Python 3.10+ for the managed Cursor SDK environment')
    platforms.add_argument('--download-python',action='store_true',help='Use installed uv to download an isolated Python if none is usable')
    sub.add_parser('security-upgrade',help='Separate owner/feed/internal credentials and disable legacy external feed; restart required')
    mobile=sub.add_parser('mobile-configure',help='Configure the private loopback phone gateway; Tailscale policy is configured separately')
    mobile.add_argument('--origin',required=True);mobile.add_argument('--identity',required=True);mobile.add_argument('--port',type=int)
    mobile.add_argument('--enabled',action='store_true')
    args=parser.parse_args();path=Path(args.config).expanduser().absolute()
    if args.command=='init':
        initialize(path,args.port);print(json.dumps({'initialized':True,'config':str(path),'agents':0}));return
    config=load_config(path)
    if args.command=='security-upgrade':
        require_offline(config)
        migrated=upgrade_security(config);save_config(path,migrated)
        print(json.dumps({'upgraded':upgraded_security(migrated),'legacy_feed_enabled':bool(migrated.get('feed')),'restart_required':True}));return
    if args.command=='mobile-configure':
        from mobile_gateway import settings as mobile_settings
        require_offline(config)
        if not upgraded_security(config):raise ValueError('Run security-upgrade first')
        config['mobile']={'enabled':args.enabled,'public_origin':args.origin,'identity':args.identity,'port':args.port or config['port']+1}
        mobile_settings(config);save_config(path,config)
        print(json.dumps({'mobile_configured':True,'enabled':args.enabled,'loopback_only':True,'restart_required':True}));return
    if args.command=='setup-cursor-sdk':
        from cursor_sdk_setup import setup_environment
        result=setup_environment(config,args.python,args.download_python,
                                 on_started=lambda data:print(json.dumps(data),flush=True))
        print(json.dumps(result),flush=True);return
    if args.command=='setup-platforms':
        from setup_platforms import prepare_platforms
        result=prepare_platforms(config,check=args.check,python=args.python,
                                 download_python=args.download_python,
                                 on_started=lambda data:print(json.dumps(data),flush=True))
        print(json.dumps(result),flush=True)
        if not result['runtimes_ready']:raise SystemExit(1)
        return
    if args.command=='connect-hermes':
        from hermes_acp import connect_session, AcpError
        from configuration import agent_name
        name=agent_name(args.name);workspace=Path(args.workspace).expanduser().resolve()
        if any(a['name']==name for a in config['agents']):raise ValueError('Display name already selected')
        if not workspace.is_dir():raise ValueError('Workspace must be an existing directory')
        if args.session and any(a['id']=='hermes:'+args.session for a in config['agents']):raise ValueError('Hermes session already selected')
        binary=args.binary or shutil.which('hermes')
        if args.launch_file:
            launch=json.loads(Path(args.launch_file).expanduser().read_text())
        else:
            if not binary:raise ValueError('Select the installed Hermes binary with --binary')
            launch=[str(Path(binary).expanduser().absolute()),'acp']
        if not isinstance(launch,list) or not launch or not all(isinstance(v,str) and v and '\x00' not in v for v in launch) or not Path(launch[0]).is_absolute() or not os.access(launch[0],os.X_OK):
            raise ValueError('Invalid local Hermes launcher argv')
        settings={'command':launch,'cwd':str(workspace)}
        if args.hermes_home:
            home=Path(args.hermes_home).expanduser().resolve()
            if not home.is_dir():raise ValueError('Hermes home must already exist')
            settings['home']=str(home)
        run=Path(config['data_dir'])/'hermes'/'connections'/('connect-'+uuid.uuid4().hex[:12])
        print(json.dumps({'state':'connecting','run_directory':str(run)}),flush=True)
        try:sid=connect_session(settings,run,args.session)
        except AcpError as error:raise ValueError(error.code)
        ident='hermes:'+sid
        config['agents'].append({'id':ident,'provider':'hermes','driver':'hermes','source_id':sid,'name':name})
        config.setdefault('command_bindings',{})[ident]={'adapter':'hermes_acp','session_id':sid}
        config.setdefault('command_adapters',{}).setdefault('hermes_acp',{}).setdefault('sessions',{})[sid]=settings
        check=path.with_name('hermes-check.json')
        try:save_config(check,config);load_config(check)
        finally:check.unlink(missing_ok=True)
        save_config(path,config)
        print(json.dumps({'enrolled':ident,'selected_count':len(config['agents']),'same_native_session':True,'restart_required':True}));return
    if args.command in {'enable-commands','disable-commands'}:
        if not any(a['id']==args.agent for a in config['agents']):raise ValueError('Select an enrolled agent')
        disabled=set(config.get('disabled_command_agents',[]))
        if args.command=='disable-commands':
            config.setdefault('command_bindings',{}).pop(args.agent,None)
            disabled.add(args.agent)
        elif args.adapter=='agent_inbox':
            config.setdefault('command_bindings',{}).pop(args.agent,None)
            disabled.discard(args.agent)
        else:
            from command_adapters import CodexAppAdapter
            agent=next((a for a in config['agents'] if a['id']==args.agent and a['driver']=='codex'),None)
            if not agent:raise ValueError('Select an enrolled native Codex agent')
            if Path(config['codex_db']).resolve()!=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')),'state_5.sqlite').resolve():
                raise ValueError('Codex observation and command runtime must use the same local home')
            settings={'binary':args.binary} if args.binary else {}
            adapter=CodexAppAdapter(settings)
            if not adapter.available():raise ValueError('Installed Codex App queue is unavailable')
            config.setdefault('command_adapters',{})['codex_app']=settings
            config.setdefault('command_bindings',{})[args.agent]={'adapter':args.adapter,'session_id':agent['source_id']}
            disabled.discard(args.agent)
        config['disabled_command_agents']=sorted(disabled)
        check=path.with_name('commands-check.json')
        try:save_config(check,config);load_config(check)
        finally:check.unlink(missing_ok=True)
        save_config(path,config)
        print(json.dumps({'agent':args.agent,'commands_enabled':args.command=='enable-commands','restart_required':True}));return
    if args.command=='url':
        print('http://127.0.0.1:'+str(config['port'])+'/office/#'+owner_key(config));return
    if args.command=='configure':
        if args.cursor_public_db:
            args.cursor_public_db = str(Path(args.cursor_public_db).expanduser().absolute())
        for key in ('codex_db','cursor_public_db','crouton_root','crouton_workspace_root','brand'):
            if getattr(args,key,None):config[key]=getattr(args,key)
        save_config(path,config);print('Local settings saved. Restart Office to apply.');return
    if args.command=='enroll':
        if args.provider=='hermes':raise ValueError('Use connect-hermes to bind a native Hermes session')
        ident=args.provider+':'+args.source
        if any(a['id']==ident or a['name']==args.name for a in config['agents']):raise ValueError('Agent or display name already selected')
        agent={'id':ident,'provider':args.provider,'source_id':args.source,'name':args.name,
               'driver':args.provider if args.provider in {'codex','crouton'} else 'push'}
        credential=None
        if agent['driver']=='push':
            if not args.credential_out:raise ValueError('Push agents require --credential-out for their own local credential')
            destination=Path(args.credential_out).expanduser().resolve()
            if destination.exists():raise ValueError('Credential file already exists')
            token=secrets.token_urlsafe(32);agent['token_sha256']=hashlib.sha256(token.encode()).hexdigest()
            credential={'endpoint':'http://127.0.0.1:'+str(config['port'])+'/api/agents/events','agent_id':ident,'source_id':args.source,'token':token}
        config['agents'].append(agent)
        # Validate before replacing the real configuration.
        check=path.with_name('enrollment-check.json')
        try:save_config(check,config);load_config(check)
        finally:check.unlink(missing_ok=True)
        if credential:
            destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            fd=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            with os.fdopen(fd,'w') as file:json.dump(credential,file,indent=2)
        save_config(path,config)
        print(json.dumps({'enrolled':ident,'selected_count':len(config['agents']),'restart_required':True,'credential_file':str(destination) if credential else None}));return
    if args.command=='cursor-hooks':
        credential=Path(args.credential).expanduser().resolve();data=json.loads(credential.read_text())
        if not data['agent_id'].startswith('cursor:'):raise ValueError('A selected Cursor credential is required')
        target=Path(args.output).expanduser().resolve();hooks=json.loads(target.read_text()) if target.exists() else {'version':1,'hooks':{}}
        command=shlex.join([sys.executable,str(ROOT/'adapters/cursor_hook.py'),'--credential',str(credential)])
        for name in ('beforeSubmitPrompt','afterAgentResponse','stop'):
            values=hooks.setdefault('hooks',{}).setdefault(name,[])
            if not any(v.get('command')==command for v in values):values.append({'command':command,'timeout':5})
        target.parent.mkdir(parents=True,exist_ok=True);target.write_text(json.dumps(hooks,indent=2)+'\n')
        print(json.dumps({'hooks_file':str(target),'events':3,'scope':data['source_id']}));return
    if args.command=='status':
        try:
            work=request(config,'/api/office/work');health=request(config,'/health')
            print(json.dumps({'state':'running','selected':sum(not a.get('is_subagent',False) for a in work['agents']),
                              'subagents':work.get('subagent_count',0),'local_ok':health['local_ok'],'pid':health.get('pid')}))
        except Exception:print(json.dumps({'state':'unavailable','selected':len(config['agents'])}))
        return
    if args.command=='stop':
        print(json.dumps(request(config,'/api/office/shutdown',b'{}')))
        return
    if args.command=='serve':
        from cursor_sdk_setup import service_python
        binary=service_python(config)
        os.execv(binary,[binary,str(ROOT/'dashboard.py'),'--config',str(path)])
    if args.command=='start':
        try:
            request(config,'/api/office/work');print('Office already running.');return
        except Exception:pass
        run_id='office-'+time.strftime('%Y%m%dT%H%M%S',time.gmtime())+'-'+uuid.uuid4().hex[:6]
        run=Path(config['data_dir'])/'runs'/run_id;run.mkdir(parents=True,mode=0o700)
        config['run_id']=run_id;config['run_dir']=str(run);save_config(path,config)
        (run/'request.json').write_text(json.dumps({'action':'start','run_id':run_id,'config':str(path)}))
        from cursor_sdk_setup import service_python
        binary=service_python(config)
        with (run/'stdout.log').open('ab',buffering=0) as log:
            child=subprocess.Popen([binary,str(ROOT/'dashboard.py'),'--config',str(path)],cwd=ROOT,
                                   stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        print(json.dumps({'run_id':run_id,'pid':child.pid,'log':str(run/'stdout.log'),'stop_command':'python3 office.py --config '+shlex.quote(str(path))+' stop'}),flush=True)
        for _ in range(40):
            if child.poll() is not None:raise RuntimeError('Office exited; inspect the recorded stdout log')
            try:request(config,'/api/office/work');print('Office ready.');return
            except Exception:time.sleep(.1)
        raise RuntimeError('Office did not become ready within the startup check')

if __name__=='__main__':
    try:main()
    except (ValueError,OSError,RuntimeError) as error:raise SystemExit(str(error))
