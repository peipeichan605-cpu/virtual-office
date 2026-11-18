"""Configuration belongs to each installation; no developer machine paths or accounts."""
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import unicodedata
from security_keys import validate_security_keys

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = Path(os.environ.get('OFFICE_CONFIG', ROOT / '.office' / 'config.json')).expanduser()
PROVIDERS = {'codex','crouton','cursor','grok_bot','generic','hermes'}
CODEX_BACKGROUND_ID = r'bg-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
CODEX_BACKGROUND_MODEL = r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}'
GROK_SDK_ID = r'sdk-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
GROK_BOT_ID = r'[A-Za-z0-9_-]{1,128}'
GROK_SESSION_ID = r'[A-Za-z0-9_.:-]{0,256}'

def codex_targets(config):
    """Native session -> selected Office identity, including invited aliases."""
    result = {a['source_id']: a for a in config['agents'] if a['driver']=='codex'}
    selected = {a['id']: a for a in config['agents']}
    for ident, binding in config.get('command_bindings', {}).items():
        if binding['adapter']=='codex_app':
            previous=result.get(binding['session_id'])
            if previous and previous['id']!=ident:
                raise ValueError('Native session already selected')
            result[binding['session_id']]=selected[ident]
    return result

def agent_name(value):
    if not isinstance(value,str):
        raise ValueError('Invalid agent name')
    value=unicodedata.normalize('NFC',value).strip()
    if not 1<=len(value.encode('utf-16-le'))//2<=80 or any(ord(ch)<32 or ord(ch)==127 for ch in value):
        raise ValueError('Invalid agent name')
    return value

