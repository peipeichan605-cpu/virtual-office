#!/usr/bin/env python3
"""Install the pinned upstream UI locally, verify hashes, then apply reviewed local changes.
No upstream installer, remote script, shell command, or model is executed.
"""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import urllib.request

ROOT=Path(__file__).resolve().parent

def sha(data):return hashlib.sha256(data).hexdigest()
def relative(root,name):
    path=(root/name).resolve()
    if root.resolve() not in path.parents:raise ValueError('Unsafe manifest path')
    return path

def build(manifest, destination):
    for item in manifest['files']:
        url='https://raw.githubusercontent.com/'+manifest['repository']+'/'+manifest['commit']+'/'+item['source']
        with urllib.request.urlopen(url,timeout=45) as response:data=response.read(64*1024*1024+1)
        if len(data)>64*1024*1024 or sha(data)!=item['source_sha256']:raise ValueError('Upstream checksum mismatch: '+item['source'])
        if item.get('edits'):
            lines=data.decode().splitlines(keepends=True)
            for begin,end,replacement in reversed(item['edits']):lines[begin:end]=replacement
            data=''.join(lines).encode()
        if sha(data)!=item['installed_sha256']:raise ValueError('Patch checksum mismatch: '+item['source'])
        target=relative(destination,item['destination']);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
    for item in manifest['overrides']:
        data=relative(ROOT/'ui-overrides',item['destination']).read_bytes()
        if sha(data)!=item['sha256']:raise ValueError('Local UI checksum mismatch')
        target=relative(destination,item['destination']);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)

def main():
    manifest=json.loads((ROOT/'ui-manifest.json').read_text())
    final=ROOT/'public'
    with tempfile.TemporaryDirectory(prefix='.office-ui-',dir=ROOT) as folder:
        stage=Path(folder);build(manifest,stage)
        files=[p for p in stage.rglob('*') if p.is_file()]
        # Check all destinations before changing any existing files.
        for source in files:
            target=relative(final,str(source.relative_to(stage)))
            if target.exists() and sha(target.read_bytes())!=sha(source.read_bytes()):raise ValueError('Existing UI differs; back it up before reinstalling: '+str(source.relative_to(stage)))
        for source in files:
            target=relative(final,str(source.relative_to(stage)));target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
    print(json.dumps({'ui_installed':True,'files':len(files),'upstream_commit':manifest['commit']}))

if __name__=='__main__':main()
