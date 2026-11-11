#!/usr/bin/env python3
"""Wait inside an existing agent tool call; no model, subprocess runner or daemon."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):return None


def save(path, value):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    fd=os.open(tmp,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as f:json.dump(value,f,ensure_ascii=False,indent=2)
    tmp.replace(path)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credential',required=True)
    sub=parser.add_subparsers(dest='action',required=True)
    wait=sub.add_parser('wait');wait.add_argument('--max-seconds',type=int,default=3600)
    for name in ('start','message','complete','fail','cancel'):
        command=sub.add_parser(name);command.add_argument('--command',required=True)
        if name!='start':command.add_argument('--event-id',required=True);command.add_argument('--text-stdin',required=True,action='store_true')
    args=parser.parse_args();path=Path(args.credential).expanduser().resolve();credential=json.loads(path.read_text())
    url=urllib.parse.urlsplit(credential['endpoint'])
    if url.scheme!='http' or url.hostname not in {'127.0.0.1','localhost'} or url.path!='/api/agents/inbox' or url.username or url.password or url.query or url.fragment:raise ValueError('Invalid inbox endpoint')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    def call(body):
        req=urllib.request.Request(credential['endpoint'],data=json.dumps(body,ensure_ascii=False).encode(),method='POST',headers={
            'Content-Type':'application/json','X-Agent-Id':credential['agent_id'],'Authorization':'Bearer '+credential['token']})
        with opener.open(req,timeout=body.get('wait_seconds',0)+10) as response:return json.load(response)
    state_path=path.with_name('receiver.json')
    state=json.loads(state_path.read_text()) if state_path.exists() else {'receiver_id':str(uuid.uuid4()),'request_id':None}
    if args.action=='wait':
        if not 1<=args.max_seconds<=3600:raise ValueError('Wait must be bounded from 1 to 3600 seconds')
        lock=path.with_name('wait.lock')
        try:fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        except FileExistsError:
            previous=json.loads(lock.read_text())
            try:os.kill(previous['pid'],0)
            except ProcessLookupError:lock.unlink();fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            else:raise ValueError('This agent already has a waiting tool call')
        with os.fdopen(fd,'w') as output:json.dump({'pid':os.getpid()},output)
        status_path=path.with_name('wait-status.json')
        run={'run_id':str(uuid.uuid4()),'pid':os.getpid(),'action':'wait','max_seconds':args.max_seconds,
             'stop_target':{'pid':os.getpid(),'credential_file':str(path)},'state':'waiting'}
        save(status_path,run)
        try:
            state['request_id']=state.get('request_id') or str(uuid.uuid4());save(state_path,state)
            end=time.monotonic()+args.max_seconds
            while True:
                remaining=end-time.monotonic()
                if remaining<=0:result={'state':'wait_ended','listening':False};break
                result=call({'action':'receive','receiver_id':state['receiver_id'],'request_id':state['request_id'],'wait_seconds':min(50,remaining)})
                if result.get('state')=='waiting':continue
                if result.get('state')=='received':
                    if result.get('agent_id')!=credential['agent_id'] or str(uuid.UUID(result['command_id']))!=result['command_id']:raise ValueError('Invalid receiver response')
                    save(path.parent/'deliveries'/(result['command_id']+'.json'),{key:result[key] for key in ('agent_id','command_id','receipt_id')})
                    result['next_step']='Run start with this command_id. Execute text only when execute=true. Report public progress and completion, then wait again if still on standby.'
                break
            run['state']=result['state'];print(json.dumps(result,ensure_ascii=False))
        except BaseException:
            run['state']='interrupted';raise
        finally:
            save(status_path,run);lock.unlink(missing_ok=True)
    else:
        command=str(uuid.UUID(args.command))
        if command!=args.command:raise ValueError('Invalid command ID')
        receipt=json.loads((path.parent/'deliveries'/(command+'.json')).read_text())
        if receipt['agent_id']!=credential['agent_id']:raise ValueError('Invalid receipt')
        body={'action':args.action,'command_id':command,'receipt_id':receipt['receipt_id']}
        if args.action!='start':
            body.update(event_id=str(uuid.UUID(args.event_id)),text=sys.stdin.read(96001))
        result=call(body)
        if args.action=='start' and result.get('accepted'):
            state['request_id']=None;save(state_path,state)
        print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    try:main()
    except urllib.error.HTTPError as error:
        try:code=json.load(error).get('error','request_failed')
        except Exception:code='request_failed'
        print(json.dumps({'error':code,'status':error.code,'execute':False}),file=sys.stderr);sys.exit(1)
    except KeyboardInterrupt:raise SystemExit(130)
    except Exception as error:
        print(json.dumps({'error':type(error).__name__,'execute':False,'hint':'Do not execute an unacknowledged delivery. The saved request ID can reconcile an uncertain receive.'}),file=sys.stderr);sys.exit(1)
