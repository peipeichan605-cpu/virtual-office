"""Remembered phone grant tests; all credentials and clocks are isolated fixtures."""
import concurrent.futures
from http.cookies import SimpleCookie
import json
from pathlib import Path
import tempfile
import unittest

from mobile_auth import MobileAuth, MobileAuthError
from mobile_gateway import COOKIE, RENEW_COOKIE
from test_mobile_auth import AGENT, OTHER, IDENTITY, Clock
import test_mobile_gateway as gateway_fixture
from test_mobile_service import IDENTITY as HTTP_IDENTITY, A, B


class RememberedAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.clock=Clock()
        self.selected={AGENT:'member-a',OTHER:'member-b'}
        self.config={'data_dir':str(self.root)}
        self.auth=MobileAuth(self.config,selected_agents=lambda:self.selected,clock=self.clock)
    def tearDown(self):
        self.auth.close();self.temp.cleanup()
    def pair(self, remember=True):
        invitation=self.auth.create_pairing(IDENTITY,'Fixture phone',[AGENT],[AGENT])
        return self.auth.pair(invitation['pairing_token'],IDENTITY,remember_device=remember)
    def deny(self,fn,*args,**kwargs):
        with self.assertRaises(MobileAuthError) as error:fn(*args,**kwargs)
        self.assertEqual(error.exception.status,401)
    def test_opt_in_is_explicit_boolean_and_legacy_expiry_does_not_extend(self):
        paired=self.pair(False);before=paired['device'];self.assertFalse(before['remembered']);self.assertNotIn('renewal_token',paired)
        self.clock.advance(3601);self.deny(self.auth.authenticate,paired['session_token'],IDENTITY)
        self.assertEqual(self.auth.list_devices()[0]['state'],'expired')
        self.assertEqual(self.auth.list_devices()[0]['expires_at'],before['expires_at'])
        invite=self.auth.create_pairing(IDENTITY,'Bad choice',[AGENT],[])
        for choice in (1,'true',None):
            with self.assertRaises(MobileAuthError):self.auth.pair(invite['pairing_token'],IDENTITY,remember_device=choice)
    def test_remembered_grant_renews_after_idle_without_new_device_or_scopes(self):
        old=self.pair();deadline=old['device']['renewal_expires_at'];self.clock.advance(3601)
        self.assertEqual(self.auth.list_devices()[0]['state'],'renewal_required')
        self.deny(self.auth.authenticate,old['session_token'],IDENTITY)
        new=self.auth.renew(old['renewal_token'],IDENTITY)
        self.assertEqual(new['device']['device_id'],old['device']['device_id']);self.assertEqual(new['device']['view_agents'],[AGENT])
        self.assertEqual(new['device']['renewal_expires_at'],deadline);self.assertNotEqual(new['session_token'],old['session_token'])
        self.assertEqual(new['device']['revision'],2)
        self.auth.authenticate(new['session_token'],IDENTITY,csrf=new['csrf_token'],write=True)
        self.deny(self.auth.authenticate,old['session_token'],IDENTITY)
    def test_renewal_secret_is_not_an_access_token_and_secrets_not_persisted(self):
        paired=self.pair();self.deny(self.auth.authenticate,paired['renewal_token'],IDENTITY)
        public=json.dumps([paired['device'],self.auth.list_devices(),self.auth.audit_events()]);raw=(self.root/'mobile-auth.sqlite3').read_bytes()
        for secret in (paired['renewal_token'],paired['session_token'],paired['csrf_token']):
            self.assertNotIn(secret,public);self.assertNotIn(secret.encode(),raw)
    def test_exact_identity_revoke_and_old_grant_cannot_renew(self):
        paired=self.pair();self.deny(self.auth.renew,paired['renewal_token'],IDENTITY.upper())
        self.auth.revoke(paired['device']['device_id'])
        self.deny(self.auth.renew,paired['renewal_token'],IDENTITY)
        self.deny(self.auth.renewal_device,paired['renewal_token'],IDENTITY)
        self.deny(self.auth.authenticate,paired['session_token'],IDENTITY)
    def test_seven_days_unused_and_thirty_day_absolute_limits(self):
        paired=self.pair();self.clock.advance(7*86400)
        self.deny(self.auth.renew,paired['renewal_token'],IDENTITY)
        paired=self.pair();deadline=paired['device']['renewal_expires_at']
        for _ in range(4):
            self.clock.advance(6*86400);self.auth.renew(paired['renewal_token'],IDENTITY)
        self.clock.now=deadline-1;last=self.auth.renew(paired['renewal_token'],IDENTITY)
        self.assertEqual(last['device']['expires_at'],deadline)
        self.clock.advance(1);self.deny(self.auth.renew,paired['renewal_token'],IDENTITY)
        self.deny(self.auth.authenticate,last['session_token'],IDENTITY)
    def test_authenticated_activity_extends_idle_not_absolute_grant(self):
        paired=self.pair();deadline=paired['device']['renewal_expires_at'];oldidle=paired['device']['renewal_idle_expires_at']
        self.clock.advance(300);self.auth.authenticate(paired['session_token'],IDENTITY)
        device=self.auth.list_devices()[0]
        self.assertEqual(device['renewal_expires_at'],deadline);self.assertEqual(device['renewal_idle_expires_at'],oldidle+300)
    def test_concurrent_renewals_return_one_current_access_without_secret_storage(self):
        paired=self.pair();self.clock.advance(3601)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.auth.renew(paired['renewal_token'],IDENTITY),range(4)))
        self.assertEqual(len({x['session_token'] for x in results}),1)
        self.assertEqual(len({x['csrf_token'] for x in results}),1)
        self.assertEqual({x['device']['revision'] for x in results},{2})
        self.auth.authenticate(results[0]['session_token'],IDENTITY)
        self.assertEqual(sum(e['event']=='device_session_renewed' for e in self.auth.audit_events()),1)
    def test_scope_changes_and_membership_removal_stay_after_renewal(self):
        paired=self.pair();self.clock.advance(3601)
        self.auth.update_scopes(paired['device']['device_id'],[AGENT,OTHER],[OTHER])
        self.auth.invalidate_agent(OTHER)
        new=self.auth.renew(paired['renewal_token'],IDENTITY)
        self.assertEqual(new['device']['view_agents'],[AGENT]);self.assertEqual(new['device']['send_agents'],[])
    def test_old_queued_command_and_principal_fenced_after_rotation(self):
        paired=self.pair();principal=self.auth.authenticate(paired['session_token'],IDENTITY,csrf=paired['csrf_token'],write=True)
        origin=self.auth.command_origin(principal,AGENT);self.clock.advance(3601)
        self.auth.renew(paired['renewal_token'],IDENTITY)
        self.assertFalse(self.auth.validate_command_origin(origin,AGENT));self.deny(self.auth.refresh,principal)
    def test_explicit_remember_requires_live_csrf_and_disable_preserves_access(self):
        legacy=self.pair(False)
        with self.assertRaises(MobileAuthError):self.auth.remember(legacy['session_token'],IDENTITY,'bad',True)
        remembered=self.auth.remember(legacy['session_token'],IDENTITY,legacy['csrf_token'],True)
        self.assertEqual(remembered['device']['device_id'],legacy['device']['device_id'])
        self.assertTrue(remembered['device']['remembered']);self.deny(self.auth.authenticate,legacy['session_token'],IDENTITY)
        disabled=self.auth.remember(remembered['session_token'],IDENTITY,remembered['csrf_token'],False)
        self.assertFalse(disabled['device']['remembered']);self.assertEqual(disabled['session_token'],remembered['session_token'])
        self.auth.authenticate(disabled['session_token'],IDENTITY);self.deny(self.auth.renew,remembered['renewal_token'],IDENTITY)
    def test_reopen_retains_legacy_and_remembered_grant_without_new_deadline(self):
        legacy=self.pair(False);remembered=self.pair();self.auth.close()
        self.auth=MobileAuth(self.config,selected_agents=lambda:self.selected,clock=self.clock)
        rows={r['device_id']:r for r in self.auth.list_devices()}
        self.assertEqual(rows[legacy['device']['device_id']],legacy['device'])
        self.assertEqual(rows[remembered['device']['device_id']],remembered['device'])
        self.clock.advance(3601);self.auth.renew(remembered['renewal_token'],IDENTITY)
    def test_device_count_includes_idle_renewable_grants(self):
        self.auth.MAX_DEVICES=1;paired=self.pair();self.clock.advance(3601)
        with self.assertRaises(MobileAuthError) as error:self.pair()
        self.assertEqual(error.exception.code,'mobile_device_limit')


