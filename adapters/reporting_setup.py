#!/usr/bin/env python3
"""Confirm a selected Bot's read-back rule through its original event identity."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from public_reporting import EVIDENCE, ReportingError, event_endpoint, private_json


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, target):
        raise ReportingError('reporting_redirect_refused', 409)


def open_local(request, timeout=5):
    """Scoped credentials never use proxies or follow even local redirects."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    return opener.open(request, timeout=timeout)


def load_manifest(path):
    path = Path(path).expanduser().absolute()
    if path.name != 'reporting.json' or path.parent.is_symlink() or path.parent.parent.is_symlink():
        raise ReportingError('reporting_manifest_path_invalid', 409)
    manifest = private_json(path)
    agent = manifest.get('agent_id')
    if manifest.get('kind') != 'office_public_reporting' or manifest.get('version') != 1 or not isinstance(agent, str):
        raise ReportingError('reporting_manifest_invalid', 409)
    if path.parent.name != hashlib.sha256(agent.encode()).hexdigest()[:32] or path.parent.parent.name != 'reporting':
        raise ReportingError('reporting_manifest_path_invalid', 409)
    for directory in (path.parent, path.parent.parent):
        if directory.stat().st_mode & 0o077:
            raise ReportingError('reporting_directory_not_private', 409)
    credential_path = Path(manifest.get('credential_path', ''))
    if not credential_path.is_absolute() or any(p.is_symlink() for p in (credential_path.parent, credential_path.parent.parent)):
        raise ReportingError('reporting_credential_path_invalid', 409)
    source = manifest.get('source_id', '')
    if (not re.fullmatch(r'inv-[0-9a-f]{32}', source) or agent != 'grok_bot:' + source or
            credential_path != path.parent.parent.parent / 'invitations' / source[4:] / 'credential.json'):
        raise ReportingError('reporting_credential_path_invalid', 409)
    credential = private_json(credential_path)
    if credential.get('agent_id') != agent or credential.get('source_id') != source:
        raise ReportingError('reporting_credential_invalid', 409)
    endpoint = event_endpoint(credential.get('endpoint'))
    expected = urllib.parse.urlunsplit(endpoint._replace(path='/api/agents/reporting/setup'))
    if manifest.get('confirmation_endpoint') != expected:
        raise ReportingError('reporting_endpoint_invalid', 409)
    if not isinstance(credential.get('token'), str) or not credential['token']:
        raise ReportingError('reporting_credential_invalid', 409)
    return manifest, credential, expected


def confirm(path, readback_version, readback_agent):
    manifest, credential, endpoint = load_manifest(path)
    if readback_version != manifest.get('policy_version') or readback_agent != manifest['agent_id']:
        raise ReportingError('reporting_readback_mismatch', 409)
    data = {'setup_id': manifest['setup_id'], 'policy_version': manifest['policy_version'],
        'policy_hash': manifest['policy_hash'], 'readback_agent_id': readback_agent,
        'readback_policy_version': readback_version, 'evidence': EVIDENCE}
    request = urllib.request.Request(endpoint, data=json.dumps(data).encode(), method='POST', headers={
        'Content-Type': 'application/json', 'X-Agent-Id': credential['agent_id'],
        'Authorization': 'Bearer ' + credential['token']})
    with open_local(request, timeout=5) as response:
        result = json.load(response)
    if result.get('agent_id') != manifest['agent_id'] or result.get('state') != 'agent_confirmed':
        raise ReportingError('reporting_confirmation_unconfirmed', 409)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    confirmation = sub.add_parser('confirm')
    confirmation.add_argument('--readback-version', required=True)
    confirmation.add_argument('--readback-agent', required=True)
    args = parser.parse_args()
    print(json.dumps(confirm(args.manifest, args.readback_version, args.readback_agent), ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('OFFICE 公開同步設定未完成：' + (error.code if isinstance(error, ReportingError) else type(error).__name__), file=sys.stderr)
        sys.exit(1)
