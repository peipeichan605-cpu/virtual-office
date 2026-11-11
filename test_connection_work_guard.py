"""Changing a Grok route must respect its separately reported App work."""
import json
from pathlib import Path
import unittest
import uuid

from configuration import load_config
from connections import ConnectionError
import test_platforms as fixtures


class ConnectionReportedWorkTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp
    tearDown = fixtures.PlatformTests.tearDown
    grok = fixtures.PlatformTests.grok

    def report(self, agent, run, kind):
        return self.bridge.accept_agent_event(agent, {
            'source_id': agent['source_id'], 'event_id': uuid.uuid4().hex,
            'run_id': run, 'type': kind, 'text': 'Synthetic public App work.'})

    def route(self, agent):
        config = load_config(self.path)
        return Path(config['command_adapters']['grok_webhook']['sessions'][agent['source_id']]['credential_file'])

    def reconnect(self, agent, key='fixture-grok-replacement-key'):
        return self.bridge.connections.connect_grok({'agent_id': agent['id'],
            'url': 'https://api.cursor.com/fixture/webhook', 'key': key})

    def test_manual_app_work_blocks_route_change_without_mutation(self):
        agent = self.grok()
        self.report(agent, 'manual-work', 'turn_started')
        config_before = self.path.read_bytes()
        credential_before = self.route(agent).read_bytes()
        revision_before = self.bridge.get_connection_checks().status(agent['id'])['route_revision']
        with self.assertRaises(ConnectionError) as caught:
            self.reconnect(agent)
        self.assertEqual(caught.exception.code, 'connection_has_active_commands')
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.path.read_bytes(), config_before)
        self.assertEqual(self.route(agent).read_bytes(), credential_before)
        self.assertEqual(self.bridge.get_connection_checks().status(agent['id'])['route_revision'], revision_before)
        self.assertEqual(self.bridge.commands.db.execute('SELECT count(*) FROM commands').fetchone()[0], 0)

    def test_same_details_are_conservatively_blocked_during_manual_work(self):
        agent = self.grok()
        self.report(agent, 'manual-work', 'turn_started')
        with self.assertRaises(ConnectionError) as caught:
            self.reconnect(agent, fixtures.KEY)
        self.assertEqual(caught.exception.code, 'connection_has_active_commands')

    def test_manual_completion_releases_route_change(self):
        agent = self.grok()
        self.report(agent, 'manual-work', 'turn_started')
        self.report(agent, 'manual-work', 'turn_completed')
        result = self.reconnect(agent)
        self.assertTrue(result['connected'])
        self.assertEqual(json.loads(self.route(agent).read_text())['key'], 'fixture-grok-replacement-key')

    def test_other_selected_agent_work_does_not_block_this_route(self):
        agent = self.grok()
        invitation = self.bridge.invitations.create({'name': 'Other synthetic Agent',
            'provider': 'generic', 'request_id': 'other-work-fixture'})
        invite_path, _, _ = self.bridge.invitations.paths(invitation['id'])
        invite = json.loads(invite_path.read_text())
        self.bridge.invitations.claim(invitation['id'], invite['token'], {'claim_id': uuid.uuid4().hex})
        other = self.bridge.definitions[invitation['agent_id']]
        self.report(other, 'other-manual-work', 'turn_started')
        self.assertTrue(self.reconnect(agent)['connected'])


if __name__ == '__main__':
    unittest.main()
