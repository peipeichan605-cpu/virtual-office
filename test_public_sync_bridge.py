"""Synthetic observer membership proof, never native App delivery evidence."""
import unittest
import test_child_events as fixtures


class PublicSyncBridgeTests(unittest.TestCase):
    setUp = fixtures.ChildEventsTests.setUp
    tearDown = fixtures.ChildEventsTests.tearDown
    root_start = fixtures.ChildEventsTests.root_start

    def test_bound_native_hook_is_distinct_from_outbound_mailbox(self):
        agent = self.roots['cursor']['id']
        capability = self.b.commands.capability(agent)
        self.assertFalse(capability['persistent'])
        self.assertTrue(self.b.public_sync_observer(agent))
        self.assertTrue(capability['public_sync']['manual_app_automatic'])
        self.assertEqual(capability['public_sync']['state'], 'configured')

    def test_pending_hook_is_not_an_active_manual_app_observer(self):
        agent = self.roots['cursor']['id']
        self.b.child_events.create_observer({'agent_id': agent, 'provider': 'cursor',
                                           'native_session_id': 'pending:fixture'})
        self.assertFalse(self.b.public_sync_observer(agent))
        capability = self.b.commands.capability(agent)['public_sync']
        self.assertFalse(capability['manual_app_automatic'])
        self.assertEqual(capability['state'], 'setup_required')

    def test_old_generation_or_inactive_observer_cannot_claim_coverage(self):
        agent = self.roots['cursor']['id']
        with self.b.history.db:
            self.b.history.db.execute("UPDATE observed_agent_roots SET generation='new-generation' WHERE root_id=?", (agent,))
        self.assertFalse(self.b.public_sync_observer(agent))
        self.assertFalse(self.b.commands.capability(agent)['public_sync']['automatic'])

    def test_grok_cooperative_reporting_is_never_a_native_subscription(self):
        agent = self.roots['grok_bot']['id']
        descriptor = self.b.commands.capability(agent)['public_sync']
        self.assertEqual(descriptor['mode'], 'cooperative')
        self.assertEqual(descriptor['scope'], 'reported_runs')
        self.assertFalse(descriptor['automatic'])
        self.assertFalse(descriptor['manual_app_automatic'])
        self.assertFalse(self.b.public_sync_observer(agent))

    def test_departed_root_cannot_reuse_an_active_observer_row(self):
        agent = self.roots['cursor']['id']
        self.b.definitions.pop(agent)
        self.assertFalse(self.b.public_sync_observer(agent))

    def test_restart_restores_hook_scope_before_any_new_report_arrives(self):
        from configuration import load_config
        from dashboard import Bridge
        agent = self.roots['cursor']['id']
        self.b.commands.close()
        self.b.history.close()
        self.b = Bridge(load_config(self.path), dry_run=True)
        capability = self.b.commands.capability(agent)['public_sync']
        self.assertEqual(capability['state'], 'configured')
        self.assertTrue(capability['manual_app_automatic'])


if __name__ == '__main__':
    unittest.main()
