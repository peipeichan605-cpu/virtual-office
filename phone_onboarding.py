"""Owner-initiated private phone setup using the signed-in local Tailscale client."""
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time

import mobile_setup
import phone_setup_assistant as devices
import tailscale_mobile as ts
from mobile_gateway import settings
from mobile_host import PLATFORMS
from mobile_lifecycle import MobileLifecycleError
from security_keys import upgraded_security


class Step(Exception):
    """A fixed UI state; never forward the native CLI's output."""
    def __init__(self, state, **fields):
        super().__init__(state)
        self.state, self.fields = state, fields


@dataclass
class Observation:
    binary: str
    network: devices.Network
    mobile: dict
    mount: str
    gate: dict


def _stable_version(value):
    version = ts._version(value)
    return version is not None and version >= ts.MINIMUM_VERSION and version[1] % 2 == 0


def _mount(config, hostname, port):
    # Native status includes the persistent config plus any foreground and
    # service mounts. Only a completely free or exactly matching config is ours.
    if (set(config) - {'TCP', 'Web', 'AllowFunnel', 'Foreground', 'Services'} or
            any(not isinstance(v, dict) for v in config.values()) or
            any(config.get(k) for k in ('Web', 'Foreground', 'Services'))):
        return 'conflict'
    funnel = config.get('AllowFunnel', {})
    if funnel and (set(funnel) != {hostname + ':443'} or funnel[hostname + ':443'] is not False):
        return 'conflict'
    tcp = config.get('TCP', {})
    if not tcp:
        return 'empty'
    expected = {'TerminateTLS': hostname, 'TCPForward': '127.0.0.1:' + str(port), 'ProxyProtocol': 2}
    if tcp == {'443': expected} and type(tcp['443']['ProxyProtocol']) is int:
        return 'matched'
    return 'conflict'


def _activate_serve(binary, port):
    argv = ['serve', '--bg', '--proxy-protocol=2', '--tls-terminated-tcp=443',
            'tcp://127.0.0.1:' + str(port)]
    # Execute the fixed command once. A lost response is reconciled from both
    # status views. Nothing returned by the program is displayed or logged.
    try:
        code, message = ts._capture_cli(binary, argv, 10, sys.platform, include_stderr=True)
    except ts.ProbeError:
        return 'uncertain'
    if code == 0:
        return 'submitted'
    detail = message.lower()
    if b'unknown flag' in detail or b'flag provided but not defined' in detail:
        return 'update_required'
    if any(x in detail for x in (b'permission denied', b'access denied', b'access is denied')):
        return 'permission_required'
    return 'rejected'


