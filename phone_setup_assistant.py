"""Interactive host setup for the private mobile gateway using Tailscale Serve."""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys

from configuration import DEFAULT_CONFIG, load_config, save_config
from mobile_gateway import settings
from office import require_offline, stop_metadata
import mobile_setup
from private_files import require_private
from host_runtime import shell_command
from security_keys import upgrade_security, upgraded_security
import tailscale_mobile as ts


class SetupNeeded(Exception):
    """A human-readable next step, without raw subprocess or configuration data."""

    def __init__(self, message, *, state=None):
        super().__init__(message)
        self.state = state


@dataclass(frozen=True)
class Node:
    node_id: str
    name: str
    ips: tuple


@dataclass(frozen=True)
class Network:
    host: Node
    origin: str
    identity: str
    phones: tuple


def gateway_proof(config, nonce):
    """Domain-separated proof that this loopback listener belongs to this install."""
    if not isinstance(nonce, str) or not re.fullmatch(r'[0-9a-f]{64}', nonce):
        raise ValueError('invalid_nonce')
    if not upgraded_security(config):
        raise ValueError('separate_credentials_required')
    mobile = settings(config)
    if not mobile['enabled']:
        raise ValueError('mobile_not_enabled')
    facts = ['office-local-mobile-gateway-v1', nonce, config.get('port', 4318), mobile]
    if 'mobile_peer_gate' in config:
        from phone_peer_gate import validate_peer_gate
        facts.append(validate_peer_gate(config['mobile_peer_gate']))
    payload = json.dumps(facts, sort_keys=True, separators=(',', ':')).encode()
    return hmac.new(config['internal_hmac_key'].encode(), payload, hashlib.sha256).hexdigest()


def owner_listener_proof(config, nonce):
    """Read-only challenge for the current installation's desktop listener."""
    if (not isinstance(nonce, str) or not re.fullmatch(r'[0-9a-f]{64}', nonce)
            or not upgraded_security(config) or config.get('host', '127.0.0.1') != '127.0.0.1'):
        raise ValueError('invalid_owner_listener_challenge')
    port = config.get('port', 4318)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('invalid_owner_listener_port')
    filename = config.get('config_path')
    location = os.path.normcase(os.path.realpath(filename)) if filename else ''
    payload = json.dumps(['office-local-owner-listener-v1', nonce, location, '127.0.0.1',
                          port, config['owner_key']], separators=(',', ':')).encode()
    return hmac.new(config['internal_hmac_key'].encode(), payload, hashlib.sha256).hexdigest()


def gateway_proof_request(config, nonce):
    """Authorize only an exact, read-only local setup challenge."""
    bound = gateway_proof(config, nonce)
    message = ('office-local-phone-proof-request-v1:' + bound).encode('ascii')
    return hmac.new(config['internal_hmac_key'].encode(), message, hashlib.sha256).hexdigest()


def _verified_listener(config, port, route, expected_proof):
    nonce = secrets.token_hex(32)
    client = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
    try:
        headers = {'X-Office-Setup-Nonce': nonce}
        if route == '/_office/local/gateway-proof' and 'mobile_peer_gate' in config:
            headers['X-Office-Setup-Request'] = gateway_proof_request(config, nonce)
        client.request('GET', route, headers=headers)
        response = client.getresponse()
        if response.status != 200 or response.getheader('Content-Type', '').split(';')[0] != 'application/json':
            return False
        raw = response.read(257)
        if len(raw) > 256:
            return False
        data = json.loads(raw)
        return (isinstance(data, dict) and set(data) == {'proof'} and isinstance(data['proof'], str)
                and re.fullmatch(r'[a-f0-9]{64}', data['proof']) is not None
                and hmac.compare_digest(expected_proof(config, nonce), data['proof']))
    except (http.client.HTTPException, OSError, ValueError):
        return False
    finally:
        client.close()


def _verified_backend(config):
    return _verified_listener(config, settings(config)['port'], '/_office/local/gateway-proof', gateway_proof)


