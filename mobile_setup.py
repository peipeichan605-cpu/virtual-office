"""Read-only, owner-facing mobile setup observations.

Configuration, a locally running private listener, and a phone round trip are
different facts. This module never starts a frontend, creates private files,
authenticates an account, changes a VPN, or probes a user-supplied network URL.
"""
import json
from pathlib import Path

import phone_tsnet as frontend


def _transport(config):
    result = {'kind': 'unverified', 'state': 'unverified', 'running': False,
              'listening': False, 'configuration_matches': False}
    try:
        data = frontend._path(config.get('data_dir', ''))
        root = frontend._path(data / 'phone-tsnet')
        # No managed receipt may also mean an independently administered
        # Tailscale Serve installation. Absence is not evidence of a failure.
        if not root.exists():
            return result
        frontend._directory(data)
        frontend._directory(root)
        if not (root / 'active.json').exists():
            return result
        result.update(kind='tsnet', state='status_unavailable')
        record = frontend._active(root)
        if not record:
            return result
        # _public_status also reaps handles from _LOCAL_CHILDREN. A GET must
        # have no lifecycle side effects, so perform the same bounded process
        # identity check directly and only read immutable run metadata.
        actual_identity = frontend._process_identity(
            record['pid'], record['config_path'], record['binary'])
        if actual_identity != record['process_identity']:
            result['state'] = ('not_running' if frontend._pid_absent(record['pid'])
                               else 'process_identity_unknown')
            return result
        result['running'] = True
        worker_config = frontend._read_json(record['config_path'])
        allowed = {'mode', 'hostname', 'backend_port', 'core_port',
                   'expected_origin', 'expected_identity', 'allowed_node_ids',
                   'state_dir', 'status_path'}
        if set(worker_config) - allowed or worker_config.get('mode') != record['mode']:
            result['state'] = 'configuration_mismatch'
            return result
        selected = frontend.validate_settings(
            config, mode=record['mode'], hostname=worker_config.get('hostname'),
            origin=worker_config.get('expected_origin'),
            identity=worker_config.get('expected_identity'),
            node_ids=worker_config.get('allowed_node_ids'))
        mobile = config.get('mobile', {})
        paths_match = (worker_config.get('state_dir') == str(root / 'state') and
                       worker_config.get('status_path') == record['status_path'])
        settings_match = all(worker_config.get(key) == value for key, value in selected.items())
        serve_match = (record['mode'] == 'serve' and isinstance(mobile, dict) and
                       selected.get('expected_origin') == mobile.get('public_origin') and
                       selected.get('expected_identity') == mobile.get('identity'))
        if not paths_match or not settings_match or record['mode'] == 'serve' and not serve_match:
            result['state'] = 'configuration_mismatch'
            return result
        worker = frontend._read_json(record['status_path'])
        phase = worker.get('phase')
        if (worker.get('version') != 1 or worker.get('pid') != record['pid'] or
                phase not in frontend.PHASES or phase == 'ready' and record['mode'] != 'serve'):
            return result
        if not frontend._fresh_status(worker):
            result['state'] = 'status_stale'
            return result
        # A valid run config alone is not proof the authenticated node matches.
        own = worker.get('own')
        if phase == 'ready' and (not isinstance(own, dict) or
                own.get('identity') != selected.get('expected_identity') or
                'https://' + str(own.get('dns_name', '')) != selected.get('expected_origin')):
            result['state'] = 'configuration_mismatch'
            return result
        # A concurrent lifecycle change invalidates the observation rather
        # than allowing this GET to report the previous run as current.
        if frontend._active(root) != record:
            result['state'] = 'status_changed'
            return result
        result.update(state=phase, configuration_matches=serve_match,
                      listening=bool(serve_match and phase == 'ready'))
    except (frontend.FrontendError, OSError, ValueError, TypeError, KeyError):
        # Do not expose paths, account names, peer identities or native errors.
        result.update(state='status_unavailable', listening=False,
                      configuration_matches=False)
    return result


def connection_setup(config, *, configured=False):
    """Return safe owner UI facts, not an ACL or phone-reachability attestation."""
    transport = _transport(config)
    configured = configured is True
    listening = bool(configured and transport['listening'])
    # Unknown external frontends remain usable; the UI must label them as
    # unverified. Known managed failures must not be presented as ready.
    pairing_available = configured and (listening or transport['state'] == 'unverified')
    return {'version': 1, 'configuration_ready': configured,
            'transport': transport, 'local_listener_ready': listening,
            'pairing_available': pairing_available,
            'phone_verified': False, 'end_to_end_verified': False}


def host_setup_prompt(config, *, installation_root=None):
    """A local helper handoff with paths only; it never contains credentials."""
    root = Path(installation_root or Path(__file__).resolve().parent).absolute()
    config_path = config.get('config_path')
    if (not isinstance(config_path, str) or not Path(config_path).is_absolute() or
            any(ord(char) < 32 or ord(char) == 127 for char in config_path)):
        return None
    location = json.dumps(str(root), ensure_ascii=False)
    reference = json.dumps(config_path, ensure_ascii=False)
    return (
        '請協助設定這套 Virtual Office 的私人手機入口。先只讀檢查，沿用已完成的設定。\n'
        '安裝目錄：' + location + '\n'
        '本機設定檔：' + reference + '\n'
        '先讀安裝目錄內 docs/MOBILE-ACCESS.md 及 docs/MOBILE-TSNET.md；'
        '按本機實際狀態使用官方 Tailscale 私人連線與手機獨立權限，不能公開 OFFICE。\n'
        '先教用戶在 Android 的 Google Play 或 iPhone 的 App Store 安裝官方 Tailscale，'
        '使用同一個獲准帳戶登入、批准 VPN 設定，並確認顯示 Connected／已連線。'
        '登入、裝置批准及系統權限由用戶親自完成；不要索取密碼或驗證碼。\n'
        '如尚未設定主機入口，先提出具體設定結果供用戶確認：只准其選定手機、'
        '核對完整網絡政策，說明 HTTPS 憑證會公開主機名稱。不要自行批准所有裝置，'
        '不要關閉現有 VPN、改路由／DNS／防火牆，亦不要啟用 Funnel 或公開隧道。\n'
        '不要安裝重複服務或中斷工作；需要安裝依賴或重啟時先交代具體步驟。'
        '不要把設定檔內容、登入網址、配對連結或憑證貼進聊天。'
        '設定後請回到 OFFICE 的「手機接入」，讓用戶選權限及掃碼；'
        '分開回報本機入口、手機配對及實際收發驗證，不能把 ready 當成手機已連通。'
    )
