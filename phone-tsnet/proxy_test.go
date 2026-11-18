package main

import (
	"context"
	"crypto/tls"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"testing"

	"tailscale.com/client/tailscale/apitype"
	"tailscale.com/tailcfg"
)

func proxyConfig() Config {
	return Config{ExpectedOrigin: "https://office.test.ts.net", ExpectedIdentity: "owner@example.invalid", AllowedNodeIDs: []string{"nPhone123"}}
}
func verifiedIdentity() *apitype.WhoIsResponse {
	return &apitype.WhoIsResponse{Node: &tailcfg.Node{StableID: "nPhone123", User: 42}, UserProfile: &tailcfg.UserProfile{ID: 42, LoginName: "owner@example.invalid"}}
}
func request(method, path string) *http.Request {
	r := httptest.NewRequest(method, path, nil)
	r.Host = "office.test.ts.net"
	r.RemoteAddr = "100.100.100.100:4567"
	return r
}

func TestRejectUnauthorizedIdentity(t *testing.T) {
	cases := []struct {
		name string
		edit func(*apitype.WhoIsResponse)
	}{
		{"nil_node", func(i *apitype.WhoIsResponse) { i.Node = nil }},
		{"nil_profile", func(i *apitype.WhoIsResponse) { i.UserProfile = nil }},
		{"tagged", func(i *apitype.WhoIsResponse) { i.Node.Tags = []string{"tag:server"} }},
		{"wrong_login", func(i *apitype.WhoIsResponse) { i.UserProfile.LoginName = "other@example.invalid" }},
		{"wrong_node_same_user", func(i *apitype.WhoIsResponse) { i.Node.StableID = "nOtherPhone" }},
		{"shared_user", func(i *apitype.WhoIsResponse) { i.Node.User = 43 }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			id := verifiedIdentity()
			tc.edit(id)
			h := newProxy(proxyConfig(), func(context.Context, string) (*apitype.WhoIsResponse, error) { return id, nil })
			w := httptest.NewRecorder()
			h.ServeHTTP(w, request("GET", "/api/mobile/work"))
			if w.Code != 403 {
				t.Fatal(w.Code)
			}
		})
	}
	for _, nilResult := range []bool{false, true} {
		h := newProxy(proxyConfig(), func(context.Context, string) (*apitype.WhoIsResponse, error) {
			if nilResult {
				return nil, nil
			}
			return nil, errors.New("secret-error")
		})
		w := httptest.NewRecorder()
		h.ServeHTTP(w, request("GET", "/"))
		if w.Code != 403 || strings.Contains(w.Body.String(), "secret") {
			t.Fatal(w)
		}
	}
}

func TestRejectRoutesBeforeIdentity(t *testing.T) {
	cases := []struct{ method, path string }{
		{"GET", "/api/admin"}, {"GET", "/api/status"}, {"GET", "/localapi/v0/status"}, {"GET", "/debug/pprof/"},
		{"GET", "/api/invitations"}, {"GET", "/api/mobile/../admin"}, {"GET", "//api/mobile/work"},
		{"GET", "/office/%2e%2e/private.json"}, {"GET", "/office/%2fapi"}, {"GET", "/office/a%5cb"},
		{"GET", "http://127.0.0.1:4318/api/status"}, {"GET", "https://office.test.ts.net/"},
		{"CONNECT", "office.test.ts.net:443"}, {"PUT", "/api/mobile/commands"}, {"POST", "/office/mobile.html"},
		{"POST", "/api/mobile/commands?x=1"}, {"GET", "/api/mobile/unknown"},
		{"GET", "/api/mobile/renew"}, {"GET", "/api/mobile/remember"},
		{"PUT", "/api/mobile/renew"}, {"DELETE", "/api/mobile/remember"},
		{"POST", "/api/mobile/renew?x=1"}, {"POST", "/api/mobile/remember?x=1"},
		{"POST", "/api/mobile/renew/"}, {"POST", "/api/mobile/remember/extra"},
		{"POST", "/api/mobile/renew-all"}, {"POST", "/api/mobile/remembered"},
		{"POST", "/api/mobile/%72enew"}, {"POST", "/api/mobile/%72emember"},
	}
	for _, tc := range cases {
		t.Run(tc.method+tc.path, func(t *testing.T) {
			called := false
			h := newProxy(proxyConfig(), func(context.Context, string) (*apitype.WhoIsResponse, error) {
				called = true
				return verifiedIdentity(), nil
			})
			w := httptest.NewRecorder()
			h.ServeHTTP(w, request(tc.method, tc.path))
			if w.Code != 404 || called {
				t.Fatalf("status=%d whois=%v", w.Code, called)
			}
		})
	}
	for _, host := range []string{"localhost:4318", "127.0.0.1:4319", "office.test.ts.net.attacker.invalid", "office.test.ts.net:4318", "OFFICE.test.ts.net"} {
		r := request("GET", "/")
		r.Host = host
		h := newProxy(proxyConfig(), func(context.Context, string) (*apitype.WhoIsResponse, error) {
			t.Fatal("unexpected whois")
			return nil, nil
		})
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if w.Code != 403 {
			t.Fatal(host, w.Code)
		}
	}
}