def _open_owner_page(path, *, phone_setup=False):
    config = load_config(path)
    port = config['port']
    if (config.get('host') != '127.0.0.1' or not upgraded_security(config) or
            not _verified_listener(config, port, '/_office/local/owner-proof', owner_listener_proof)):
        raise SetupNeeded('請先正常啟動這套 OFFICE，再開此助手選「開啟管理頁」。目前尚未核對管理服務。',
                          state='owner_listener_unverified')
    query = '?mobile-setup=1' if phone_setup else ''
    url = f"http://127.0.0.1:{port}/office/{query}#{config['owner_key']}"
    try:
        if sys.platform == 'win32':
            os.startfile(url)
            return
        if sys.platform == 'darwin':
            # Pass the private fragment via an anonymous pipe. A URL on the
            # command line would be visible to other process-list readers.
            script = ('ObjC.import("AppKit");\n'
                      'const url = $.NSURL.URLWithString(' + json.dumps(url) + ');\n'
                      'if (!$.NSWorkspace.sharedWorkspace.openURL(url)) throw Error("open_failed");')
            opened = subprocess.run(['/usr/bin/osascript', '-l', 'JavaScript', '-'], input=script.encode(),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=8)
            if opened.returncode == 0:
                return
        elif sys.platform.startswith('linux') and (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
            # Use the installed graphical desktop's URI handler in process.
            # Headless and minimal systems retain the private terminal fallback.
            import ctypes
            import ctypes.util
            library = ctypes.util.find_library('gio-2.0')
            if library:
                launch = ctypes.CDLL(library).g_app_info_launch_default_for_uri
                launch.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p]
                launch.restype = ctypes.c_int
                if launch(url.encode('ascii'), None, None):
                    return
    except (OSError, AttributeError, subprocess.TimeoutExpired):
        pass
    raise SetupNeeded('未能交給桌面瀏覽器。你可以在這部電腦的私人終端機執行以下指令，取回管理頁書籤。請只在自己電腦開啟結果。\n' + _local_command(path, 'url'))


def _local_command(path, action):
    argv = [*stop_metadata(path)['stop_argv'][:-1], action]
    return shell_command(argv)


_IPV4 = ipaddress.ip_network('100.64.0.0/10')
_IPV6 = ipaddress.ip_network('fd7a:115c:a1e0::/48')


