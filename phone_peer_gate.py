"""Verify the device behind a private Tailscale Serve TCP connection."""
from datetime import datetime
import ipaddress
import re
import struct
import sys
import threading
import time

import tailscale_mobile


SIGNATURE = b'\r\n\r\n\x00\r\nQUIT\n'
NODE_ID = re.compile(r'[a-zA-Z0-9_-]{1,128}')
TAILNET_V4 = ipaddress.ip_network('100.64.0.0/10')
TAILNET_V6 = ipaddress.ip_network('fd7a:115c:a1e0::/48')
CACHE_TTL = 3.0
FAILURE_TTL = 2.0


def validate_peer_gate(gate):
    if (not isinstance(gate, dict) or set(gate) != {'version', 'host_id', 'node_ids'} or
            type(gate['version']) is not int or gate['version'] != 1 or
            not isinstance(gate['host_id'], str) or not NODE_ID.fullmatch(gate['host_id'])):
        raise ValueError('invalid_mobile_peer_gate')
    ids = gate['node_ids']
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 64 or
            any(not isinstance(node, str) or not NODE_ID.fullmatch(node) for node in ids) or
            len(set(ids)) != len(ids) or gate['host_id'] in ids):
        raise ValueError('invalid_mobile_peer_gate')
    return {'version': 1, 'host_id': gate['host_id'], 'node_ids': sorted(ids)}


def read_proxy_v2(stream, port):
    """The pinned Serve TCP forwarder emits just the address and ports."""
    header = stream.read(16)
    if len(header) != 16 or header[:12] != SIGNATURE or header[12] != 0x21:
        raise ValueError('invalid_mobile_proxy')
    address_size = {0x11: 4, 0x21: 16}.get(header[13])
    frame_len = int.from_bytes(header[14:16], 'big')
    if not address_size or frame_len != 2 * address_size + 4:
        raise ValueError('invalid_mobile_proxy')
    frame = stream.read(frame_len)
    if len(frame) != frame_len:
        raise ValueError('invalid_mobile_proxy')
    source = ipaddress.ip_address(frame[:address_size])
    destination = ipaddress.ip_address(frame[address_size:-4])
    src_port, dst_port = struct.unpack('!HH', frame[-4:])
    subnet = TAILNET_V4 if address_size == 4 else TAILNET_V6
    if (source not in subnet or destination not in subnet or source == destination or
            not src_port or dst_port != port):
        raise ValueError('invalid_mobile_proxy')
    return str(source), str(destination)


def _read_status():
    binary = tailscale_mobile.detect_binary()
    if not binary:
        raise tailscale_mobile.ProbeError('cli_unavailable')
    return tailscale_mobile._probe(binary, 'status', 2.0, sys.platform)


def _expires(row):
    expiry = row.get('KeyExpiry')
    if expiry is None:
        return float('inf')
    return datetime.fromisoformat(expiry.replace('Z', '+00:00')).timestamp()


class PeerGate:
    """Share one bounded native status read across concurrent connections.

    Cache only validated addresses, ids and expirations. A failed or expired
    observation is never used while waiting for a replacement observation.
    """
    def __init__(self, host_id, mobile, *, read_status=None, clock=time.monotonic, wall_clock=time.time):
        self.host_id = host_id
        self.mobile = dict(mobile)
        self.read_status = read_status or _read_status
        self.clock = clock
        self.wall_clock = wall_clock
        self.lock = threading.Lock()
        self.refresh_at = 0.0
        self.destinations = frozenset()
        self.peers = {}

    def _observe(self):
        from phone_setup_assistant import network_from_status
        status = self.read_status()
        network = network_from_status(status, self.mobile['port'])
        if (network.host.node_id != self.host_id or network.origin != self.mobile['public_origin'] or
                network.identity != self.mobile['identity']):
            return frozenset(), {}
        # Reject an address with any ambiguous owner, including other accounts.
        addresses = {}
        for row in status['Peer'].values():
            if not isinstance(row, dict):
                continue
            for ip in row.get('TailscaleIPs') or ():
                if isinstance(ip, str):
                    ip = str(ipaddress.ip_address(ip))
                    addresses[ip] = None if ip in addresses else row
        peers = {}
        host_expiry = _expires(status['Self'])
        for phone in network.phones:
            for ip in phone.ips:
                row = addresses.get(ip)
                if row and row.get('ID') == phone.node_id:
                    peers[ip] = (phone.node_id, min(_expires(row), host_expiry))
        return frozenset(network.host.ips), peers

    def resolve(self, source, destination):
        from phone_setup_assistant import SetupNeeded
        with self.lock:
            if self.clock() >= self.refresh_at:
                try:
                    self.destinations, self.peers = self._observe()
                    ttl = CACHE_TTL
                except (tailscale_mobile.ProbeError, SetupNeeded, OSError, ValueError,
                        AttributeError, KeyError, TypeError, OverflowError):
                    self.destinations, self.peers = frozenset(), {}
                    ttl = FAILURE_TTL
                self.refresh_at = self.clock() + ttl
            peer = self.peers.get(source)
            if destination in self.destinations and peer and self.wall_clock() < peer[1]:
                return peer[0]
            return None
