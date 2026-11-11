"""Cross-flow fixture evidence, not proof of Grok native App observation."""
import json
import unittest
import uuid
from commands import Commands
from event_ingest import EventConflict
import test_platforms as fixtures


class OfficeReliabilityFlowTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp
    tearDown = fixtures.PlatformTests.tearDown
    grok = fixtures.PlatformTests.grok

    def report(self, agent, run, kind, text, **extra):
        return self.bridge.accept_agent_event(agent, {
            'source_id': agent['source_id'], 'run_id': run,
            'event_id': run + ':' + kind + ':' + str(uuid.uuid4()),
            'type': kind, 'text': text, **extra})

    def test_manual_reports_office_dispatch_history_restart_and_logout_share_one_contract(self):
        agent = self.grok()
        descriptor = self.bridge.commands.capability(agent['id'])['public_sync']
        self.assertFalse(descriptor['automatic'])
        self.assertFalse(descriptor['manual_app_automatic'])
        calls = []
        class Transport:
            def open(self, method, url, headers, body):
                calls.append(json.loads(body))
                return fixtures.Connection(), fixtures.Response()
        self.bridge.commands.adapters['grok_webhook'].transport = Transport()
        self.report(agent, 'manual-fixture', 'turn_started', 'Public manual task password=private-value')
        self.report(agent, 'manual-fixture', 'message', 'Public progress', role='assistant', phase='commentary')
        queued = {'request_id': str(uuid.uuid4()), 'agent_id': agent['id'], 'text': 'Next Office task'}
        self.bridge.commands.create(queued)
        self.bridge.commands.tick()
        row = self.bridge.commands.db.execute('SELECT * FROM commands WHERE id=?', (queued['request_id'],)).fetchone()
        self.assertEqual((row['state'], row['detail_code']), ('pending', 'waiting_for_reported_work'))
        self.assertEqual(calls, [])
        snapshot = next(a for a in self.bridge.work_snapshot()['agents'] if a['id'] == agent['id'])
        self.assertEqual(snapshot['state'], 'working')
        self.report(agent, 'manual-fixture', 'message', 'Manual delivery', role='assistant', phase='final')
        self.report(agent, 'manual-fixture', 'turn_completed', 'Manual delivery')
        self.bridge.commands.tick()
        self.assertEqual(len(calls), 1)
        command_id = queued['request_id']
        self.report(agent, command_id, 'turn_started', calls[0]['prompt']['text'])
        self.report(agent, command_id, 'message', 'Office delivery', role='assistant', phase='final')
        self.report(agent, command_id, 'turn_completed', 'Office delivery')
        command = self.bridge.commands.db.execute('SELECT * FROM commands WHERE id=?', (command_id,)).fetchone()
        self.assertEqual(command['state'], 'completed')
        self.assertTrue(self.bridge.commands.create(queued)['duplicate'])
        self.bridge.commands.tick()
        self.assertEqual(len(calls), 1)
        messages = self.bridge.history.messages({agent['id']}, agent['id'])
        self.assertEqual(sum(m['text'] == 'Manual delivery' for m in messages), 1)
        self.assertEqual(sum(m['text'] == 'Office delivery' for m in messages), 1)
        self.assertNotIn('private-value', json.dumps(messages))
        seq = next(m['seq'] for m in messages if m['text'] == 'Office delivery')
        self.assertFalse(any(m['text'] == 'Office delivery' for m in
            self.bridge.history.messages({agent['id']}, agent['id'], after=seq)))
        count = self.bridge.history.db.execute('SELECT count(*) FROM runs').fetchone()[0]
        self.bridge.commands.close()
        self.bridge.commands = Commands(self.bridge.config, self.bridge.history)
        self.bridge.get_connection_checks()
        self.bridge.commands.adapters['grok_webhook'].transport = Transport()
        self.bridge.commands.tick()
        self.assertEqual(len(calls), 1, 'Restart must not replay a delivered command')
        self.assertEqual(self.bridge.history.db.execute('SELECT count(*) FROM runs').fetchone()[0], count)

        self.bridge.members.logout({'agent_id': agent['id']})
        self.assertFalse(self.bridge.commands.capability(agent['id'])['can_send'])
        self.assertEqual(self.bridge.commands.capability(agent['id'])['public_sync']['state'], 'unsupported')
        with self.assertRaises(EventConflict):
            self.report(agent, 'retired-fixture', 'turn_started', 'Must not return')
        self.assertEqual(self.bridge.history.db.execute('SELECT count(*) FROM runs').fetchone()[0], count)

    def test_unrelated_app_start_cannot_strand_an_already_delivered_office_job(self):
        agent = self.grok()
        calls = []
        class Transport:
            def open(self, method, url, headers, body):
                calls.append(json.loads(body))
                return fixtures.Connection(), fixtures.Response()
        self.bridge.commands.adapters['grok_webhook'].transport = Transport()
        ident = str(uuid.uuid4())
        self.bridge.commands.create({'request_id': ident, 'agent_id': agent['id'], 'text': 'Office task'})
        self.bridge.commands.tick()
        for stage in ('before-start', 'working'):
            if stage == 'working':
                self.report(agent, ident, 'turn_started', calls[0]['prompt']['text'])
            before = self.bridge.history.db.execute('SELECT count(*) FROM runs').fetchone()[0]
            with self.assertRaisesRegex(EventConflict, 'grok_work_in_progress'):
                self.report(agent, 'unrelated-' + stage, 'turn_started', 'Another App chat')
            self.assertEqual(self.bridge.history.db.execute('SELECT count(*) FROM runs').fetchone()[0], before)
        self.report(agent, ident, 'turn_completed', 'Office finished')
        row = self.bridge.commands.db.execute('SELECT state FROM commands WHERE id=?', (ident,)).fetchone()
        self.assertEqual(row['state'], 'completed')
        self.report(agent, 'manual-after', 'turn_started', 'Later App task')
        self.report(agent, 'manual-after', 'turn_completed', 'Later public delivery')


if __name__ == '__main__':
    unittest.main()
