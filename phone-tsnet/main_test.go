package main

import (
	"context"
	"errors"
	"iter"
	"os"
	"testing"

	"tailscale.com/envknob"
	"tailscale.com/feature/buildfeatures"
	"tailscale.com/ipn"
	"tailscale.com/ipn/ipnstate"
	"tailscale.com/tailcfg"
	"tailscale.com/types/key"
)

type memoryStore map[ipn.StateKey][]byte

func (m memoryStore) ReadState(k ipn.StateKey) ([]byte, error) {
	b, ok := m[k]
	if !ok {
		return nil, ipn.ErrStateNotExist
	}
	return b, nil
}
func (m memoryStore) WriteState(k ipn.StateKey, b []byte) error { m[k] = b; return nil }
func (m memoryStore) All() iter.Seq2[ipn.StateKey, []byte] {
	return func(yield func(ipn.StateKey, []byte) bool) {
		for k, v := range m {
			if !yield(k, v) {
				return
			}
		}
	}
}

func TestRejectReusedDaemonPreferences(t *testing.T) {
	for _, value := range []string{`{"RunSSH":true}`, `{"RunWebClient":true}`, `{"AdvertiseRoutes":["0.0.0.0/0"]}`, `{"AdvertiseServices":["svc:old"]}`, `{"ExitNodeID":"nExit"}`, `{"ExitNodeIP":"100.100.1.1"}`, `invalid`} {
		if err := checkStoredPreferences(memoryStore{"profile-one": []byte(value)}); err == nil {
			t.Error("accepted", value)
		}
	}
	if err := checkStoredPreferences(memoryStore{"profile-one": []byte(`{"RunSSH":false,"ControlURL":"https://controlplane.tailscale.com"}`)}); err != nil {
		t.Fatal(err)
	}
}
func TestNoRestoredServeOrFunnel(t *testing.T) {
	m := memoryStore{"_serve/profile": []byte(`{"TCP":{"443":{"HTTPS":true}},"AllowFunnel":{"host:443":true}}`), "identity": []byte("private")}
	s := isolatedStore{m}
	if _, e := s.ReadState("_serve/profile"); !errors.Is(e, ipn.ErrStateNotExist) {
		t.Fatal("restored listener exposed")
	}
	for _, b := range [][]byte{nil, []byte("{}"), []byte("null")} {
		if e := s.WriteState("_serve/profile", b); e != nil {
			t.Fatal(e)
		}
	}
	if e := s.WriteState("_serve/profile", []byte(`{"TCP":{"4318":{}}}`)); e == nil {
		t.Fatal("accepted serve config")
	}
	if got, e := s.ReadState("identity"); e != nil || string(got) != "private" {
		t.Fatal("identity changed")
	}
}
func TestRequiredBuildAndNoUnsafeFallback(t *testing.T) {
	if buildfeatures.HasPeerAPIServer {
		c, p := configFixture(t)
		saveConfig(t, c, p)
		if e := run(context.Background(), c); e == nil || e.Error() != "unsafe_build_missing_peerapi_omission" {
			t.Fatal("unsafe build did not fail before node startup")
		}
		t.Skip("production builds require -tags=ts_omit_peerapiserver")
	}
}
func TestEnvironmentRestrictions(t *testing.T) {
	for _, name := range []string{"TS_AUTHKEY", "TS_AUTH_KEY", "TS_CONTROL_URL", "TS_CLIENT_SECRET", "TSNET_FORCE_LOGIN"} {
		t.Setenv(name, "do-not-use")
	}
	restrictEnvironment()
	for _, name := range []string{"TS_AUTHKEY", "TS_AUTH_KEY", "TS_CONTROL_URL", "TS_CLIENT_SECRET", "TSNET_FORCE_LOGIN"} {
		if os.Getenv(name) != "" {
			t.Error(name)
		}
	}
	if !envknob.NoLogsNoSupport() || os.Getenv("TS_DISABLE_PORTMAPPER") != "true" {
		t.Fatal("missing process protections")
	}
}
func TestStatusPhasesAndStablePhoneID(t *testing.T) {
	for _, tc := range []struct{ backend, want string }{{"NeedsLogin", "login_required"}, {"NeedsMachineAuth", "device_approval_required"}, {"Starting", "starting"}, {"Running", "authenticated"}} {
		st := statusFromNode(&ipnstate.Status{BackendState: tc.backend, AuthURL: "https://evil.invalid"})
		if st.Phase != tc.want || st.AuthURL != "" {
			t.Fatal(st)
		}
	}
	node := &ipnstate.Status{BackendState: "Running", Self: &ipnstate.PeerStatus{ID: "nOffice", UserID: 42, DNSName: "office.test.ts.net."}, User: map[tailcfg.UserID]tailcfg.UserProfile{42: {LoginName: "owner@example.invalid"}}, Peer: map[key.NodePublic]*ipnstate.PeerStatus{key.NewNode().Public(): {ID: "nPhone123", UserID: 42, HostName: "phone", OS: "android", Online: true}, key.NewNode().Public(): {ID: "foreign", UserID: 43}, key.NewNode().Public(): {ID: "shared", UserID: 42, AltSharerUserID: 43}}}
	st := statusFromNode(node)
	if len(st.Peers) != 1 || st.Peers[0].NodeID != "nPhone123" || st.Own.NodeID != "nOffice" || st.Own.DNSName != "office.test.ts.net" {
		t.Fatal(st)
	}
	if e := validSelf(proxyConfig(), st.Own); e != nil {
		t.Fatal(e)
	}
	st.Own.Identity = "other@example.invalid"
	if e := validSelf(proxyConfig(), st.Own); e == nil {
		t.Fatal("accepted wrong node owner")
	}
}
