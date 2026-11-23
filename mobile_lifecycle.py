"""Own the loopback phone listener for the lifetime of one OFFICE process."""
from contextlib import contextmanager
import os
from pathlib import Path
import threading

from configuration import load_config, save_config
from mobile_gateway import create_server, settings
from mobile_setup import assistant_pending, clear_assistant_pending, mark_assistant_pending
from security_keys import security_secrets, upgrade_security, upgraded_security


class MobileLifecycleError(Exception):
    def __init__(self, code, status=409):
        self.code = code
        self.status = status
        super().__init__(code)


class MobileLifecycle:
    def __init__(self, bridge, public_root):
        self.bridge = bridge
        self.public_root = public_root
        self.lock = threading.RLock()
        self.server = self.thread = None
        self.bound_settings = self.bound_host = None
        self.closed = False
        self.listener_error = False

    def _open(self, candidate):
        server = create_server(self.bridge, self.public_root, prepared_config=candidate)
        if server is None:
            raise ValueError('Mobile listener not enabled')
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1},
                                  name='office-mobile-gateway', daemon=True)
        try:
            thread.start()
        except BaseException:
            server.server_close()
            raise
        self.server, self.thread = server, thread
        self.bound_settings = settings(candidate)
        self.bound_host = (candidate.get('mobile_peer_gate') or {}).get('host_id')

    def start_configured(self):
        """An unavailable phone port must not take down the desktop or native Agents."""
        with self.lock:
            if self.closed or self.server is not None:
                return
            try:
                if settings(self.bridge.config)['enabled']:
                    self._open(self.bridge.config)
                    self.listener_error = False
            except (OSError, ValueError, RuntimeError):
                self.listener_error = True

    def _stop_listener(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()
        self.server = self.thread = self.bound_settings = self.bound_host = None

    def close(self):
        with self.lock:
            self.closed = True
            self._stop_listener()

    def _target(self, mobile, gate):
        from phone_peer_gate import validate_peer_gate
        try:
            selected = settings(dict(self.bridge.config, mobile=mobile))
            if selected != mobile or not selected['enabled']:
                raise ValueError('Missing mobile settings')
            return selected, validate_peer_gate(gate)
        except (TypeError, ValueError, KeyError):
            raise MobileLifecycleError('invalid_mobile_configuration', 400) from None

    def listener_ready(self, mobile, peer_gate):
        with self.lock:
            selected, gate = self._target(mobile, peer_gate)
            return bool(not self.closed and not self.bridge.stop.is_set() and self.server and
                        self.thread.is_alive() and self.server.fileno() >= 0 and
                        self.server.server_address == ('127.0.0.1', selected['port']) and
                        self.bound_settings == selected and self.bound_host == gate['host_id'] and
                        settings(self.bridge.config) == selected and
                        self.bridge.config.get('mobile_peer_gate') == gate)

    @contextmanager
    def hold_listener(self, mobile, peer_gate):
        """Keep the verified port reserved until a bounded Serve operation finishes."""
        with self.lock:
            if not self.listener_ready(mobile, peer_gate):
                raise MobileLifecycleError('mobile_listener_unavailable')
            yield

    @contextmanager
    def _edit(self):
        from phone_setup_assistant import SetupNeeded, _setup_lock
        b = self.bridge
        # Native Agent lifecycle writers take these locks in command/history
        # order. The setup file lock also excludes the standalone assistant.
        with self.lock, b.feed_lock, b.commands.lock, b.history.lock:
            if self.closed or b.stop.is_set():
                raise MobileLifecycleError('office_stopping')
            try:
                with _setup_lock(b.config):
                    disk = load_config(b.config['config_path'])
                    for key in ('port', 'host', 'data_dir', 'feed', 'topic', 'owner_key',
                                'internal_hmac_key', 'legacy_integrity_keys', 'mobile', 'mobile_peer_gate'):
                        if disk.get(key) != b.config.get(key) or (key in disk) != (key in b.config):
                            raise MobileLifecycleError('mobile_configuration_changed')
                    yield disk
            except SetupNeeded:
                raise MobileLifecycleError('mobile_setup_busy') from None
            except (OSError, ValueError):
                raise MobileLifecycleError('mobile_configuration_unavailable', 503) from None

    def _commit(self, disk, changes):
        candidate = dict(disk, **changes)
        save_config(disk['config_path'], candidate)
        # Every store/adapter referring to the live config observes the same
        # update. Unrelated live Agent state and disk-only settings are retained.
        self.bridge.config.update(changes)
        if 'owner_key' in changes:
            history = self.bridge.history
            history.secrets = tuple(dict.fromkeys((*history.secrets, *security_secrets(candidate))))
            self.bridge.limit_until = 0
            self.bridge.health.pop('last_publish_error', None)

    @staticmethod
    def _upgraded_fields(disk):
        if 'mobile' in disk or 'mobile_peer_gate' in disk:
            raise MobileLifecycleError('mobile_existing_configuration')
        if os.path.lexists(Path(disk['data_dir']) / 'phone-tsnet'):
            raise MobileLifecycleError('mobile_existing_configuration')
        migrated = upgrade_security(disk)
        return {k: migrated[k] for k in ('owner_key', 'internal_hmac_key', 'legacy_integrity_keys', 'feed')}

    def upgrade_legacy(self, *, consent_valid=None):
        """Caller must have obtained independent local desktop approval."""
        with self._edit() as disk:
            if upgraded_security(disk):
                return {'credentials_rotated': False}
            if consent_valid is not None and not consent_valid():
                raise MobileLifecycleError('local_consent_expired')
            self._commit(disk, self._upgraded_fields(disk))
            return {'credentials_rotated': True}

    def configure(self, mobile, *, peer_gate):
        """First-time application after host discovery, owner consent and preflight."""
        mobile, gate = self._target(mobile, peer_gate)
        with self._edit() as disk:
            if not upgraded_security(disk):
                raise MobileLifecycleError('mobile_owner_upgrade_required', 403)
            if os.path.lexists(Path(disk['data_dir']) / 'phone-tsnet'):
                raise MobileLifecycleError('mobile_existing_configuration')
            existing = 'mobile' in disk or 'mobile_peer_gate' in disk
            if existing and (settings(disk) != mobile or disk.get('mobile_peer_gate') != gate):
                raise MobileLifecycleError('mobile_existing_configuration')
            if not existing and len(gate['node_ids']) != 1:
                raise MobileLifecycleError('invalid_mobile_configuration', 400)
            if self.listener_ready(mobile, gate):
                return {'configured': True, 'credentials_rotated': False}
            if self.server is not None:
                raise MobileLifecycleError('mobile_listener_unavailable')
            changes = {'mobile': mobile, 'mobile_peer_gate': gate}
            candidate = dict(disk, **changes)
            already_pending = assistant_pending(candidate)
            try:
                self._open(candidate)
            except (OSError, RuntimeError, ValueError):
                self.listener_error = True
                raise MobileLifecycleError('mobile_listener_unavailable') from None
            try:
                if not existing:
                    mark_assistant_pending(disk, mobile)
                    self._commit(disk, changes)
            except BaseException:
                self._stop_listener()
                if not already_pending and not existing:
                    clear_assistant_pending(disk, mobile)
                raise
            self.listener_error = False
            return {'configured': True, 'credentials_rotated': False}

    def authorize_phone(self, mobile, *, peer_gate):
        mobile, gate = self._target(mobile, peer_gate)
        with self._edit() as disk:
            previous = disk.get('mobile_peer_gate')
            if not previous or not self.listener_ready(mobile, previous):
                raise MobileLifecycleError('mobile_listener_unavailable')
            if previous == gate:
                return {'configured': True}
            old_nodes, new_nodes = set(previous['node_ids']), set(gate['node_ids'])
            if (gate['host_id'] != previous['host_id'] or not old_nodes < new_nodes or
                    len(new_nodes - old_nodes) != 1):
                raise MobileLifecycleError('mobile_existing_configuration')
            self._commit(disk, {'mobile_peer_gate': gate})
            return {'configured': True}
