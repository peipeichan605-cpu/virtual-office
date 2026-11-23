#!/usr/bin/env python3
"""Read-only, bounded Tailscale preflight. Never authenticates or changes settings.

The result is deliberately not a security attestation: local CLI state cannot prove
tailnet grants, phone identity, listener isolation, or an OFFICE round trip.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit


MINIMUM_VERSION = (1, 104, 7)  # Reviewed deployment baseline, not a latest-version lookup.
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_TIMEOUT = 5.0
PROBES = {
    'version': ('version', '--json'),
    'status': ('status', '--json'),
    'preferences': ('get', '--json', 'all'),
    'remote_control': ('get', '--json', 'remote-config'),
    'serve': ('serve', 'status', '--json'),
    'funnel': ('funnel', 'status', '--json'),
}
MAC_BINARY = '/Applications/Tailscale.app/Contents/MacOS/Tailscale'


def detect_binary(binary=None, platform_name=None):
    """Check named installation locations only; never scan a user's home."""
    platform_name = platform_name or sys.platform
    if binary is not None:
        candidates = [os.fspath(binary)]
    else:
        candidates = [shutil.which('tailscale')]
        if platform_name == 'darwin':
            candidates.append(MAC_BINARY)
        elif platform_name == 'win32':
            for key in ('ProgramFiles', 'ProgramFiles(x86)'):
                if os.environ.get(key):
                    candidates.append(str(Path(os.environ[key]) / 'Tailscale' / 'tailscale.exe'))
    for candidate in candidates:
        if (isinstance(candidate, str) and '\0' not in candidate and
                Path(candidate).is_absolute() and Path(candidate).is_file() and
                os.access(candidate, os.X_OK)):
            return candidate
    return None


