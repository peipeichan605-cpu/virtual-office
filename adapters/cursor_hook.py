#!/usr/bin/env python3
"""Cursor public-message hooks. Never consumes thoughts, tools, or transcript files."""
import argparse
import hashlib
import json
import sys
from pathlib import Path
from client import load_credential,send


def translate(data, source_id):
    if data.get('conversation_id')!=source_id:return None
    kind=data.get('hook_event_name');run=data.get('generation_id')
    if not isinstance(run,str) or not run:return None
    event={'source_id':source_id,'run_id':run}
    if kind=='beforeSubmitPrompt':
        event.update(type='turn_started',text=data.get('prompt',''))
        roots=data.get('workspace_roots')
        if isinstance(roots,list) and len(roots)==1 and isinstance(roots[0],str) and Path(roots[0]).is_absolute():
            event['workspace_path']=roots[0]
    elif kind=='afterAgentResponse':event.update(type='message',role='assistant',phase='final',text=data.get('text',''))
    elif kind=='stop':
        status=data.get('status')
        if status not in {'completed','aborted','error'}:return None
        event['type']={'completed':'turn_completed','aborted':'turn_cancelled','error':'turn_failed'}[status]
    else:return None
    event['event_id']=hashlib.sha256(json.dumps(event,sort_keys=True).encode()).hexdigest()
    return event

def main():
    p=argparse.ArgumentParser();p.add_argument('--credential',required=True);args=p.parse_args()
    data={}
    try:
        raw=sys.stdin.read(100001)
        if len(raw)>100000:raise ValueError('Hook input too large')
        data=json.loads(raw);credential=load_credential(args.credential)
        event=translate(data,credential['source_id'])
        if event:send(credential,event)
    except Exception as error:
        # Observational hook: report only error type; never block user work or auto-submit a new task.
        print('Office sync unavailable: '+type(error).__name__,file=sys.stderr)
    kind=data.get('hook_event_name') if isinstance(data,dict) else None
    print(json.dumps({'continue':True} if kind in {None,'beforeSubmitPrompt'} else {}))

if __name__=='__main__':main()
