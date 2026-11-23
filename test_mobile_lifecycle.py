"""Real loopback listeners and private temporary configs, without Tailscale or native agents."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import queue
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from configuration import initialize, load_config, save_config
from dashboard import Bridge
from mobile_lifecycle import MobileLifecycle, MobileLifecycleError
from mobile_setup import assistant_pending
from security_keys import matches_owner_key, upgraded_security


GATE = {'version': 1, 'host_id': 'host01', 'node_ids': ['phone01']}


class MobileLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / '.office' / 'config.json'
        with socket.socket() as core, socket.socket() as mobile:
            core.bind(('127.0.0.1', 0))
            mobile.bind(('127.0.0.1', 0))
            initialize(self.path, core.getsockname()[1])
            self.mobile = {'enabled': True, 'port': mobile.getsockname()[1],
                           'public_origin': 'https://office.fixture.ts.net', 'identity': 'test@example.test'}
        self.bridge = SimpleNamespace(config=load_config(self.path), stop=threading.Event(),
                                      feed_lock=threading.Lock(), mobile=SimpleNamespace(),
                                      commands=SimpleNamespace(lock=threading.RLock()),
                                      history=SimpleNamespace(lock=threading.RLock(), secrets=('saved',)),
                                      health={}, limit_until=0)
        self.manager = self.new_manager()

    def new_manager(self):
        manager = MobileLifecycle(self.bridge, self.root)
        self.addCleanup(manager.close)
        return manager

    def install(self, **updates):
        self.bridge.config.update(updates)
        save_config(self.path, self.bridge.config)

    def prepare(self):
        return self.manager.configure(self.mobile, peer_gate=GATE)

    def test_commit_does_not_replace_running_config_and_preserves_unrelated_writes(self):
        existing = self.bridge.config
        disk = load_config(self.path)
        disk['brand'] = 'Saved by an earlier writer'
        save_config(self.path, disk)
        with ThreadPoolExecutor(max_workers=2) as executor:
            for result in executor.map(lambda _: self.prepare(), [1, 2]):
                self.assertTrue(result['configured'])
        self.assertIs(existing, self.bridge.config)
        self.assertTrue(assistant_pending(existing))
        self.assertEqual(load_config(self.path)['brand'], disk['brand'])
        self.assertTrue(self.manager.listener_ready(self.mobile, GATE))
        with socket.create_connection(('127.0.0.1', self.mobile['port']), timeout=1):
            pass
        self.manager.close()
        manager = self.new_manager()
        manager.start_configured()
        self.assertTrue(manager.listener_ready(self.mobile, GATE))

    def test_on_failure_rollback_releases_listener_and_no_partial_configuration(self):
        initial = self.path.read_bytes()
        with patch('mobile_lifecycle.save_config', side_effect=OSError('fixture')):
            with self.assertRaises(MobileLifecycleError):
                self.prepare()
        self.assertEqual(self.path.read_bytes(), initial)
        self.assertNotIn('mobile', self.bridge.config)
        self.assertIsNone(self.manager.server)
        self.assertFalse(assistant_pending(dict(self.bridge.config, mobile=self.mobile)))
        self.assertTrue(self.prepare()['configured'])

    def test_occupied_port_preserves_config_and_desktop_can_retry(self):
        initial = self.path.read_bytes()
        with socket.socket() as other:
            other.bind(('127.0.0.1', self.mobile['port']))
            other.listen(1)
            with self.assertRaisesRegex(MobileLifecycleError, 'mobile_listener_unavailable'):
                self.prepare()
            self.assertEqual(self.path.read_bytes(), initial)
            self.assertTrue(self.manager.listener_error)
        self.prepare()
        self.assertFalse(self.manager.listener_error)

    def test_shutdown_does_not_release_port_during_activation(self):
        self.prepare()
        stopping = threading.Event()
        done = threading.Event()
        def close():
            stopping.set()
            self.manager.close()
            done.set()
        with self.manager.hold_listener(self.mobile, GATE):
            thread = threading.Thread(target=close)
            thread.start()
            self.assertTrue(stopping.wait(1))
            self.assertFalse(done.wait(.05))
        thread.join(timeout=2)
        self.assertTrue(done.is_set())
        with self.assertRaisesRegex(MobileLifecycleError, 'office_stopping'):
            self.prepare()

    def test_external_gate_cannot_be_adopted_and_managed_ids_extend_by_one(self):
        self.install(mobile=self.mobile)
        with self.assertRaisesRegex(MobileLifecycleError, 'mobile_existing_configuration'):
            self.prepare()
        self.bridge.config.pop('mobile')
        self.install()
        self.prepare()
        extended = dict(GATE, node_ids=['phone01', 'phone02'])
        self.manager.authorize_phone(self.mobile, peer_gate=extended)
        self.manager.authorize_phone(self.mobile, peer_gate=extended)
        self.assertTrue(self.manager.listener_ready(self.mobile, extended))
        before = self.path.read_bytes()
        for other in (GATE, dict(extended, host_id='other'), dict(extended, node_ids=['phone02', 'phone03'])):
            with self.assertRaises(MobileLifecycleError):
                self.manager.authorize_phone(self.mobile, peer_gate=other)
        self.assertEqual(self.path.read_bytes(), before)

    def test_live_legacy_rotation_invalidates_owner_and_preserves_report_redaction(self):
        for name in ('owner_key', 'internal_hmac_key'):
            self.bridge.config.pop(name)
        old = self.bridge.config['topic']
        self.install(feed='https://ntfy.sh/' + old)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(MobileLifecycleError, 'mobile_owner_upgrade_required'):
            self.prepare()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.manager.upgrade_legacy(), {'credentials_rotated': True})
        current = self.bridge.config
        self.assertFalse(matches_owner_key(current, old))
        self.assertTrue(upgraded_security(current))
        self.assertIsNone(current['feed'])
        self.assertEqual(load_config(self.path)['owner_key'], current['owner_key'])
        for v in (old, current['owner_key'], current['internal_hmac_key'], 'saved'):
            self.assertIn(v, self.bridge.history.secrets)
        self.assertEqual(self.manager.upgrade_legacy(), {'credentials_rotated': False})

    def test_startup_failure_does_not_start_a_partial_phone_service(self):
        self.install(mobile=self.mobile, mobile_peer_gate={'version': False})
        self.manager.start_configured()
        self.assertTrue(self.manager.listener_error)
        self.assertIsNone(self.manager.server)

    def test_feed_cutoff_waits_for_inflight_publisher_and_drops_old_queue(self):
        b = self.bridge
        for key in ('internal_hmac_key', 'owner_key'):
            b.config.pop(key)
        self.install(feed='https://ntfy.sh/' + b.config['topic'])
        with ThreadPoolExecutor(max_workers=1) as executor:
            with b.feed_lock:
                result = executor.submit(self.manager.upgrade_legacy)
                self.assertFalse(result.done())
            self.assertTrue(result.result(timeout=2)['credentials_rotated'])
        b.outbound = queue.Queue()
        b.outbound.put({'bot': 'test'})
        # Leave the loop immediately after the queued pre-upgrade item is
        # accounted for. A real publisher call here would require a live feed.
        completed = b.outbound.task_done
        def finish():
            completed()
            b.stop.set()
        with patch.object(b.outbound, 'task_done', finish), patch('urllib.request.urlopen') as network:
            Bridge.publish_loop(b)
            network.assert_not_called()
        self.assertEqual(b.outbound.unfinished_tasks, 0)


if __name__ == '__main__':
    unittest.main()
