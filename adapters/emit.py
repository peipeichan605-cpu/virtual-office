#!/usr/bin/env python3
"""Grok Bot / generic public-event reporter. Run on the same machine as Office."""
import argparse
import json
import sys
import uuid
from pathlib import Path
from client import load_credential,send


def main():
    p=argparse.ArgumentParser();p.add_argument('--credential',required=True)
    p.add_argument('type',choices=['turn_started','message','turn_completed','turn_failed','turn_cancelled','heartbeat'])
    p.add_argument('--run',required=True);p.add_argument('--event-id');p.add_argument('--phase',choices=['prompt','commentary','final'],default='commentary');p.add_argument('--text-stdin',action='store_true')
    p.add_argument('--workspace',help='Absolute project root; turn_started defaults to current directory')
    args=p.parse_args();credential=load_credential(args.credential)
    event={'source_id':credential['source_id'],'event_id':args.event_id or uuid.uuid4().hex,'run_id':args.run,'type':args.type}
    if args.type=='turn_started':event['workspace_path']=args.workspace or str(Path.cwd())
    if args.text_stdin:
        text=sys.stdin.read(24001)
        if len(text)>24000:raise ValueError('Message exceeds 24000 characters')
        event['text']=text
    if args.type=='message':event.update(role='user' if args.phase=='prompt' else 'assistant',phase=args.phase)
    print(json.dumps(send(credential,event)))

if __name__=='__main__':
    try:main()
    except Exception as error:
        print('Office report failed: '+type(error).__name__,file=sys.stderr);sys.exit(1)