def load_config(path=DEFAULT_CONFIG):
    path = _private_config_path(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd) as file:
        info = os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or (hasattr(os, 'getuid') and info.st_uid != os.getuid())):
            raise ValueError('Office configuration must be a private owned file')
        data = json.load(file)
    data['config_path'] = str(path)
    data['_data_dir_setting'] = data.get('data_dir')
    directory=Path(data.get('data_dir') or path.parent).expanduser()
    if not directory.is_absolute():directory=path.parent/directory
    data['data_dir'] = str(directory.resolve())
    data.setdefault('host','127.0.0.1')
    if data['host'] != '127.0.0.1':
        raise ValueError('This edition listens only on loopback')
    if type(data.get('port')) is not int or not 1024 <= data['port'] <= 65535:
        raise ValueError('Port must be between 1024 and 65535')
    if not re.fullmatch(r'[A-Za-z0-9_-]{22,64}', data.get('topic','')):
        raise ValueError('Invalid private dashboard key')
    if data.get('feed') and data['feed'] != 'https://ntfy.sh/'+data['topic']:
        raise ValueError('Unsupported optional status feed')
    validate_security_keys(data)
    agents = data.get('agents',[])
    ids,names = set(),set()
    for a in agents:
        if a.get('provider') not in PROVIDERS or not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}',a.get('source_id','')):
            raise ValueError('Invalid selected agent')
        a['id'] = a['provider']+':'+a['source_id']
        a['name']=agent_name(a.get('name'))
        if a['id'] in ids or a['name'] in names:
            raise ValueError('Agent identities and display names must be unique')
        a.setdefault('driver','codex' if a['provider']=='codex' else 'crouton' if a['provider']=='crouton' else 'push')
        background = a['driver']=='codex_background' and a['provider']=='codex' and re.fullmatch(CODEX_BACKGROUND_ID,a['source_id'])
        grok_sdk = a['driver']=='grok_sdk' and a['provider']=='grok_bot' and re.fullmatch(GROK_SDK_ID,a['source_id'])
        if a['driver'] not in {'codex','crouton','push','hermes','cursor','codex_background','grok_sdk'} or (a['driver']!='push' and a['driver']!=a['provider'] and not background and not grok_sdk):
            raise ValueError('Invalid source driver')
        if a['driver']=='push' and not re.fullmatch(r'[0-9a-f]{64}',a.get('token_sha256','')):
            raise ValueError('Push agents require a scoped credential')
        ids.add(a['id']);names.add(a['name'])
    data['agents'] = agents
    for key,pattern in (('logged_out_agent_ids',r'(?:codex|crouton|cursor|grok_bot|generic|hermes):[A-Za-z0-9_.-]{1,160}'),('revoked_invitation_ids',r'[0-9a-f]{32}')):
        values=data.get(key,[])
        if not isinstance(values,list) or any(not isinstance(v,str) or not re.fullmatch(pattern,v) for v in values) or len(set(values))!=len(values):
            raise ValueError('Invalid Office membership retirement records')
    disabled=data.get('disabled_command_agents',[])
    if not isinstance(disabled,list) or any(not isinstance(ident,str) or ident not in ids for ident in disabled) or len(set(disabled))!=len(disabled):
        raise ValueError('Disabled command targets must be unique selected agents')
    bindings=data.get('command_bindings',{})
    if not isinstance(bindings,dict):raise ValueError('Invalid command bindings')
    selected={a['id']:a for a in agents}
    for ident,binding in bindings.items():
        agent=selected.get(ident)
        if not agent or not isinstance(binding,dict) or set(binding)!={'adapter','session_id'}:
            raise ValueError('Command target must be an explicitly selected agent')
        expected = {'codex_app':'codex','codex_background':'codex_background','hermes_acp':'hermes','cursor_cloud':'cursor','cursor_local':'cursor','grok_webhook':'push','grok_sdk':'grok_sdk'}.get(binding['adapter'])
        pattern = GROK_SDK_ID if binding['adapter']=='grok_sdk' else CODEX_BACKGROUND_ID if binding['adapter']=='codex_background' else r'agent-[A-Za-z0-9_.-]{1,140}' if binding['adapter']=='cursor_local' else r'bc-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}' if binding['adapter']=='cursor_cloud' else r'[A-Za-z0-9_.-]{1,160}' if binding['adapter']=='grok_webhook' else r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
        invited_codex=binding['adapter']=='codex_app' and agent['provider']=='codex' and agent['driver']=='push'
        if not expected or (not invited_codex and (agent['driver']!=expected or binding['session_id']!=agent['source_id'])) or not isinstance(binding['session_id'],str) or not re.fullmatch(pattern,binding['session_id']) or (binding['adapter']=='grok_webhook' and agent['provider']!='grok_bot'):
            raise ValueError('Command target must match the selected native session')
    codex_targets(data)
    adapters=data.get('command_adapters',{})
    if not isinstance(adapters,dict) or set(adapters)-{'codex_app','codex_background','hermes_acp','cursor_cloud','cursor_local','grok_webhook','grok_sdk'}:raise ValueError('Unknown command adapter')
    grok=adapters.get('grok_sdk',{})
    if not isinstance(grok,dict) or set(grok)-{'sessions','node_binary','client_version'} or not isinstance(grok.get('sessions',{}),dict):
        raise ValueError('Invalid Grok SDK settings')
    if 'node_binary' in grok and (not isinstance(grok['node_binary'],str) or not Path(grok['node_binary']).is_absolute()
            or len(grok['node_binary'])>4096 or any(ord(c)<32 or ord(c)==127 for c in grok['node_binary'])):
        raise ValueError('Grok SDK Node binary must be an absolute local path')
    if 'client_version' in grok and (not isinstance(grok['client_version'],str)
            or not re.fullmatch(r'[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}',grok['client_version'])):
        raise ValueError('Invalid Grok SDK protocol client version')
    grok_targets=set()
    for sid,item in grok.get('sessions',{}).items():
        agent=selected.get('grok_bot:'+sid) if isinstance(sid,str) else None
        if (not agent or agent['driver']!='grok_sdk' or not re.fullmatch(GROK_SDK_ID,sid)
                or not isinstance(item,dict) or set(item)!={'bot_id','native_session_id','account_fingerprint'}
                or not isinstance(item['bot_id'],str) or not re.fullmatch(GROK_BOT_ID,item['bot_id'])
                or not isinstance(item['native_session_id'],str) or not re.fullmatch(GROK_SESSION_ID,item['native_session_id'])
                or not isinstance(item['account_fingerprint'],str) or not re.fullmatch(r'[0-9a-f]{64}',item['account_fingerprint'])):
            raise ValueError('Grok SDK requires an explicitly selected native Bot session')
        target=(item['account_fingerprint'],item['bot_id'],item['native_session_id'])
        if target in grok_targets:raise ValueError('Grok SDK native session already selected')
        grok_targets.add(target)
        if bindings.get(agent['id'])!={'adapter':'grok_sdk','session_id':sid}:
            raise ValueError('Grok SDK settings require their selected binding')
    for ident,agent in selected.items():
        if agent['driver']=='grok_sdk' and (bindings.get(ident)!={'adapter':'grok_sdk','session_id':agent['source_id']}
                or agent['source_id'] not in grok.get('sessions',{})):
            raise ValueError('Grok SDK binding needs native session settings')
    codex=adapters.get('codex_app',{})
    if not isinstance(codex,dict) or set(codex)-{'binary'}:raise ValueError('Invalid command adapter settings')
    if 'binary' in codex and (not isinstance(codex['binary'],str) or not Path(codex['binary']).is_absolute()):
        raise ValueError('Command adapter binary must be an absolute path')
    background=adapters.get('codex_background',{})
    if not isinstance(background,dict) or set(background)-{'binary','sessions'} or not isinstance(background.get('sessions',{}),dict):
        raise ValueError('Invalid Codex background settings')
    if 'binary' in background and (not isinstance(background['binary'],str) or len(background['binary'])>4096
            or any(ord(c)<32 or ord(c)==127 for c in background['binary']) or not Path(background['binary']).is_absolute()):
        raise ValueError('Codex background binary must be an absolute path')
    background_root=Path(data['data_dir'])/'codex-background'
    for sid,item in background.get('sessions',{}).items():
        if not isinstance(sid,str):raise ValueError('Invalid Codex background session ID')
        agent=selected.get('codex:'+sid)
        if (not agent or agent['driver']!='codex_background' or not re.fullmatch(CODEX_BACKGROUND_ID,sid)
                or not isinstance(item,dict) or not {'cwd','lifecycle_file','sandbox','approval_policy'}<=set(item)
                or set(item)-{'cwd','lifecycle_file','sandbox','approval_policy','model'}):
            raise ValueError('Codex background requires a selected session')
        if any(not isinstance(item[key],str) or len(item[key])>4096
                or any(ord(c)<32 or ord(c)==127 for c in item[key]) or not Path(item[key]).is_absolute()
                for key in ('cwd','lifecycle_file')):
            raise ValueError('Codex background paths must be absolute')
        expected_file=background_root/(sid+'.json')
        if (item['lifecycle_file']!=str(expected_file) or background_root.is_symlink() or expected_file.is_symlink()):
            raise ValueError('Codex background lifecycle must belong to this installation')
        if item['sandbox']!='read-only' or item['approval_policy']!='on-request':
            raise ValueError('Codex background requires read-only and on-request policy')
        if 'model' in item and (not isinstance(item['model'],str) or not re.fullmatch(CODEX_BACKGROUND_MODEL,item['model'])):
            raise ValueError('Invalid Codex background model')
    for ident,agent in selected.items():
        if agent['driver']=='codex_background' and (bindings.get(ident)!={'adapter':'codex_background','session_id':agent['source_id']}
                or agent['source_id'] not in background.get('sessions',{})):
            raise ValueError('Codex background binding needs private lifecycle settings')
    hermes=adapters.get('hermes_acp',{})
    if not isinstance(hermes,dict) or set(hermes)-{'sessions'} or not isinstance(hermes.get('sessions',{}),dict):
        raise ValueError('Invalid Hermes settings')
    for sid, settings in hermes.get('sessions',{}).items():
        agent=selected.get('hermes:'+sid)
        if not agent or agent['driver']!='hermes':raise ValueError('Hermes session must be selected')
        if not isinstance(settings,dict) or set(settings)-{'command','cwd','home','timeout_seconds'}:
            raise ValueError('Invalid Hermes session settings')
        cmd=settings.get('command')
        if not isinstance(cmd,list) or not 1<=len(cmd)<=16 or not all(isinstance(v,str) and v and '\x00' not in v and len(v)<=4096 for v in cmd) or not Path(cmd[0]).is_absolute():
            raise ValueError('Hermes requires a local executable argv, never a shell string')
        for key in ('cwd','home'):
            if key=='home' and key not in settings:continue
            if not isinstance(settings.get(key),str) or not Path(settings[key]).is_absolute():
                raise ValueError('Hermes paths must be absolute')
        if type(settings.get('timeout_seconds',600)) is not int or not 30<=settings.get('timeout_seconds',600)<=3600:
            raise ValueError('Hermes timeout must be 30 to 3600 seconds')
    for ident,binding in bindings.items():
        if binding['adapter']=='hermes_acp' and binding['session_id'] not in hermes.get('sessions',{}):
            raise ValueError('Hermes command binding needs native launch settings')
    for adapter,provider in [('cursor_cloud','cursor'),('grok_webhook','grok_bot')]:
        settings=adapters.get(adapter,{})
        if not isinstance(settings,dict) or set(settings)-{'sessions'} or not isinstance(settings.get('sessions',{}),dict):
            raise ValueError('Invalid platform settings')
        for sid,item in settings.get('sessions',{}).items():
            if provider+':'+sid not in selected or not isinstance(item,dict) or set(item)!={'credential_file'} or not isinstance(item['credential_file'],str) or not Path(item['credential_file']).is_absolute():
                raise ValueError('Platform connection requires a selected agent and a local credential file')
        for binding in bindings.values():
            if binding['adapter']==adapter and binding['session_id'] not in settings.get('sessions',{}):
                raise ValueError('Platform command binding needs connection settings')
    local=adapters.get('cursor_local',{})
    if not isinstance(local,dict) or set(local)-{'sessions'} or not isinstance(local.get('sessions',{}),dict):
        raise ValueError('Invalid Cursor SDK settings')
    for sid,item in local.get('sessions',{}).items():
        if ('cursor:'+sid not in selected or not re.fullmatch(r'agent-[A-Za-z0-9_.-]{1,140}',sid)
                or not isinstance(item,dict) or not {'credential_file','cwd','model'}<=set(item)
                or set(item)-{'credential_file','cwd','model','tools','disallowed_tools','setting_sources'}):
            raise ValueError('Cursor SDK requires a selected session')
        if any(not isinstance(item[key],str) or not Path(item[key]).is_absolute() for key in ('credential_file','cwd')):
            raise ValueError('Cursor SDK paths must be absolute')
        if not isinstance(item['model'],str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,64}',item['model']):
            raise ValueError('Invalid Cursor SDK model')
        for key in ('tools','disallowed_tools','setting_sources'):
            if key in item and (not isinstance(item[key],list) or item[key]):
                raise ValueError('Cursor SDK route requires explicit chat-only policy')
    for binding in bindings.values():
        if binding['adapter']=='cursor_local' and binding['session_id'] not in local.get('sessions',{}):
            raise ValueError('Cursor SDK binding needs connection settings')
    data.setdefault('codex_db',str(Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))/'state_5.sqlite'))
    cursor_public_db = data.get('cursor_public_db')
    if cursor_public_db is not None and (
            not isinstance(cursor_public_db, str) or len(cursor_public_db) > 4096
            or any(ord(c) < 32 for c in cursor_public_db)
            or not Path(cursor_public_db).is_absolute()):
        raise ValueError('Cursor public storage override must be an absolute local path')
    if any(a['driver']=='crouton' for a in agents) and not data.get('crouton_root'):
        raise ValueError('Select the local Crouton runtime directory first')
    return data