class ProbeError(Exception):
    """Only a constant reason code may escape a subprocess boundary."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_json_key')
        result[key] = value
    return result


def _capture_cli(binary, argv, timeout, platform_name, *, include_stderr=False):
    """Run a caller-selected, fixed argv with bounded output and time."""
    env = os.environ.copy()
    if platform_name == 'darwin':
        # The macOS app executable can otherwise launch a second GUI process.
        env['TAILSCALE_BE_CLI'] = '1'
    process = None
    finished = threading.Event()
    chunks, read_errors = [], []

    def read_stdout():
        total = 0
        try:
            while True:
                # Read one extra byte to detect overflow without buffering the
                # full CLI response. read1 returns available pipe data promptly.
                chunk = process.stdout.read1(min(65536, MAX_OUTPUT_BYTES - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_OUTPUT_BYTES:
                    read_errors.append('cli_output_limit')
                    break
                chunks.append(chunk)
        except (OSError, ValueError):
            read_errors.append('cli_unavailable')
        finally:
            process.stdout.close()
            finished.set()

    try:
        deadline = time.monotonic() + timeout
        process = subprocess.Popen(
            [binary, *argv], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT if include_stderr else subprocess.DEVNULL,
            env=env, shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        # A daemon reader also bounds memory on Windows, where select() cannot
        # monitor subprocess pipes. It owns closure of the read descriptor.
        threading.Thread(target=read_stdout, daemon=True).start()
        if not finished.wait(max(0, deadline - time.monotonic())):
            raise ProbeError('cli_timeout')
        if read_errors:
            raise ProbeError(read_errors[0])
        process.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        raise ProbeError('cli_timeout') from None
    except OSError:
        raise ProbeError('cli_unavailable') from None
    finally:
        if process is not None and process.poll() is None:
            # Only this helper's exact child is stopped. Never touch the GUI,
            # network extension, daemon or an unrelated process group.
            try:
                process.kill()
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
    return process.returncode, b''.join(chunks)


def _probe(binary, name, timeout, platform_name):
    code, raw = _capture_cli(binary, PROBES[name], timeout, platform_name)
    if code != 0:
        raise ProbeError('cli_command_failed')
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise ProbeError('cli_invalid_json') from None
    # The stock CLI marshals a nil ServeConfig as JSON null on fresh installs.
    if value is None and name in ('serve', 'funnel'):
        return {}
    if not isinstance(value, dict):
        raise ProbeError('cli_unexpected_schema')
    return value


def _version(value):
    if not isinstance(value, str) or len(value) > 200:
        return None
    match = re.fullmatch(r'(\d{1,4})\.(\d{1,4})\.(\d{1,4})(?:-t[0-9a-f]+(?:-g[0-9a-f]+)?)?', value)
    return tuple(map(int, match.groups())) if match else None


def _configuration(origin, identity, port):
    if (not isinstance(origin, str) or len(origin) > 253 or
            not isinstance(identity, str) or not identity.strip() or
            len(identity) > 254 or any(ord(c) < 32 for c in identity) or
            isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535):
        return None
    try:
        parsed = urlsplit(origin)
        host = parsed.hostname or ''
        if (parsed.scheme != 'https' or parsed.netloc != host or
                parsed.path not in ('', '/') or parsed.query or parsed.fragment or
                not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+ts\.net', host)):
            return None
        return host
    except ValueError:
        return None


def _funnel_enabled(value):
    """Look in foreground and service configs too; never return their contents."""
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            if 'AllowFunnel' in current:
                funnel = current['AllowFunnel']
                if not isinstance(funnel, dict) or any(v is not False for v in funnel.values()):
                    return True
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return False


def _serve_matches(value, host, port):
    # This deployment deliberately uses one persistent HTTPS mount. Unknown,
    # foreground, service, raw TCP and file-serving configurations require review.
    if set(value) - {'TCP', 'Web', 'AllowFunnel', 'Foreground', 'Services'}:
        return False
    if any(not isinstance(value.get(key, {}), dict) for key in ('TCP', 'Web', 'AllowFunnel', 'Foreground', 'Services')):
        return False
    if value.get('Foreground') or value.get('Services'):
        return False
    tcp = value.get('TCP', {})
    if set(tcp) != {'443'} or not isinstance(tcp['443'], dict) or set(tcp['443']) != {'HTTPS'} or tcp['443']['HTTPS'] is not True:
        return False
    expected = {host + ':443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:' + str(port)}}}}
    # Serve may normalize the backend URL with a trailing slash.
    actual = value.get('Web')
    if actual == expected:
        return True
    expected[host + ':443']['Handlers']['/']['Proxy'] += '/'
    return actual == expected


def inspect_tailscale(origin, identity, port, binary=None, *, timeout=MAX_TIMEOUT, platform_name=None):
    """Return sanitized observations, never secrets or a claim of safe activation.

    At most six read-only CLI processes run, each for at most five seconds.
    The first unavailable process stops inspection. No subprocess error text,
    auth URL, peer list, IP address, email address or unfiltered JSON is returned.
    """
    result = {'state': 'setup_required', 'transport_ready': False,
              'policy_verified': False, 'requires_policy_review': True,
              'end_to_end_verified': False, 'probes_completed': [], 'checks': {}, 'issues': []}
    host = _configuration(origin, identity, port)
    if host is None or isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_TIMEOUT:
        result['issues'].append('invalid_expected_configuration')
        return result
    platform_name = platform_name or sys.platform
    executable = detect_binary(binary, platform_name)
    if executable is None:
        result['issues'].append('tailscale_not_installed_or_binary_unavailable')
        return result

    observations = {}
    for name in PROBES:
        try:
            data = _probe(executable, name, timeout, platform_name)
        except ProbeError as error:
            result['issues'].append(name + '_' + str(error))
            return result
        observations[name] = data
        result['probes_completed'].append(name)
        if name == 'version':
            version = _version(data.get('short'))
            result['checks']['client_meets_reviewed_baseline'] = bool(version and version >= MINIMUM_VERSION and version[1] % 2 == 0)
            if not result['checks']['client_meets_reviewed_baseline']:
                result['issues'].append('tailscale_security_update_or_version_review_required')
                return result
            result['client_version'] = '.'.join(map(str, version))
        if name == 'status':
            state = data.get('BackendState')
            result['checks']['running'] = state == 'Running'
            if state != 'Running':
                reason = {'NeedsLogin': 'tailscale_login_required',
                          'NeedsMachineAuth': 'tailscale_device_approval_required',
                          'Stopped': 'tailscale_not_running'}.get(state, 'tailscale_backend_not_ready')
                result['issues'].append(reason)
                return result

    checks = result['checks']
    status = observations['status']
    daemon = _version(status.get('Version'))
    checks['daemon_meets_reviewed_baseline'] = bool(daemon and daemon >= MINIMUM_VERSION and daemon[1] % 2 == 0)
    own = status.get('Self') if isinstance(status.get('Self'), dict) else {}
    checks['origin_matches_node_dns'] = own.get('DNSName') in (host, host + '.')
    users = status.get('User') if isinstance(status.get('User'), dict) else {}
    checks['expected_identity_visible'] = any(isinstance(u, dict) and u.get('LoginName') == identity for u in users.values())
    # This identifies an account visible to the host, not the requesting phone.
    host_user = users.get(str(own.get('UserID')), {})
    checks['host_identity_matches'] = isinstance(host_user, dict) and host_user.get('LoginName') == identity
    tailnet = status.get('CurrentTailnet') if isinstance(status.get('CurrentTailnet'), dict) else {}
    checks['magic_dns_enabled'] = tailnet.get('MagicDNSEnabled') is True
    checks['health_clear'] = status.get('Health') in (None, [])
    prefs = observations['preferences']
    expected = {'shields-up': False, 'ssh': False, 'accept-routes': False,
                'advertise-exit-node': False, 'advertise-connector': False,
                'webclient': False, 'advertise-routes': '', 'exit-node': ''}
    checks['preferences_match_minimal_profile'] = all(
        key in prefs and type(prefs[key]) is type(want) and prefs[key] == want
        for key, want in expected.items())
    # Hidden preferences are intentionally excluded from `get ... all`.
    checks['remote_control_disabled'] = observations['remote_control'].get('remote-config') is False
    checks['funnel_disabled'] = not any(_funnel_enabled(observations[name]) for name in ('serve', 'funnel'))
    checks['single_loopback_https_mount'] = all(
        _serve_matches(observations[name], host, port) for name in ('serve', 'funnel'))
    reasons = {'daemon_meets_reviewed_baseline': 'daemon_security_update_or_version_review_required',
               'origin_matches_node_dns': 'origin_dns_mismatch',
               'expected_identity_visible': 'expected_identity_not_observed',
               'magic_dns_enabled': 'magic_dns_not_confirmed',
               'health_clear': 'tailscale_health_review_required',
               'preferences_match_minimal_profile': 'network_preferences_review_required',
               'remote_control_disabled': 'remote_control_review_required',
               'funnel_disabled': 'funnel_enabled_or_unrecognized',
               'single_loopback_https_mount': 'serve_setup_or_conflict_review_required'}
    result['issues'].extend(reason for check, reason in reasons.items() if not checks[check])
    result['transport_ready'] = not result['issues']
    if result['transport_ready']:
        result['state'] = 'transport_observed_policy_and_phone_test_required'
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', required=True, help='Expected HTTPS node origin (no dashboard token)')
    parser.add_argument('--identity', required=True, help='Expected Tailscale login identity')
    parser.add_argument('--port', required=True, type=int, help='Dedicated loopback phone gateway port')
    parser.add_argument('--binary', help='Optional installed Tailscale executable, absolute path')
    args = parser.parse_args(argv)
    result = inspect_tailscale(args.origin, args.identity, args.port, args.binary)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['transport_ready'] else 2


if __name__ == '__main__':
    sys.exit(main())