class RememberedGatewayTests(unittest.TestCase):
    setUp=gateway_fixture.MobileGatewayTests.setUp
    tearDown=gateway_fixture.MobileGatewayTests.tearDown
    request=gateway_fixture.MobileGatewayTests.request
    json=gateway_fixture.MobileGatewayTests.json
    pair=gateway_fixture.MobileGatewayTests.pair
    command=gateway_fixture.MobileGatewayTests.command
    def remember_pair(self):
        # Collect real multiple Set-Cookie headers using the shared fixture's
        # first-class header list added for this test family.
        invitation=self.service.owner_create_pairing({'identity':HTTP_IDENTITY,'label':'Remember fixture','view_agents':[A],'send_agents':[A]})
        status,headers,body=self.json('/api/mobile/pair',method='POST',data={'token':invitation['pairing_token'],'remember_device':True})
        self.assertEqual(status,200,body);self.accept_cookies(headers);self.csrf=body['csrf_token'];return body,headers
    def accept_cookies(self,headers):
        jar=SimpleCookie();jar.load(self.cookie or '')
        for raw in headers.get('Set-Cookie-All',[]):
            fresh=SimpleCookie();fresh.load(raw)
            for name,item in fresh.items():
                if item['max-age']=='0':jar.pop(name,None)
                else:jar[name]=item.value
        self.cookie='; '.join(name+'='+item.value for name,item in jar.items())
    def test_remember_cookie_is_secure_separate_and_no_secret_in_body(self):
        body,headers=self.remember_pair();cookies=headers['Set-Cookie-All'];self.assertEqual(len(cookies),2)
        self.assertTrue(any(c.startswith(RENEW_COOKIE+'=') for c in cookies))
        for cookie in cookies:
            for flag in ('Path=/','Secure','HttpOnly','SameSite=Strict','Max-Age='):self.assertIn(flag,cookie)
        self.assertNotIn('renewal_token',body);self.assertNotIn('session_token',body)
    def test_expired_session_recovers_only_by_exact_same_origin_renewal(self):
        body,_=self.remember_pair();self.service.auth.db.execute('UPDATE mobile_devices SET last_seen=last_seen-3601 WHERE id=?',(body['device']['device_id'],))
        status,_,error=self.json('/api/mobile/session');self.assertEqual(status,401);self.assertEqual(error['error'],'mobile_session_renewal_required')
        for headers,data in [({},{}),({'X-Mobile-Renew':'1'},{'unexpected':True}),({'X-Mobile-Renew':'1','Origin':'https://evil.example'},{}),({'X-Mobile-Renew':'1','Sec-Fetch-Site':'cross-site'},{})]:
            status,_,_=self.json('/api/mobile/renew',method='POST',headers=headers,data=data);self.assertIn(status,(400,403))
        status,headers,new=self.json('/api/mobile/renew',method='POST',headers={'X-Mobile-Renew':'1'},data={})
        self.assertEqual(status,200,new);self.accept_cookies(headers)
        self.assertEqual(new['device']['device_id'],body['device']['device_id']);self.assertEqual(self.json('/api/mobile/session')[0],200)
    def test_remembered_logout_revokes_even_with_expired_access(self):
        body,_=self.remember_pair();self.service.auth.db.execute('UPDATE mobile_devices SET last_seen=last_seen-3601 WHERE id=?',(body['device']['device_id'],))
        oldcookie=self.cookie
        status,headers,result=self.json('/api/mobile/logout',method='POST',headers={'X-Mobile-Renew':'1','X-Mobile-CSRF':None},data={})
        self.assertEqual(status,200,result);self.assertEqual(len(headers['Set-Cookie-All']),2)
        self.assertTrue(all('Max-Age=0' in c for c in headers['Set-Cookie-All']))
        self.cookie=oldcookie
        self.assertEqual(self.json('/api/mobile/renew',method='POST',headers={'X-Mobile-Renew':'1'},data={})[0],401)
    def test_existing_live_phone_can_explicitly_enable_then_disable_remember(self):
        oldid=self.paired['device']['device_id']
        status,_,_=self.json('/api/mobile/remember',method='POST',headers={'X-Mobile-CSRF':None},data={'remember_device':True});self.assertEqual(status,403)
        status,headers,body=self.json('/api/mobile/remember',method='POST',data={'remember_device':True});self.assertEqual(status,200,body)
        self.assertEqual(body['device']['device_id'],oldid);self.accept_cookies(headers);self.csrf=body['csrf_token']
        status,headers,body=self.json('/api/mobile/remember',method='POST',data={'remember_device':False});self.assertEqual(status,200,body)
        self.assertFalse(body['device']['remembered']);self.accept_cookies(headers);self.assertNotIn(RENEW_COOKIE+'=',self.cookie)
        self.assertEqual(self.json('/api/mobile/session')[0],200)

if __name__=='__main__':unittest.main()
