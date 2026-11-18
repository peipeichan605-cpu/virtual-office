import json
import math
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest

from mobile_auth import MobileAuth, MobileAuthError


AGENT = 'codex:00000000-0000-4000-8000-000000000001'
OTHER = 'grok_bot:sdk-00000000-0000-4000-8000-000000000002'
IDENTITY = 'owner@example.invalid'


class Clock:
    def __init__(self): self.now = 1800000000.0
    def __call__(self): return self.now
    def advance(self, value): self.now += value


class MobileAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.selected = {AGENT: 'enrollment-a', OTHER: 'enrollment-b'}
        self.config = {'data_dir': str(self.root), 'topic': 'owner-secret-never-for-mobile'}
        self.auth = MobileAuth(self.config, selected_agents=lambda: self.selected, clock=self.clock)

    def tearDown(self):
        self.auth.close()
        self.temp.cleanup()

    def pair(self, view=None, send=None, **options):
        record = self.auth.create_pairing(IDENTITY, 'My phone', view or [AGENT], [AGENT] if send is None else send)
        return self.auth.pair(record['pairing_token'], IDENTITY, **options)

    def principal(self, pairing, write=False):
        return self.auth.authenticate(pairing['session_token'], IDENTITY, csrf=pairing['csrf_token'], write=write)

    def error(self, code, fn, *args, **kwargs):
        with self.assertRaises(MobileAuthError) as error:
            fn(*args, **kwargs)
        self.assertEqual(error.exception.code, code)
        return error.exception.status

    def test_one_use_pairing_binds_exact_identity_and_current_enrollment(self):
        invitation = self.auth.create_pairing(IDENTITY, 'My phone', [AGENT, OTHER], [AGENT])
        token = invitation['pairing_token']
        self.error('invalid_mobile_pairing', self.auth.pair, token, 'intruder@example.invalid')
        device = self.auth.pair(token, IDENTITY)
        self.error('invalid_mobile_pairing', self.auth.pair, token, IDENTITY)
        self.assertEqual(device['device']['view_agents'], [AGENT, OTHER])
        self.assertEqual(device['device']['send_agents'], [AGENT])
        self.error('mobile_auth_required', self.auth.authenticate, device['session_token'], IDENTITY.upper())

    def test_database_and_public_objects_do_not_persist_auth_secrets(self):
        invitation = self.auth.create_pairing(IDENTITY, 'Phone', [AGENT], [AGENT])
        token = invitation['pairing_token']
        self.assertNotIn(token.encode(), (self.root/'mobile-auth.sqlite3').read_bytes())
        pairing = self.auth.pair(token, IDENTITY)
        principal = self.principal(pairing)
        public = json.dumps([self.auth.list_devices(), self.auth.audit_events(), principal])
        raw = (self.root/'mobile-auth.sqlite3').read_bytes()
        for secret in (token, pairing['session_token'], pairing['csrf_token'], self.config['topic']):
            self.assertNotIn(secret, public)
            self.assertNotIn(secret.encode(), raw)
        self.assertEqual(stat.S_IMODE((self.root/'mobile-auth.sqlite3').stat().st_mode), 0o600)
        self.assertEqual(set(self.auth.audit_events()[0]), {'seq', 'at', 'event', 'device_id', 'reason'})

    def test_pairing_expires_and_member_generation_change_requires_new_pairing(self):
        invitation = self.auth.create_pairing(IDENTITY, 'Phone', [AGENT], [AGENT])
        self.selected[AGENT] = 'new-enrollment'
        self.error('mobile_pairing_membership_changed', self.auth.pair, invitation['pairing_token'], IDENTITY)
        self.clock.advance(300)
        self.error('invalid_mobile_pairing', self.auth.pair, invitation['pairing_token'], IDENTITY)

    def test_send_requires_csrf_and_explicit_send_scope(self):
        pairing = self.pair(view=[AGENT, OTHER])
        read = self.principal(pairing)
        self.auth.authorize(read, OTHER, 'view')
        self.error('mobile_csrf_required', self.auth.authorize, read, AGENT, 'send')
        self.error('mobile_csrf_required', self.auth.authenticate, pairing['session_token'], IDENTITY, write=True)
        self.error('mobile_csrf_required', self.auth.authenticate, pairing['session_token'], IDENTITY, csrf='0'*64, write=True)
        writer = self.principal(pairing, write=True)
        self.auth.authorize(writer, AGENT, 'send')
        self.error('mobile_agent_forbidden', self.auth.authorize, writer, OTHER, 'send')
        self.error('mobile_agent_forbidden', self.auth.authorize, writer, 'codex:unselected', 'view')

    def test_expiry_is_absolute_even_with_activity_and_idle_expiry_cannot_revive(self):
        pairing = self.pair()
        for _ in range(23):
            self.clock.advance(3500)
            self.principal(pairing)
        self.clock.advance(86400 - (23*3500))
        self.error('mobile_auth_required', self.principal, pairing)
        fresh = self.pair()
        self.clock.advance(3600)
        self.error('mobile_auth_required', self.principal, fresh)
        self.assertEqual(self.auth.list_devices()[0]['state'], 'expired')

    def test_revocation_invalidates_existing_principals_and_pending_origins(self):
        pairing = self.pair()
        writer = self.principal(pairing, write=True)
        origin = self.auth.command_origin(writer, AGENT)
        self.assertTrue(self.auth.validate_command_origin(origin, AGENT))
        revoked = self.auth.revoke(writer['device_id'])
        self.assertEqual(revoked['state'], 'revoked')
        self.assertEqual(revoked['revision'], 2)
        self.assertEqual(self.auth.revoke(writer['device_id'])['revision'], 2)
        self.error('mobile_auth_required', self.auth.refresh, writer)
        self.error('mobile_auth_required', self.auth.authorize, writer, AGENT)
        self.error('mobile_auth_required', self.principal, pairing)
        self.assertFalse(self.auth.validate_command_origin(origin, AGENT))

    def test_membership_changes_do_not_grant_new_agents_or_rejoined_identity(self):
        pairing = self.pair(view=[AGENT, OTHER], send=[AGENT, OTHER])
        writer = self.principal(pairing, write=True)
        self.selected['cursor:new'] = 'new-enrollment'
        self.error('mobile_agent_forbidden', self.auth.authorize, writer, 'cursor:new')
        self.selected[AGENT] = 'changed-generation'
        self.error('mobile_agent_forbidden', self.auth.authorize, writer, AGENT, 'send')
        refreshed = self.auth.refresh(writer)
        self.assertEqual(refreshed['view_agents'], [OTHER])
        self.assertFalse(self.auth.validate_command_origin({**self.auth.command_origin(writer, OTHER), 'agent_id': AGENT}, AGENT))

    def test_logout_invalidation_fences_same_identity_rejoin_and_unclaimed_pairings(self):
        pairing = self.pair(view=[AGENT, OTHER], send=[AGENT, OTHER])
        writer = self.principal(pairing, write=True)
        origin = self.auth.command_origin(writer, AGENT)
        invitation = self.auth.create_pairing(IDENTITY, 'Other phone', [AGENT], [AGENT])
        self.assertEqual(self.auth.invalidate_agent(AGENT), [writer['device_id']])
        self.assertFalse(self.auth.validate_command_origin(origin, AGENT))
        self.error('invalid_mobile_pairing', self.auth.pair, invitation['pairing_token'], IDENTITY)
        self.assertEqual(self.error('mobile_scope_changed', self.auth.refresh, writer), 409)
        current = self.principal(pairing, write=True)
        self.assertEqual(current['send_agents'], [OTHER])
        self.error('mobile_agent_forbidden', self.auth.authorize, current, AGENT)
        self.auth.authorize(current, OTHER, 'send')

    def test_origin_expiry_and_identity_validation_are_strict_and_non_sliding(self):
        pairing = self.pair()
        origin = self.auth.command_origin(self.principal(pairing, write=True), AGENT)
        self.assertEqual(origin['expires_at'], self.clock() + 900)
        invalids = [None, {}, {**origin, 'expires_at': math.inf}, {**origin, 'expires_at': True},
                    {**origin, 'revision': True}, {**origin, 'revision': 999},
                    {**origin, 'expires_at': self.clock()+901}, {**origin, 'secret': 'unexpected'},
                    {**origin, 'device_id': 'phone-other'}, {**origin, 'agent_id': OTHER}]
        for invalid in invalids:
            self.assertFalse(self.auth.validate_command_origin(invalid, AGENT))
        last_seen = self.auth.list_devices()[0]['last_seen']
        self.clock.advance(899)
        self.assertTrue(self.auth.validate_command_origin(origin, AGENT))
        self.assertEqual(self.auth.list_devices()[0]['last_seen'], last_seen)
        self.clock.advance(1)
        self.assertFalse(self.auth.validate_command_origin(origin, AGENT))

    def test_public_dict_cannot_be_forged_into_a_principal(self):
        pairing = self.pair()
        principal = self.principal(pairing, write=True)
        self.error('mobile_auth_required', self.auth.authorize, dict(principal), AGENT, 'send')
        principal['device_id'] = 'phone-other'
        self.error('mobile_auth_required', self.auth.refresh, principal)

    def test_csrf_survives_page_reload_and_restart_without_plaintext_storage(self):
        pairing = self.pair()
        self.assertEqual(self.auth.session_status(pairing['session_token'], IDENTITY)['csrf_token'], pairing['csrf_token'])
        self.auth.close()
        self.auth = MobileAuth(self.config, selected_agents=lambda: self.selected, clock=self.clock)
        self.assertEqual(self.auth.csrf_for(pairing['session_token'], IDENTITY), pairing['csrf_token'])
        self.auth.authorize(self.principal(pairing, write=True), AGENT, 'send')

    def test_invalid_scope_does_not_expand_permissions(self):
        examples = [([], []), ([AGENT, AGENT], [AGENT]), ([AGENT], [OTHER]),
                    ([AGENT], [AGENT, AGENT]), ('all', []), ([5], [])]
        for views, sends in examples:
            self.error('invalid_mobile_scope', self.auth.create_pairing, IDENTITY, 'Phone', views, sends)
        self.error('mobile_agent_not_selected', self.auth.create_pairing, IDENTITY, 'Phone', ['codex:unknown'], [])
        self.error('mobile_identity_required', self.auth.create_pairing, 'owner\nforged', 'Phone', [AGENT], [])
        self.error('invalid_mobile_label', self.auth.create_pairing, IDENTITY, '\nPhone', [AGENT], [])

    def test_enrolled_view_follows_current_roster_without_expanding_selected_send(self):
        invitation = self.auth.create_pairing(IDENTITY, 'Follow office', [], [AGENT], view_mode='enrolled')
        paired = self.auth.pair(invitation['pairing_token'], IDENTITY)
        writer = self.principal(paired, write=True)
        self.assertEqual(writer['view_agents'], sorted(self.selected))
        self.assertEqual(writer['send_agents'], [AGENT])
        self.selected['cursor:new'] = 'new-enrollment'
        self.auth.authorize(writer, 'cursor:new', 'view')
        self.error('mobile_agent_forbidden', self.auth.authorize, writer, 'cursor:new', 'send')
        self.selected.pop(OTHER)
        self.error('mobile_agent_forbidden', self.auth.authorize, writer, OTHER)
        # A re-enrolled target is visible again, but the old selected send grant
        # is never silently refreshed by the new view policy.
        self.selected[AGENT] = 'new-generation'
        fresh = self.auth.refresh(writer)
        self.assertIn(AGENT, fresh['view_agents'])
        self.assertEqual(fresh['send_agents'], [])

    def test_enrolled_send_fences_removed_and_identically_rejoined_targets_across_restart(self):
        invitation = self.auth.create_pairing(IDENTITY, 'Follow office', [], [],
                                               view_mode='enrolled', send_mode='enrolled')
        paired = self.auth.pair(invitation['pairing_token'], IDENTITY)
        writer = self.principal(paired, write=True)
        origin = self.auth.command_origin(writer, AGENT)
        other_origin = self.auth.command_origin(writer, OTHER)
        self.assertRegex(origin['membership'], r'^[0-9a-f]{64}$')
        self.assertTrue(self.auth.validate_command_origin(origin, AGENT))
        missing = dict(origin); missing.pop('membership')
        self.assertFalse(self.auth.validate_command_origin(missing, AGENT))
        generation = self.selected.pop(AGENT)
        self.assertFalse(self.auth.validate_command_origin(origin, AGENT))
        self.assertEqual(self.auth.invalidate_agent(AGENT), [])
        self.auth.close()
        self.auth = MobileAuth(self.config, selected_agents=lambda: self.selected, clock=self.clock)
        self.selected[AGENT] = generation
        self.assertFalse(self.auth.validate_command_origin(origin, AGENT))
        self.assertTrue(self.auth.validate_command_origin(other_origin, OTHER))
        writer = self.principal(paired, write=True)
        self.assertEqual(writer['revision'], 1)
        fresh_origin = self.auth.command_origin(writer, AGENT)
        self.assertNotEqual(fresh_origin['membership'], origin['membership'])
        self.assertTrue(self.auth.validate_command_origin(fresh_origin, AGENT))
        self.auth.revoke(writer['device_id'])
        self.assertFalse(self.auth.validate_command_origin(fresh_origin, AGENT))

    def test_owner_scope_change_keeps_cookie_csrf_and_fences_previous_revision(self):
        paired = self.pair()
        writer = self.principal(paired, write=True)
        origin = self.auth.command_origin(writer, AGENT)
        result = self.auth.update_scopes(writer['device_id'], [], [AGENT], 'enrolled', 'selected')
        self.assertTrue(result['changed'])
        self.assertEqual(result['device']['revision'], 2)
        self.assertEqual(self.error('mobile_scope_changed', self.auth.refresh, writer), 409)
        self.assertFalse(self.auth.validate_command_origin(origin, AGENT))
        fresh = self.principal(paired, write=True)
        self.assertEqual(fresh['view_agents'], sorted(self.selected))
        self.assertEqual(fresh['send_agents'], [AGENT])
        self.assertEqual(self.auth.csrf_for(paired['session_token'], IDENTITY), paired['csrf_token'])
        unchanged = self.auth.update_scopes(fresh['device_id'], [], [AGENT], 'enrolled', 'selected')
        self.assertFalse(unchanged['changed'])
        self.assertEqual(unchanged['device']['revision'], 2)
        self.auth.revoke(fresh['device_id'])
        self.error('mobile_device_unavailable', self.auth.update_scopes, fresh['device_id'], [AGENT], [])

    def test_modes_are_explicit_fail_closed_and_do_not_change_existing_phones(self):
        paired = self.pair()
        self.assertEqual(paired['device']['view_mode'], 'selected')
        self.assertEqual(paired['device']['send_mode'], 'selected')
        for view_mode, send_mode in [('all', 'selected'), ('selected', 'enrolled'), (None, 'selected')]:
            self.error('invalid_mobile_scope', self.auth.create_pairing, IDENTITY, 'Invalid', [AGENT], [],
                       view_mode=view_mode, send_mode=send_mode)
        self.error('mobile_agent_not_selected', self.auth.update_scopes,
                   paired['device']['device_id'], [], ['cursor:unregistered'], 'enrolled', 'selected')
        self.error('invalid_mobile_device', self.auth.update_scopes, {}, [AGENT], [])
        self.assertEqual(self.principal(paired)['revision'], 1)
        self.selected['cursor:new'] = 'new-enrollment'
        self.assertNotIn('cursor:new', self.principal(paired)['view_agents'])

    def test_redundant_enrolled_arrays_never_bind_unrelated_roster_members(self):
        def invite():
            return self.auth.create_pairing(IDENTITY, 'Dynamic', [AGENT, OTHER], [AGENT, OTHER],
                                             view_mode='enrolled', send_mode='enrolled')
        first = invite()
        self.assertEqual(first['view_agents'], [])
        self.assertEqual(first['send_agents'], [])
        paired = self.auth.pair(first['pairing_token'], IDENTITY)
        other_origin = self.auth.command_origin(self.principal(paired, write=True), OTHER)
        pending = invite()
        self.selected.pop(AGENT)
        self.assertEqual(self.auth.invalidate_agent(AGENT), [])
        self.assertEqual(self.principal(paired)['revision'], 1)
        self.assertTrue(self.auth.validate_command_origin(other_origin, OTHER))
        self.assertEqual(self.auth.pair(pending['pairing_token'], IDENTITY)['device']['view_agents'], [OTHER])

    def test_legacy_database_migration_preserves_selected_permissions(self):
        paired = self.pair()
        # Construct the exact pre-policy schema while preserving existing rows.
        for table in ('mobile_pairings', 'mobile_devices'):
            self.auth.db.execute('ALTER TABLE ' + table + ' DROP COLUMN view_mode')
            self.auth.db.execute('ALTER TABLE ' + table + ' DROP COLUMN send_mode')
        self.auth.db.execute('DROP TABLE mobile_membership_epochs')
        self.auth.close()
        self.auth = MobileAuth(self.config, selected_agents=lambda: self.selected, clock=self.clock)
        self.selected['cursor:new'] = 'new-enrollment'
        principal = self.principal(paired, write=True)
        self.assertEqual(principal['view_mode'], 'selected')
        self.assertEqual(principal['send_mode'], 'selected')
        self.assertEqual(principal['view_agents'], [AGENT])
        self.assertEqual(principal['send_agents'], [AGENT])
        self.assertNotIn('membership', self.auth.command_origin(principal, AGENT))

    def test_rate_limits_are_bounded_and_do_not_evict_active_limits(self):
        for _ in range(30): self.auth.allow_request('device-a', 'write')
        self.assertEqual(self.error('mobile_rate_limited', self.auth.allow_request, 'device-a', 'write'), 429)
        self.auth.MAX_RATE_BUCKETS = 4
        self.auth.allow_request('device-b', 'write')
        self.auth.allow_request('device-c', 'write')
        self.error('mobile_rate_limited', self.auth.allow_request, 'device-d', 'write')
        self.assertEqual(len(self.auth._rates), 4)
        self.clock.advance(60)
        self.auth.allow_request('device-a', 'write')
        self.assertEqual(len(self.auth._rates), 2)

    def test_three_dimensional_startup_and_normal_polling_fit_separate_budgets(self):
        pairing = self.pair()
        device = pairing['device']['device_id']
        # One cold 3D load, followed in the same minute by one work read/second,
        # two dialogue reads/second, one command read/second and 30 history reads.
        for _ in range(90):
            self.auth.allow_request(IDENTITY, 'asset')
            self.auth.authenticate(pairing['session_token'], IDENTITY)
        for count in (60, 120, 60, 30):
            for _ in range(count):
                principal = self.auth.authenticate(pairing['session_token'], IDENTITY)
                self.auth.allow_request(principal['device_id'], 'read')
                self.auth.authorize(principal, AGENT)
        self.assertEqual(principal['device_id'], device)
        # Dispatch keeps its tighter budget even when read traffic is busy.
        for _ in range(30): self.auth.allow_request(device, 'write')
        self.error('mobile_rate_limited', self.auth.allow_request, device, 'write')

    def test_exhausted_asset_budget_does_not_consume_device_api_read_budget(self):
        for _ in range(240): self.auth.allow_request(IDENTITY, 'asset')
        self.error('mobile_rate_limited', self.auth.allow_request, IDENTITY, 'asset')
        for _ in range(360): self.auth.allow_request('device-a', 'read')
        self.error('mobile_rate_limited', self.auth.allow_request, 'device-a', 'read')
        # A different authenticated device retains its independent API budget.
        self.auth.allow_request('device-b', 'read')
        self.clock.advance(60)
        self.auth.allow_request(IDENTITY, 'asset')
        self.auth.allow_request('device-a', 'read')

    def test_larger_read_asset_and_auth_limits_still_have_global_caps(self):
        workloads = [('asset', 240, 5), ('read', 360, 5), ('auth', 600, 4)]
        for kind, per_subject, subjects in workloads:
            for index in range(subjects):
                for _ in range(per_subject):
                    self.auth.allow_request('subject-' + str(index), kind)
            self.error('mobile_rate_limited', self.auth.allow_request, 'fresh-subject', kind)
        self.assertLessEqual(len(self.auth._rates), self.auth.MAX_RATE_BUCKETS)

    def test_pairing_attempts_and_unclaimed_pairings_are_bounded(self):
        for _ in range(8):
            self.error('invalid_mobile_pairing', self.auth.pair, 'bad-token', IDENTITY)
        self.error('mobile_rate_limited', self.auth.pair, 'bad-token', IDENTITY)
        for _ in range(8): self.auth.create_pairing(IDENTITY, 'Phone', [AGENT], [])
        self.error('mobile_pairing_limit', self.auth.create_pairing, IDENTITY, 'Phone', [AGENT], [])
        self.clock.advance(300)
        self.auth.create_pairing(IDENTITY, 'Phone', [AGENT], [])
        self.assertEqual(self.auth.db.execute('SELECT COUNT(*) FROM mobile_pairings').fetchone()[0], 1)

    def test_device_limit_and_audit_bound(self):
        self.auth.MAX_DEVICES = 2
        a = self.pair(); self.pair()
        self.error('mobile_device_limit', self.pair)
        self.auth.revoke(a['device']['device_id'])
        newest = self.pair()
        self.auth.MAX_AUDIT = 3
        self.auth.revoke(newest['device']['device_id'])
        self.assertEqual(len(self.auth.audit_events()), 3)

    def test_atomic_pair_claim_across_store_instances(self):
        invitation = self.auth.create_pairing(IDENTITY, 'Phone', [AGENT], [AGENT])
        other = MobileAuth(self.config, selected_agents=lambda: self.selected, clock=self.clock)
        results = []
        barrier = threading.Barrier(2)
        def claim(store):
            barrier.wait()
            try: results.append(('ok', store.pair(invitation['pairing_token'], IDENTITY)))
            except MobileAuthError as error: results.append(('error', error.code))
        threads = [threading.Thread(target=claim, args=(store,)) for store in (self.auth, other)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(3); self.assertFalse(thread.is_alive())
        other.close()
        self.assertEqual(sorted(value[0] for value in results), ['error', 'ok'])
        self.assertEqual(len(self.auth.list_devices()), 1)

    def test_duration_bounds_and_private_storage_validation(self):
        for option in [{'session_ttl':86401}, {'idle_ttl':3601}, {'pairing_ttl':301}, {'idle_ttl':True}]:
            with self.assertRaises(ValueError): MobileAuth(self.config, **option)
        public = self.root/'public'; public.mkdir(mode=0o755); public.chmod(0o755)
        self.error('mobile_private_directory_required', MobileAuth, {'data_dir':str(public)})
        self.auth.close()
        db = self.root/'mobile-auth.sqlite3'
        original = self.root/'saved.sqlite3'; db.rename(original); db.symlink_to(original)
        try: self.error('mobile_private_database_required', MobileAuth, self.config)
        finally:
            db.unlink(); original.rename(db)
            self.auth = MobileAuth(self.config, selected_agents=lambda: self.selected, clock=self.clock)

    def test_unicode_identity_is_exact_and_never_falls_back_to_another_account(self):
        identity = '使用者@example.invalid'
        invitation = self.auth.create_pairing(identity, '手機', [AGENT], [])
        pairing = self.auth.pair(invitation['pairing_token'], identity)
        self.auth.authenticate(pairing['session_token'], identity)
        self.error('mobile_auth_required', self.auth.authenticate, pairing['session_token'], IDENTITY)


if __name__ == '__main__':
    unittest.main()