def _node(row):
    """Return only display-safe, currently valid, owned-device metadata."""
    if (not isinstance(row, dict) or row.get('Online') is not True or
            row.get('Expired', False) is not False or row.get('Tags') or
            row.get('ShareeNode') or row.get('AltSharerUserID', 0) != 0):
        return None
    expiry = row.get('KeyExpiry')
    if expiry is not None:
        try:
            expiry = datetime.fromisoformat(expiry.replace('Z', '+00:00'))
            if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
                return None
        except (AttributeError, ValueError, TypeError, OverflowError):
            return None
    name, node_id = row.get('HostName'), row.get('ID')
    if not isinstance(name, str) or not name.isprintable() or not 1 <= len(name) <= 128:
        return None
    if not isinstance(node_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', node_id):
        return None
    addresses = row.get('TailscaleIPs')
    if not isinstance(addresses, list) or not 1 <= len(addresses) <= 8:
        return None
    ips = []
    for address in addresses:
        if not isinstance(address, str) or '%' in address:
            return None
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return None
        if ip not in _IPV4 and ip not in _IPV6:
            return None
        ips.append(str(ip))
    return Node(node_id, name, tuple(sorted(set(ips))))


def discover(binary, port):
    status = ts._probe(binary, 'status', ts.MAX_TIMEOUT, sys.platform)
    return network_from_status(status, port)


def network_from_status(status, port):
    state = status.get('BackendState')
    if state != 'Running':
        raise SetupNeeded('請在電腦開啟 Tailscale，用手機相同帳戶登入並連線；如果顯示等候批准，請由帳戶管理員批准。')
    own = status.get('Self')
    host = _node(own)
    if not host or type(own.get('UserID')) is not int or own['UserID'] <= 0:
        raise SetupNeeded('未能辨認這部電腦的有效個人裝置身份。請在 Tailscale 檢查此裝置及登入帳戶。')
    users = status.get('User')
    user = users.get(str(own['UserID'])) if isinstance(users, dict) else None
    identity = user.get('LoginName') if isinstance(user, dict) else None
    dns = own.get('DNSName')
    if not isinstance(dns, str) or not isinstance(identity, str) or not identity.isascii() or not identity.isprintable():
        raise SetupNeeded('Tailscale 尚未提供電腦名稱和帳戶。請重新檢查連線。')
    host_dns = dns.removesuffix('.')
    origin = 'https://' + host_dns
    if ts._configuration(origin, identity, port) != host_dns:
        raise SetupNeeded('未能核對私人 HTTPS 網址。請在 Tailscale 管理頁檢查 MagicDNS。')
    if not isinstance(status.get('CertDomains'), list) or host_dns not in status['CertDomains']:
        raise SetupNeeded(f'如同意憑證公開記錄電腦名稱 {host_dns}，請由擁有人在 https://login.tailscale.com/admin/dns 啟用 MagicDNS 及 HTTPS Certificates，再繼續。')
    peers = status.get('Peer')
    phones = []
    if isinstance(peers, dict):
        for row in peers.values():
            if (isinstance(row, dict) and type(row.get('UserID')) is int and row['UserID'] == own['UserID']
                    and str(row.get('OS', '')).lower() in {'android', 'ios'}):
                phone = _node(row)
                if phone and phone.node_id != host.node_id and not set(phone.ips).intersection(host.ips):
                    phones.append(phone)
    if not phones:
        raise SetupNeeded('請在 Android／iPhone 的 Tailscale 登入同一帳戶，按 Connect 至顯示已連線；確認手機沒有過期。', state='no_phone')
    if (len({n.node_id for n in phones}) != len(phones)
            or len({ip for n in phones for ip in n.ips}) != sum(len(n.ips) for n in phones)):
        raise SetupNeeded('手機身份或地址重複。請在 Tailscale 裝置清單核對後再試。')
    return Network(host, origin, identity, tuple(sorted(phones, key=lambda n: (n.name, n.node_id))))


def _mount_state(binary, network, port):
    result = ts.inspect_tailscale(network.origin, network.identity, port, binary=binary)
    if result['transport_ready'] and result['checks'].get('host_identity_matches') is True:
        return 'existing'
    if result['issues'] != ['serve_setup_or_conflict_review_required'] or not result['checks'].get('host_identity_matches'):
        issues = set(result['issues'])
        if issues.intersection({'tailscale_security_update_or_version_review_required',
                                'daemon_security_update_or_version_review_required'}):
            raise SetupNeeded('請先從 https://tailscale.com/download 更新電腦的 Tailscale 至支援的穩定版本，開啟並連線後再試。')
        if 'funnel_enabled_or_unrecognized' in issues:
            raise SetupNeeded('此電腦可能有對公網開放的 Funnel。請網絡管理員核對現有用途，或使用獨立的 OFFICE 私人節點。')
        if 'network_preferences_review_required' in issues or 'remote_control_review_required' in issues:
            raise SetupNeeded('這部電腦的 Tailscale 網絡設定需要管理員核對。請由管理員按 docs/MOBILE-ACCESS.md 檢查子網路、出口節點和遠端控制設定；請保留日常使用的 VPN。')
        if 'magic_dns_not_confirmed' in issues:
            raise SetupNeeded('請在 https://login.tailscale.com/admin/dns 檢查這個私人網絡的 MagicDNS，完成後重新開啟助手。')
        raise SetupNeeded('未能核對此電腦的 Tailscale。請先在電腦 App 確認帳戶及連線，如需管理員協助可提供以下檢查代號：' + ', '.join(sorted(issues)))
    # Only a completely empty Serve AND Funnel state qualifies. Unknown or
    # unrelated mounts must never be replaced by the assistant.
    for kind in ('serve', 'funnel'):
        data = ts._probe(binary, kind, ts.MAX_TIMEOUT, sys.platform)
        if set(data) - {'TCP', 'Web', 'AllowFunnel', 'Foreground', 'Services'} or any(v != {} for v in data.values()):
            raise SetupNeeded('這部電腦已有其他 Serve／Funnel 設定。請保留原設定，讓網絡管理員核對專用入口。')
    return 'empty'


def _installation(path):
    config = load_config(path)
    if not upgraded_security(config) and ('owner_key' in config or 'internal_hmac_key' in config):
        raise SetupNeeded('這套 OFFICE 的憑證設定不完整。請先核對原有安裝設定。')
    if os.path.lexists(Path(config['data_dir']) / 'phone-tsnet'):
        raise SetupNeeded('這套 OFFICE 已有獨立私人節點的資料。請用原有私人入口；維護步驟見 docs/MOBILE-TSNET.md。')
    return config


def _activate(binary, port):
    env = os.environ.copy()
    if sys.platform == 'darwin':
        env['TAILSCALE_BE_CLI'] = '1'
    try:
        result = subprocess.run([binary, 'serve', '--bg', '--https=443', f'http://127.0.0.1:{port}'],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=env, timeout=12, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise SetupNeeded('Tailscale 未回報設定結果。請再開助手核對已保存的入口。') from None
    if result.returncode:
        raise SetupNeeded('Tailscale 未確認啟用。請檢查 Tailscale 的帳戶批准或本機操作權限，再開助手核對。')


@contextmanager
def _setup_lock(config):
    root = require_private(Path(config['data_dir']), directory=True)
    path = root / 'phone-setup.lock'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        require_private(path, fd=fd)
        try:
            if os.name == 'nt':
                import msvcrt
                # Windows permits locking a byte beyond EOF.
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SetupNeeded('這套 OFFICE 已開啟另一個手機設定助手。請回到原本視窗繼續。') from None
        yield
    finally:
        os.close(fd)


def run_setup(path, ask=input, show=print):
    config = _installation(path)
    with _setup_lock(config):
        if _installation(path) != config:
            raise SetupNeeded('OFFICE 設定已變更。請重新開啟助手。')
        _setup(path, config, ask, show)


def _setup(path, config, ask, show):
    def pause(message):
        if ask(message + '（完成後按 Enter；輸入 q 離開）').strip():
            raise SetupNeeded('已離開設定。稍後可從同一助手繼續。')

    legacy = not upgraded_security(config)
    new_mobile = 'mobile' not in config or (legacy and config['mobile'] == {})
    if legacy and not new_mobile:
        raise SetupNeeded('舊版 OFFICE 已有手機設定。請由擁有人核對原入口及憑證設定，再進行升級。')
    if config.get('legacy_integrity_keys') and not legacy:
        action = ask('要用本機瀏覽器開啟更新後的管理頁，輸入 O；要繼續手機設定，按 Enter：').strip().casefold()
        if action == 'o':
            _open_owner_page(path)
            show('已請本機瀏覽器開啟管理頁。你可以更新原本的書籤。')
            return
        if action:
            raise SetupNeeded('已離開手機設定助手。')
    port = settings(config)['port']
    binary = ts.detect_binary()
    if not binary:
        pause('請在這部電腦從 https://tailscale.com/download 安裝 Tailscale，登入手機相同帳戶並連線。')
        binary = ts.detect_binary()
        if not binary:
            raise SetupNeeded('仍未找到電腦上的 Tailscale。請在安裝完成後重新開啟助手。')
    try:
        network = discover(binary, port)
    except SetupNeeded as error:
        pause(str(error))
        network = discover(binary, port)
    current_mount = _mount_state(binary, network, port)
    target = {'enabled': True, 'port': port, 'public_origin': network.origin, 'identity': network.identity}
    if not new_mobile and settings(config) != target:
        raise SetupNeeded('這套 OFFICE 已保存另一項手機設定或已停用手機。請由擁有人核對原有設定。')
    if current_mount == 'existing' and (new_mobile or not _verified_backend(config)):
        raise SetupNeeded('已有 HTTPS 入口，但尚未確認它接到這套 OFFICE。請先檢查原本的手機服務。')
    show('請選擇可連入 OFFICE 的手機：')
    for index, phone in enumerate(network.phones, start=1):
        show(f'{index}. {phone.name}  ({", ".join(phone.ips)})')
    choice = ask('輸入手機編號（Enter 離開）：').strip()
    if choice not in {str(i) for i in range(1, len(network.phones) + 1)}:
        raise SetupNeeded('尚未選擇手機。')
    chosen = network.phones[int(choice) - 1]
    show(f'手機 {chosen.name} ({", ".join(chosen.ips)}) → 電腦 {network.host.name} ({", ".join(network.host.ips)}) TCP 443')
    show('打開 https://login.tailscale.com/admin/acls 的 Access controls（存取控制）→ Grants／規則。')
    show('核對來源是上面的手機地址，目的地是電腦地址，只開 TCP 443。既有規則會疊加；若有允許全部裝置互通的規則，請管理員核對整份政策。')
    show('未肯定可按 Enter 安全離開；保留原有政策，稍後再繼續。')
    show(f'HTTPS 憑證的公開記錄會包含 {network.origin[8:]}。請在這段時間獨自管理此入口。')
    if ask('已核對以上政策，並同意為所選手機設定私人連線？輸入 Y 繼續：').strip().casefold() != 'y':
        raise SetupNeeded('尚未確認網絡政策及 HTTPS 名稱，已暫停。')

    if legacy:
        show('這套 OFFICE 使用舊版管理書籤。升級會更換桌面管理書籤，把管理權限和手機入口分開，並停用原有的外部動態來源。已登記的 Agent 及對話紀錄會保留。')
        show('服務重新啟動後，可從這個助手直接開啟新的管理頁並儲存書籤。')
        if ask('是否同意在 OFFICE 正常停止後，一併升級管理憑證及儲存手機設定？輸入 Y 同意：').strip().casefold() != 'y':
            raise SetupNeeded('已取消升級。原本的管理書籤和 OFFICE 設定仍然有效。')

    def recheck(*, after_start=False):
        latest = _installation(path)
        # The normal launcher writes these two run records on every start.
        # Before the config write we check the entire snapshot.
        expected = config
        if after_start:
            expected = {k:v for k,v in config.items() if k not in {'run_id','run_dir'}}
            latest = {k:v for k,v in latest.items() if k not in {'run_id','run_dir'}}
        observed = discover(binary, port)
        if (observed.host != network.host or observed.origin != network.origin or observed.identity != network.identity
                or chosen not in observed.phones or latest != expected):
            raise SetupNeeded('電腦、手機或 OFFICE 設定已變更。請重新開啟助手核對。')
        return _mount_state(binary, observed, port)

    if new_mobile:
        candidate = dict(config, mobile=target)
        try:
            require_offline(candidate)
        except ValueError:
            show('請等目前的工作完成，在另一個終端機正常停止這套 OFFICE。停止指令如下。')
            show(_local_command(path, 'stop'))
            pause('請保持本助手開啟，待 OFFICE 停止。')
        if recheck() != 'empty':
            raise SetupNeeded('私人入口已變更。請重新核對現有連線。')
        try:
            require_offline(candidate)
        except ValueError:
            raise SetupNeeded('OFFICE 或手機連接埠仍在使用。請完成現有工作並確認服務已停止，再開助手。') from None
        if _installation(path) != config:
            raise SetupNeeded('OFFICE 設定已變更。請重新核對。')
        if legacy:
            candidate = upgrade_security(candidate)
        mobile_setup.mark_assistant_pending(config, target)
        save_config(path, candidate)
        config = _installation(path)
        if config != candidate:
            raise SetupNeeded('剛儲存的 OFFICE 設定已變更。請重新核對。')
        if legacy:
            show('管理憑證已升級。即使稍後才完成手機配對，也可正常啟動 OFFICE，再開本助手選「開啟管理頁」取回新書籤。')
    if not _verified_backend(config):
        show('本機設定已保存。如已自行重新開啟 OFFICE，請等到辦公室畫面出現。')
        action = ask('如要助手幫你啟動已停止的 OFFICE，輸入 Y；已自行啟動請按 Enter；輸入 q 離開：').strip().casefold()
        if action == 'y':
            try:
                require_offline(config)
            except ValueError:
                raise SetupNeeded('OFFICE 或原本的連接埠正在使用。請讓原有服務完成啟動，再回助手核對。') from None
            argv = [sys.executable, str(Path(__file__).resolve().with_name('office.py')), '--config', str(path), 'start']
            try:
                started = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, timeout=40, check=False)
            except (OSError, subprocess.TimeoutExpired):
                raise SetupNeeded('啟動未確認。請核對正常 OFFICE 已開，再使用本助手。') from None
            if started.returncode:
                raise SetupNeeded('未能確認 OFFICE 啟動。請用平常方法開啟，再回助手核對。')
        elif action:
            raise SetupNeeded('已保存本機設定；稍後可重新開啟助手繼續。')
    mount = recheck(after_start=True)
    if not _verified_backend(config):
        raise SetupNeeded('未能驗證手機連接埠屬於這套 OFFICE。請核對同一安裝已啟動、已載入新版手機元件，再開助手。')
    if mount == 'empty':
        mobile_setup.mark_assistant_pending(config, target)
        _activate(binary, port)
    if _mount_state(binary, network, port) != 'existing' or not _verified_backend(config):
        raise SetupNeeded('尚未核對啟用結果。請重新開啟助手檢查。')
    mobile_setup.clear_assistant_pending(config, target)
    if legacy and ask('現在用這部電腦的瀏覽器開啟新的管理頁？輸入 O 開啟，或按 Enter 稍後開啟：').strip().casefold() == 'o':
        _open_owner_page(path)
    show('已核對電腦私人入口。請回 OFFICE 的「手機接入」，選擇權限並用手機相機掃碼。手機實際連線及收發仍需在手機確認。')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=DEFAULT_CONFIG, type=Path)
    parser.add_argument('--desktop', action='store_true', help='Keep the interactive terminal open to read the result')
    args = parser.parse_args(argv)
    if not sys.stdin.isatty():
        print('請從這部電腦開啟互動終端機執行手機設定助手。')
        return 2
    try:
        run_setup(args.config.absolute())
    except SetupNeeded as error:
        print(str(error))
        return 2
    except (ts.ProbeError, OSError, ValueError, EOFError, KeyboardInterrupt):
        print('未能核對本機狀態，請檢查 OFFICE 和 Tailscale，然後重新開啟助手。')
        return 2
    finally:
        if args.desktop:
            try:
                input('按 Enter 關閉助手。')
            except (EOFError, KeyboardInterrupt):
                pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