func TestProxyFixedTargetAndVerifiedHeaders(t *testing.T) {
	c := proxyConfig()
	hits := 0
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits++
		if r.Host != "office.test.ts.net" {
			t.Error("wrong host", r.Host)
		}
		if r.Header.Get("Tailscale-User-Login") != c.ExpectedIdentity {
			t.Error("wrong identity")
		}
		for k := range r.Header {
			lower := strings.ToLower(k)
			if k != "Tailscale-User-Login" && (strings.HasPrefix(lower, "tailscale-") || strings.HasPrefix(lower, "x-tailscale-") || strings.HasPrefix(lower, "x-forwarded-") || lower == "forwarded" || lower == "authorization" || lower == "x-office-key") {
				t.Error("untrusted header", k)
			}
		}
		if r.Header.Get("Origin") != c.ExpectedOrigin || r.Header.Get("X-CSRF-Token") != "phone-csrf" || r.Header.Get("Cookie") != "phone=test" {
			t.Error("phone credentials missing")
		}
		io.WriteString(w, "ok")
	}))
	defer backend.Close()
	u, _ := url.Parse(backend.URL)
	_, p, _ := net.SplitHostPort(u.Host)
	c.BackendPort, _ = strconv.Atoi(p)
	t.Setenv("HTTP_PROXY", "http://127.0.0.1:1")
	t.Setenv("HTTPS_PROXY", "http://127.0.0.1:1")
	h := newProxy(c, func(ctx context.Context, remote string) (*apitype.WhoIsResponse, error) {
		if remote != "100.100.100.100:4567" {
			t.Error("forged remote", remote)
		}
		if _, ok := ctx.Deadline(); !ok {
			t.Error("missing whois timeout")
		}
		return verifiedIdentity(), nil
	})
	r := request("POST", "/api/mobile/commands")
	for _, key := range []string{"Tailscale-User-Login", "Tailscale-User-Name", "Tailscale-Foo", "X-Tailscale-User", "Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Authorization", "X-Office-Key"} {
		r.Header.Set(key, "attacker")
	}
	r.Header.Set("Connection", "Tailscale-User-Login")
	r.Header.Set("Origin", c.ExpectedOrigin)
	r.Header.Set("Cookie", "phone=test")
	r.Header.Set("X-CSRF-Token", "phone-csrf")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != 200 || hits != 1 || w.Body.String() != "ok" {
		t.Fatal(w.Code, hits, w.Body.String())
	}
}

func TestRenewalRoutesRemainIdentityBound(t *testing.T) {
	for _, path := range []string{"/api/mobile/renew", "/api/mobile/remember"} {
		t.Run(path, func(t *testing.T) {
			if !allowedRequest(request("POST", path)) {
				t.Fatal("exact renewal route denied")
			}
			for _, change := range []string{"other-phone", "other-account", "tagged"} {
				id := verifiedIdentity()
				switch change {
				case "other-phone":
					id.Node.StableID = "nUnapprovedPhone"
				case "other-account":
					id.UserProfile.LoginName = "other@example.invalid"
				case "tagged":
					id.Node.Tags = []string{"tag:server"}
				}
				h := newProxy(proxyConfig(), func(context.Context, string) (*apitype.WhoIsResponse, error) { return id, nil })
				w := httptest.NewRecorder()
				h.ServeHTTP(w, request("POST", path))
				if w.Code != http.StatusForbidden {
					t.Fatalf("%s: status=%d", change, w.Code)
				}
			}
		})
	}
}

