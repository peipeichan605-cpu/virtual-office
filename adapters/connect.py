#!/usr/bin/env python3
"""Attach this enrolled agent's native entry point, using only its own credential."""
import argparse
from itertools import islice
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid


REJOIN_NEXT_STEP = ('目前沒有可用的 OFFICE 憑證。請由擁有人在 OFFICE 產生新邀請，'
    '再於你選定的原聊天執行新接入指引及首次回報。'
    '不要還原或使用已登出憑證，不要改接其他平台或啟動等待工具。')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):return None


def missing_credential_result(path):
    """Inspect only sibling names; a retired credential is never a fallback."""
    logged_out = False
    try:
        siblings = islice(path.parent.iterdir(), 256) if path.name == 'credential.json' else ()
        for sibling in siblings:
            if (re.fullmatch(r'credential\.logged-out-[0-9a-f]{32}\.json', sibling.name)
                    and not sibling.is_symlink() and sibling.is_file()):
                logged_out = True
                break
    except OSError:
        pass
    return {
        'connected': False, 'persistent': False, 'child_observer_connected': False,
        'requires_rejoin': True,
        'reason': 'credential_logged_out' if logged_out else 'credential_missing',
        'next_step': REJOIN_NEXT_STEP,
    }


def rejected_credential_result(result):
    return dict(result, connected=False, persistent=False, requires_rejoin=True,
                next_step=REJOIN_NEXT_STEP)


def native_request(credential, payload):
    url=urllib.parse.urlsplit(credential['endpoint'])
    if url.scheme!='http' or url.hostname not in {'127.0.0.1','localhost'} or url.path not in {'/api/agents/events','/api/agents/inbox'} or url.username or url.password or url.query or url.fragment:
        raise ValueError('Invalid local credential')
    endpoint=urllib.parse.urlunsplit((url.scheme,url.netloc,'/api/agents/connect','',''))
    request=urllib.request.Request(endpoint,data=json.dumps(payload).encode(),method='POST',headers={
        'Content-Type':'application/json','X-Agent-Id':credential['agent_id'],'Authorization':'Bearer '+credential['token']})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    try:
        with opener.open(request,timeout=20) as response:return json.load(response)
    except urllib.error.HTTPError as error:
        try:reason=json.load(error).get('error','connection_unavailable')
        except (ValueError,AttributeError):reason='connection_unavailable'
        result = {'connected':False,'reason':reason}
        if error.code in {401, 403, 410}:
            return rejected_credential_result(dict(result, status=error.code))
        return result


def auto_connect(credential, environ=None):
    environ=os.environ if environ is None else environ
    if credential['agent_id'].startswith('cursor:'):
        return {
            'connected': False, 'persistent': False,
            'reason': 'cursor_local_background_unavailable', 'requires_rejoin': False,
            'observation_setup_separate': True,
            'next_step': '這個本機 Cursor 聊天尚未有原生背景指令入口，'
                '不代表 OFFICE 入場或公開同步失敗。入場以 joined 及首次回報為準；'
                '請按本次邀請完成父子 Agent 的公開同步設定。'
                '不要試接其他平台，也不要用等待工具冒充持續收訊。',
        }
    if not credential['agent_id'].startswith('codex:'):
        return {'connected':False,'reason':'native_setup_required'}
    sid=environ.get('CODEX_THREAD_ID')
    try:
        if str(uuid.UUID(sid))!=sid:raise ValueError()
    except (ValueError,TypeError,AttributeError):
        return {'connected':False,'reason':'native_session_identity_required'}
    return native_request(credential,{'adapter':'codex_app','session_id':sid,'workspace_path':str(Path.cwd())})


def cursor_children(credential, hooks_file, credential_path=None):
    # Cursor local observation is separate from background command delivery.
    if not credential.get('agent_id','').startswith('cursor:'):
        return {'child_observer_connected':False,'reason':'cursor_agent_required'}
    base=urllib.parse.urlsplit(credential['endpoint'])
    if (base.scheme!='http' or base.hostname not in {'127.0.0.1','localhost'} or base.username or base.password
            or base.query or base.fragment or base.path!='/api/agents/events'):
        raise ValueError('Invalid local credential')
    url=urllib.parse.urlunsplit((base.scheme,base.netloc,'/api/agents/observers/pending','',''))
    request=urllib.request.Request(url,data=b'{}',method='POST',headers={'Content-Type':'application/json',
        'X-Agent-Id':credential['agent_id'],'Authorization':'Bearer '+credential['token']})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    try:
        with opener.open(request,timeout=5) as response:pending=json.load(response)
    except urllib.error.HTTPError as error:
        result = {'child_observer_connected':False,'reason':'observer_setup_rejected','status':error.code}
        return rejected_credential_result(result) if error.code in {401, 403, 410} else result
    from install_cursor_children import install
    result=install(pending['credential_path'],hooks_file,legacy_credential_path=credential_path)
    result.update(child_observer_connected=not pending.get('pending',False),
                  evidence='native_hook',background_command_delivery_unchanged=True)
    if pending.get('pending'):
        marker=pending.get('activation_marker')
        if not isinstance(marker,str) or not 8<=len(marker)<=240:raise ValueError('Invalid observer activation')
        result.update(activation_marker=marker,next_step='在目前原聊天的公開回覆包含 activation_marker 原文一次；由原生 Hook 驗證身份後接通，毋須等待工具。')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credential',required=True)
    sub=parser.add_subparsers(dest='action',required=True)
    sub.add_parser('auto')
    grok=sub.add_parser('grok');grok.add_argument('--details-stdin',action='store_true',required=True)
    cursor=sub.add_parser('cursor-children');cursor.add_argument('--hooks-file',required=True)
    args=parser.parse_args()
    path=Path(args.credential).expanduser()
    try:
        credential=json.loads(path.read_text())
    except FileNotFoundError:
        print(json.dumps(missing_credential_result(path), ensure_ascii=False))
        raise SystemExit(1)
    if args.action=='auto':result=auto_connect(credential)
    elif args.action=='cursor-children':result=cursor_children(credential,args.hooks_file,args.credential)
    else:
        details=json.loads(sys.stdin.read(16001))
        if not isinstance(details,dict) or set(details)!={'url','key'}:raise ValueError('Invalid details')
        result=native_request(credential,{'adapter':'grok_webhook',**details})
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    try:main()
    except Exception as error:
        print(json.dumps({'connected':False,'reason':type(error).__name__}),file=sys.stderr)
        raise SystemExit(1)
