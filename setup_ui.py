#!/usr/bin/env python3
"""Install the pinned upstream UI locally, verify hashes, then apply reviewed local changes.
No upstream installer, remote script, shell command, or model is executed.
"""
from collections import deque
import hashlib
import http.client
import json
from pathlib import Path
import shutil
import socket
import stat
import sys
import tempfile
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parent

def sha(data):return hashlib.sha256(data).hexdigest()
def relative(root,name):
    path=(root/name).resolve()
    if root.resolve() not in path.parents:raise ValueError('Unsafe manifest path')
    return path

def fetch_source(repositories, commit, item):
    # Try alternatives immediately. Retry a transiently interrupted source only once.
    pending = deque(repositories)
    retried = set()
    last_error = None
    while pending:
        repository = pending.popleft()
        url = 'https://raw.githubusercontent.com/{}/{}/{}'.format(repository, commit, item['source'])
        try:
            with urllib.request.urlopen(url, timeout=45) as response:
                expected = getattr(response, 'headers', {}).get('Content-Length', '')
                data = response.read(64 * 1024 * 1024 + 1)
                if expected.isdigit() and len(data) < min(int(expected), 64 * 1024 * 1024 + 1):
                    raise http.client.IncompleteRead(data, int(expected) - len(data))
        except urllib.error.HTTPError as error:
            error.close()
            if error.code not in (403, 404, 408, 410, 429) and not 500 <= error.code < 600:
                raise
            last_error = error
            if error.code != 408 and not 500 <= error.code < 600:
                continue
        except (urllib.error.URLError, socket.timeout, ConnectionError, http.client.IncompleteRead) as error:
            last_error = error
        else:
            if len(data) > 64 * 1024 * 1024 or sha(data) != item['source_sha256']:
                raise ValueError('Upstream checksum mismatch: ' + item['source'])
            return data, repository
        if repository not in retried:
            retried.add(repository)
            pending.append(repository)
    raise RuntimeError('Pinned UI file unavailable from all configured sources: ' + item['source']) from last_error


def build(manifest, destination):
    repositories = [manifest['repository'], *manifest.get('fallback_repositories', [])]
    for item in manifest['files']:
        data, source_repo = fetch_source(repositories, manifest['commit'], item)
        if source_repo != repositories[0]:
            repositories.remove(source_repo)
            repositories.insert(0, source_repo)
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
    manifest=json.loads((ROOT/'ui-manifest.json').read_text(encoding='utf-8'))
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
    # Some ZIP extractors discard the executable bit needed by macOS Terminal.
    if sys.platform == 'darwin':
        launcher = ROOT / 'Connect Phone.command'
        if launcher.is_file() and not launcher.is_symlink():
            mode = launcher.stat().st_mode
            if not mode & stat.S_IXUSR:
                launcher.chmod(mode | stat.S_IXUSR)
    print(json.dumps({'ui_installed':True,'files':len(files),'upstream_commit':manifest['commit']}))

if __name__=='__main__':main()