class PhoneOnboarding:
    def __init__(self, bridge):
        self.bridge = bridge
        self.lock = threading.Lock()
        self.offer_key = secrets.token_bytes(32)
        self.retry_after = 0.0

    def _result(self, state, **fields):
        return {'state': state, 'platform': PLATFORMS.get(sys.platform, 'other'), **fields}

    def _observe(self):
        config = self.bridge.config
        if not upgraded_security(config):
            raise Step('legacy_required')
        if ('mobile_peer_gate' not in config and 'mobile' in config or
                os.path.lexists(Path(config['data_dir']) / 'phone-tsnet')):
            raise Step('existing_entry' if config.get('mobile', {}).get('enabled') is True else 'conflict')
        binary = ts.detect_binary()
        if not binary:
            raise Step('install')
        def probe(name):
            return ts._probe(binary, name, 2.0, sys.platform)
        if not _stable_version(probe('version').get('short')):
            raise Step('update_required')
        status = probe('status')
        state = status.get('BackendState')
        if state != 'Running':
            raise Step({'NeedsLogin':'login', 'NeedsMachineAuth':'device_approval',
                        'Stopped':'connect'}.get(state, 'unknown'))
        if not _stable_version(status.get('Version')):
            raise Step('update_required')
        if (status.get('CurrentTailnet') or {}).get('MagicDNSEnabled') is not True:
            raise Step('dns_required')
        port = settings(config)['port']
        own = status.get('Self')
        if not isinstance(own, dict) or not isinstance(own.get('DNSName'), str):
            raise Step('unknown')
        hostname = own['DNSName'].removesuffix('.')
        if ts._configuration('https://' + hostname, 'x', port) != hostname:
            raise Step('dns_required')
        certs = status.get('CertDomains')
        if not isinstance(certs, list) or hostname not in certs:
            raise Step('https_required', host=hostname)
        try:
            net = devices.network_from_status(status, port)
        except devices.SetupNeeded as error:
            raise Step('no_phone' if getattr(error, 'state', '') == 'no_phone' else 'unknown') from None
        target = {'enabled': True, 'port': port, 'public_origin': net.origin, 'identity': net.identity}
        gate = config.get('mobile_peer_gate')
        if gate is not None:
            from phone_peer_gate import validate_peer_gate
            validate_peer_gate(gate)
            if settings(config) != target or gate['host_id'] != net.host.node_id:
                raise Step('conflict')
        elif 'mobile_peer_gate' in config:
            raise Step('conflict')
        serve = _mount(probe('serve'), hostname, port)
        funnel = _mount(probe('funnel'), hostname, port)
        if serve != funnel or serve == 'conflict' or (serve == 'matched' and not gate):
            raise Step('conflict')
        return Observation(binary, net, target, serve, gate)

    def _token(self, observed, phone, expiry):
        net = observed.network
        # A change of host, account, target port or displayed device after
        # the user saw the confirmation must invalidate the selection.
        selection = ['private-phone-choice-v1', net.host.node_id, net.origin, net.identity,
                     observed.mobile['port'], phone.node_id, phone.name, expiry]
        encoded = json.dumps(selection, separators=(',', ':')).encode()
        return str(expiry) + '.' + hmac.new(self.offer_key, encoded, hashlib.sha256).hexdigest()

    def _chosen(self, observation, phone_id, token):
        phone = next((p for p in observation.network.phones if p.node_id == phone_id), None)
        if (phone is None or not isinstance(token, str) or
                not re.fullmatch(r'[0-9]{10,11}\.[0-9a-f]{64}', token)):
            raise Step('confirm_again')
        expiry = int(token.split('.')[0])
        if expiry < time.time() or not hmac.compare_digest(token, self._token(observation, phone, expiry)):
            raise Step('confirm_again')
        return phone

    def _view(self, observation, selected=None):
        gate = observation.gate
        authorized = gate['node_ids'] if gate else []
        expiry = int(time.time()) + 300
        phones = [{'id': p.node_id, 'name': p.name, 'authorized': p.node_id in authorized,
                   'selection_token': self._token(observation, p, expiry)}
                  for p in observation.network.phones]
        ready = (observation.mount == 'matched' and gate is not None and
                 self.bridge.mobile_lifecycle.listener_ready(observation.mobile, gate) and
                 not mobile_setup.assistant_pending(self.bridge.config))
        data = {'host': observation.network.origin[8:], 'phones': phones}
        if selected:
            data['selected_phone_id'] = selected
        return self._result('ready' if ready else 'select_phone', **data)

    def inspect(self):
        return self._run(None)

    def pairing_ready(self, node_id):
        # Re-observe native Serve, Funnel, the local listener and phone at QR
        # creation. This is a host-side check; the handset still has to pair.
        current = self.inspect()
        return (current['state'] == 'ready' and
                any(p['id'] == node_id and p['authorized'] for p in current['phones']))

    def activate(self, phone_id, selection_token):
        if not isinstance(phone_id, str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}', phone_id):
            return self._result('confirm_again')
        return self._run((phone_id, selection_token))

    def _run(self, choice):
        if not self.lock.acquire(blocking=False):
            return self._result('busy')
        try:
            observation = self._observe()
            if choice is None:
                return self._view(observation)
            return self._enable(observation, *choice)
        except Step as step:
            return self._result(step.state, **step.fields)
        except MobileLifecycleError as error:
            return self._result('conflict', reason=error.code)
        except (OSError, ts.ProbeError, KeyError, ValueError, TypeError, AttributeError):
            return self._result('unknown')
        finally:
            self.lock.release()

    def _enable(self, observation, phone_id, token):
        self._chosen(observation, phone_id, token)
        lifecycle = self.bridge.mobile_lifecycle
        target = observation.mobile
        old_gate = observation.gate
        gate = {'version': 1, 'host_id': observation.network.host.node_id,
                'node_ids': sorted(set(old_gate['node_ids'] if old_gate else []) | {phone_id})}
        if old_gate and gate != old_gate:
            # Extending the set of phones should never repair a broken mount
            # or take ownership of an unrelated listener as a side effect.
            if observation.mount != 'matched' or not lifecycle.listener_ready(target, old_gate):
                raise Step('conflict')
            lifecycle.authorize_phone(target, peer_gate=gate)
        else:
            lifecycle.configure(target, peer_gate=gate)
        with lifecycle.hold_listener(target, gate):
            current = self._observe()
            self._chosen(current, phone_id, token)
            if current.mobile != target or current.gate != gate or current.binary != observation.binary:
                raise Step('conflict')
            if current.mount == 'empty':
                if time.monotonic() < self.retry_after:
                    raise Step('activation_unconfirmed')
                mobile_setup.mark_assistant_pending(self.bridge.config, target)
                self.retry_after = time.monotonic() + 12
                result = _activate_serve(current.binary, target['port'])
                # Even if native activation returned success, only the actual
                # matching mount, current selected node and owned gateway count.
                try:
                    current = self._observe()
                except (ts.ProbeError, OSError):
                    raise Step('activation_unconfirmed') from None
                self._chosen(current, phone_id, token)
                if current.binary != observation.binary or current.mobile != target or current.gate != gate:
                    raise Step('conflict')
                if current.mount != 'matched':
                    if result not in ('submitted', 'uncertain') and current.mount == 'empty':
                        self.retry_after = 0
                        if result in ('permission_required', 'update_required'):
                            raise Step(result)
                    raise Step('activation_unconfirmed')
            if not lifecycle.listener_ready(target, gate):
                raise Step('activation_unconfirmed')
            mobile_setup.clear_assistant_pending(self.bridge.config, target)
            self.retry_after = 0
            return self._view(current, phone_id)