func TestRenewalRoutesReachFixedBackendWithCookies(t *testing.T) {
	c := proxyConfig()
	hits := make(map[string]int)
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits[r.URL.Path]++
		if r.Method != "POST" || r.URL.RawQuery != "" || r.Host != "office.test.ts.net" {
			t.Error("request changed", r.Method, r.URL.Path, r.Host)
		}
		if r.Header.Get("Tailscale-User-Login") != c.ExpectedIdentity || r.Header.Get("X-Office-Key") != "" {
			t.Error("unverified identity reached backend")
		}
		if r.Header.Get("Origin") != c.ExpectedOrigin || r.Header.Get("X-Mobile-CSRF") != "fixture-csrf" ||
			r.Header.Get("Cookie") != "__Host-OfficePhone=fixture-session; __Host-OfficePhoneRenew=fixture-renewal" {
			t.Error("phone credentials not forwarded")
		}
		body, _ := io.ReadAll(r.Body)
		if string(body) != "{}" {
			t.Error("request body changed")
		}
		w.Header().Add("Set-Cookie", "__Host-OfficePhone=next-session; Path=/; Secure; HttpOnly; SameSite=Strict")
		w.Header().Add("Set-Cookie", "__Host-OfficePhoneRenew=next-renewal; Path=/; Secure; HttpOnly; SameSite=Strict")
		io.WriteString(w, "renewed")
	}))
	defer backend.Close()
	u, _ := url.Parse(backend.URL)
	_, port, _ := net.SplitHostPort(u.Host)
	c.BackendPort, _ = strconv.Atoi(port)
	h := newProxy(c, func(context.Context, string) (*apitype.WhoIsResponse, error) { return verifiedIdentity(), nil })
	for _, path := range []string{"/api/mobile/renew", "/api/mobile/remember"} {
		r := request("POST", path)
		r.Body = io.NopCloser(strings.NewReader("{}"))
		r.ContentLength = 2
		r.Header.Set("Origin", c.ExpectedOrigin)
		r.Header.Set("Cookie", "__Host-OfficePhone=fixture-session; __Host-OfficePhoneRenew=fixture-renewal")
		r.Header.Set("X-Mobile-CSRF", "fixture-csrf")
		r.Header.Set("Tailscale-User-Login", "forged-identity")
		r.Header.Set("X-Office-Key", "forged-owner-key")
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if w.Code != http.StatusOK || hits[path] != 1 || w.Body.String() != "renewed" || len(w.Header().Values("Set-Cookie")) != 2 {
			t.Fatalf("%s status=%d hits=%d cookies=%d", path, w.Code, hits[path], len(w.Header().Values("Set-Cookie")))
		}
	}
}

func TestBodyAndUpgradeLimits(t *testing.T) {
	h := newProxy(proxyConfig(), func(context.Context, string) (*apitype.WhoIsResponse, error) { return verifiedIdentity(), nil })
	for _, chunked := range []bool{false, true} {
		r := request("POST", "/api/mobile/commands")
		r.Body = io.NopCloser(strings.NewReader(strings.Repeat("x", maxBody+1)))
		r.ContentLength = maxBody + 1
		if chunked {
			r.ContentLength = -1
		}
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if w.Code != 413 {
			t.Fatal(w.Code)
		}
	}
	r := request("GET", "/")
	r.Body = io.NopCloser(strings.NewReader("x"))
	r.ContentLength = 1
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != 400 {
		t.Fatal(w.Code)
	}
	r = request("GET", "/")
	r.Header.Set("Upgrade", "websocket")
	w = httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != 404 {
		t.Fatal(w.Code)
	}
}

func TestStaticAndMobileRouteScope(t *testing.T) {
	for _, p := range []string{"/", "/mobile/", "/mobile/pair", "/mobile/agent", "/api/mobile/session", "/api/mobile/health", "/api/mobile/work", "/api/mobile/messages", "/api/mobile/history", "/api/mobile/run", "/api/mobile/commands", "/office/mobile.js", "/assets/vendor/three.js"} {
		if !allowedRequest(request("GET", p)) {
			t.Error(p)
		}
	}
}

type queuedListener struct{ conns []net.Conn }

func (l *queuedListener) Accept() (net.Conn, error) {
	if len(l.conns) == 0 {
		return nil, net.ErrClosed
	}
	c := l.conns[0]
	l.conns = l.conns[1:]
	return c, nil
}
func (l *queuedListener) Close() error   { return nil }
func (l *queuedListener) Addr() net.Addr { return &net.TCPAddr{} }

func TestListenerCapsAndPreservesTLSConnection(t *testing.T) {
	q := &queuedListener{}
	for i := 0; i < maxConnections+1; i++ {
		a, b := net.Pipe()
		defer a.Close()
		defer b.Close()
		q.conns = append(q.conns, tls.Server(a, &tls.Config{}))
	}
	first := q.conns[0]
	limited := limitListener(q)
	for i := 0; i < maxConnections; i++ {
		c, e := limited.Accept()
		if e != nil {
			t.Fatal(e)
		}
		if _, ok := c.(*tls.Conn); !ok {
			t.Fatal("TLS connection hidden from http.Server")
		}
		if i == 0 && c != first {
			t.Fatal("original connection replaced")
		}
	}
	if _, e := limited.Accept(); !errors.Is(e, net.ErrClosed) {
		t.Fatal("excess connection accepted")
	}
	if len(limited.active) != maxConnections {
		t.Fatal("incorrect limit")
	}
	limited.connState(first, http.StateClosed)
	if len(limited.active) != maxConnections-1 {
		t.Fatal("closed slot not released")
	}
	a, b := net.Pipe()
	defer a.Close()
	defer b.Close()
	q.conns = append(q.conns, tls.Server(a, &tls.Config{}))
	c, e := limited.Accept()
	if e != nil {
		t.Fatal("slot not reused", e)
	}
	limited.connState(c, http.StateHijacked)
}
