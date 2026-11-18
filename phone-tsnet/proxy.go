package main

import (
	"bytes"
	"context"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"path"
	"strconv"
	"strings"
	"sync"
	"time"

	"tailscale.com/client/tailscale/apitype"
)

const maxBody = 32768
const maxConnections = 16

type whoIsFunc func(context.Context, string) (*apitype.WhoIsResponse, error)

func allowedRequest(r *http.Request) bool {
	p := r.URL.Path
	if r.URL.IsAbs() || r.URL.Host != "" || r.URL.Opaque != "" || r.URL.RawPath != "" || strings.ContainsAny(p, "\\%\x00") || strings.Contains(p, "//") || (path.Clean(p) != p && p != "/mobile/") || len(r.URL.RawQuery) > 4096 || r.Header.Get("Upgrade") != "" {
		return false
	}
	if r.Method == "POST" {
		if r.URL.RawQuery != "" {
			return false
		}
		switch p {
		case "/api/mobile/pair", "/api/mobile/renew", "/api/mobile/remember", "/api/mobile/commands", "/api/mobile/commands/cancel", "/api/mobile/logout":
			return true
		}
		return false
	}
	if r.Method != "GET" {
		return false
	}
	switch p {
	case "/", "/mobile/", "/mobile/pair", "/mobile/agent", "/api/mobile/session", "/api/mobile/health", "/api/mobile/work", "/api/mobile/messages", "/api/mobile/history", "/api/mobile/run", "/api/mobile/commands":
		return true
	}
	// Python's generated static manifest is the authoritative asset allowlist.
	return strings.HasPrefix(p, "/office/") || strings.HasPrefix(p, "/assets/")
}

func stripUntrustedIdentity(h http.Header) {
	for k := range h {
		lower := strings.ToLower(k)
		if strings.HasPrefix(lower, "tailscale-") || strings.HasPrefix(lower, "x-tailscale-") || strings.HasPrefix(lower, "x-forwarded-") || lower == "forwarded" || lower == "x-real-ip" || lower == "authorization" || lower == "proxy-authorization" || lower == "x-office-key" {
			delete(h, k)
		}
	}
}

func denied(w http.ResponseWriter, code int) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(code)
	io.WriteString(w, "{\"error\":\"request_denied\"}\n")
}

func newProxy(c Config, whois whoIsFunc) http.Handler {
	host, _ := originHost(c.ExpectedOrigin)
	target := &url.URL{Scheme: "http", Host: net.JoinHostPort("127.0.0.1", strconv.Itoa(c.BackendPort))}
	allowed := map[string]bool{}
	for _, id := range c.AllowedNodeIDs {
		allowed[id] = true
	}
	transport := &http.Transport{
		Proxy:        nil,
		DialContext:  (&net.Dialer{Timeout: 3 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
		MaxIdleConns: 16, MaxIdleConnsPerHost: 16, MaxConnsPerHost: 16,
		IdleConnTimeout: 30 * time.Second, ResponseHeaderTimeout: 10 * time.Second,
		DisableCompression: true,
	}
	proxy := &httputil.ReverseProxy{
		Rewrite: func(p *httputil.ProxyRequest) {
			p.SetURL(target)
			p.Out.Host = host
			stripUntrustedIdentity(p.Out.Header)
			p.Out.Header.Set("Tailscale-User-Login", c.ExpectedIdentity)
		},
		Transport: transport, ErrorLog: log.New(io.Discard, "", 0),
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) { denied(w, http.StatusBadGateway) },
	}
	// Bound requests independently of connection bounds, including HTTP/2 streams.
	seats := make(chan struct{}, maxConnections)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Host != host && r.Host != host+":443" {
			denied(w, 403)
			return
		}
		if !allowedRequest(r) {
			denied(w, 404)
			return
		}
		select {
		case seats <- struct{}{}:
			defer func() { <-seats }()
		default:
			denied(w, 503)
			return
		}
		// Native identity must come from this embedded node, never a claimed header.
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		identity, err := whois(ctx, r.RemoteAddr)
		cancel()
		if err != nil || identity == nil || identity.Node == nil || identity.UserProfile == nil || len(identity.Node.Tags) != 0 || identity.Node.User != identity.UserProfile.ID || identity.UserProfile.LoginName != c.ExpectedIdentity || !allowed[string(identity.Node.StableID)] {
			denied(w, 403)
			return
		}
		if r.ContentLength > maxBody {
			denied(w, 413)
			return
		}
		body, err := io.ReadAll(io.LimitReader(r.Body, maxBody+1))
		if err != nil || len(body) > maxBody {
			denied(w, 413)
			return
		}
		if r.Method == "GET" && len(body) != 0 {
			denied(w, 400)
			return
		}
		r.Body = io.NopCloser(bytes.NewReader(body))
		r.ContentLength = int64(len(body))
		// The proxy strips identity again after hop-by-hop header processing, so a
		// Connection header cannot remove the verified replacement.
		stripUntrustedIdentity(r.Header)
		proxyCtx, proxyCancel := context.WithTimeout(r.Context(), 20*time.Second)
		defer proxyCancel()
		proxy.ServeHTTP(w, r.WithContext(proxyCtx))
	})
}

// Limit pre-handshake connections, not only requests. Expire stalled TLS clients
// with server read-header deadlines and close connections that exceed capacity.
type boundedListener struct {
	net.Listener
	mu     sync.Mutex
	active map[net.Conn]bool
}

func limitListener(l net.Listener) *boundedListener {
	return &boundedListener{Listener: l, active: make(map[net.Conn]bool)}
}
func (l *boundedListener) Accept() (net.Conn, error) {
	for {
		c, e := l.Listener.Accept()
		if e != nil {
			return nil, e
		}
		l.mu.Lock()
		allowed := len(l.active) < maxConnections
		if allowed {
			l.active[c] = true
		}
		l.mu.Unlock()
		if allowed {
			return c, nil
		}
		c.Close()
	}
}
func (l *boundedListener) connState(c net.Conn, s http.ConnState) {
	if s == http.StateClosed || s == http.StateHijacked {
		l.mu.Lock()
		delete(l.active, c)
		l.mu.Unlock()
	}
}

func validSelf(c Config, stOwn *OwnStatus) error {
	host, _ := originHost(c.ExpectedOrigin)
	if stOwn == nil || stOwn.DNSName != host || stOwn.Identity != c.ExpectedIdentity || stOwn.NodeID == "" {
		return errors.New("node_identity_mismatch")
	}
	return nil
}