def _private_config_path(path, create=False):
    path = Path(path).expanduser().absolute()
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('Office configuration must not use a symbolic link')
    if create:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = path.parent.resolve()
    info = parent.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077
            or (hasattr(os, 'getuid') and info.st_uid != os.getuid())):
        raise ValueError('Office configuration directory must be private and owned')
    if path.exists() and not path.is_file():
        raise ValueError('Office configuration must be a regular file')
    return parent / path.name


def save_config(path, config):
    path = _private_config_path(path, create=True)
    clean={k:v for k,v in config.items() if k!='config_path' and not k.startswith('_')}
    if '_data_dir_setting' in config:
        if config['_data_dir_setting'] is None:clean.pop('data_dir',None)
        else:clean['data_dir']=config['_data_dir_setting']
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(json.dumps(clean, ensure_ascii=False, indent=2)+'\n')
            file.flush()
            os.fsync(file.fileno())
        if path.is_symlink():
            raise ValueError('Office configuration changed to a symbolic link')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)

def initialize(path, port=4318):
    path=_private_config_path(path, create=True)
    if path.exists():
        raise ValueError('Configuration already exists')
    data={'version':1,'host':'127.0.0.1','port':port,'topic':secrets.token_urlsafe(32),
          'owner_key':secrets.token_urlsafe(32),'internal_hmac_key':secrets.token_urlsafe(32),
          'feed':None,'agents':[],'brand':'VIRTUAL OFFICE'}
    save_config(path,data)
    return load_config(path)
