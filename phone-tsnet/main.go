package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"tailscale.com/envknob"
	"tailscale.com/feature/buildfeatures"
	"tailscale.com/ipn"
	"tailscale.com/ipn/store"
	"tailscale.com/logtail"
	"tailscale.com/tsnet"
)

// tsnet's Up clears old Serve config only after the node reaches Running.
// Hide persisted Serve/Funnel configuration BEFORE startup instead, including
// during the login-only phase. Never permit it to be reintroduced by this node.
type isolatedStore struct{ ipn.StateStore }

// Although tsnet replaces ordinary saved preferences before connecting, reject
// non-isolated state up front rather than silently importing an existing daemon.
func checkStoredPreferences(st store.ExportableStore) error {
	for key, value := range st.All() {
		name := string(key)
		if !strings.HasPrefix(name, "profile-") && !strings.HasPrefix(name, "user-") && key != ipn.LegacyGlobalDaemonStateKey {
			continue
		}
		var prefs ipn.Prefs
		if json.Unmarshal(value, &prefs) != nil {
			return errors.New("unsafe_saved_preferences")
		}
		if prefs.RunSSH || prefs.RunWebClient || len(prefs.AdvertiseRoutes) != 0 || len(prefs.AdvertiseServices) != 0 || prefs.ExitNodeID != "" || prefs.ExitNodeIP.IsValid() {
			return errors.New("unsafe_saved_preferences")
		}
	}
	return nil
}

func (s isolatedStore) ReadState(k ipn.StateKey) ([]byte, error) {
	if strings.HasPrefix(string(k), "_serve/") {
		return nil, ipn.ErrStateNotExist
	}
	return s.StateStore.ReadState(k)
}
func (s isolatedStore) WriteState(k ipn.StateKey, b []byte) error {
	if strings.HasPrefix(string(k), "_serve/") {
		trim := strings.TrimSpace(string(b))
		if trim == "" || trim == "{}" || trim == "null" {
			return nil
		}
		return errors.New("serve_configuration_disabled")
	}
	return s.StateStore.WriteState(k, b)
}

func restrictEnvironment() {
	for _, entry := range os.Environ() {
		name, _, _ := strings.Cut(entry, "=")
		if strings.HasPrefix(name, "TS_") || strings.HasPrefix(name, "TSNET_") {
			envknob.Setenv(name, "")
			os.Unsetenv(name)
		}
	}
	// These affect only this helper process, not the macOS VPN or its routes.
	envknob.Setenv("TS_DISABLE_PORTMAPPER", "true")
	envknob.SetNoLogsNoSupport()
	logtail.Disable()
	log.SetOutput(io.Discard)
}

func run(ctx context.Context, c Config) error {
	if buildfeatures.HasPeerAPIServer {
		return errors.New("unsafe_build_missing_peerapi_omission")
	}
	restrictEnvironment()
	statePath := filepath.Join(c.StateDir, "tailscaled.state")
	if _, err := os.Lstat(statePath); err == nil {
		if err := privatePath(statePath, false); err != nil {
			return err
		}
	} else if !os.IsNotExist(err) {
		return errors.New("state_unavailable")
	}
	discard := func(string, ...any) {}
	st, err := store.NewFileStore(discard, statePath)
	if err != nil {
		return errors.New("state_unavailable")
	}
	exportable, ok := st.(store.ExportableStore)
	if !ok {
		return errors.New("state_preflight_unavailable")
	}
	if err := checkStoredPreferences(exportable); err != nil {
		return err
	}
	srv := &tsnet.Server{Dir: c.StateDir, Store: isolatedStore{st}, Hostname: c.Hostname,
		ControlURL: "https://controlplane.tailscale.com", RunWebClient: false, Logf: discard, UserLogf: discard}
	if err := writeStatus(c.StatusPath, Status{Phase: "starting"}); err != nil {
		return err
	}
	if err := srv.Start(); err != nil {
		return errors.New("node_start_failed")
	}
	defer srv.Close()
	lc, err := srv.LocalClient()
	if err != nil {
		return errors.New("local_client_failed")
	}
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	var server *http.Server
	var serving bool
	serveErr := make(chan error, 1)
	defer func() {
		if server != nil {
			closeCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			server.Shutdown(closeCtx)
		}
	}()
	for {
		queryCtx, cancel := context.WithTimeout(ctx, 3*time.Second)
		node, err := lc.Status(queryCtx)
		cancel()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return errors.New("node_status_unavailable")
		}
		if node.TUN || node.ExitNodeStatus != nil {
			return errors.New("unexpected_network_mode")
		}
		snapshot := statusFromNode(node)
		if c.Mode == "serve" && snapshot.Phase == "authenticated" {
			if !serving {
				node, err = waitForStartupIdentity(ctx, c, node, lc.Status,
					func(status Status) error { return writeStatus(c.StatusPath, status) })
				if err != nil {
					if ctx.Err() != nil {
						return nil
					}
					return err
				}
				snapshot = statusFromNode(node)
			}
			if err := validSelf(c, snapshot.Own); err != nil {
				return err
			}
			if !serving {
				if node.Self == nil || (node.Self.Tags != nil && node.Self.Tags.Len() != 0) {
					return errors.New("tagged_node_not_supported")
				}
				if node.CurrentTailnet == nil || !node.CurrentTailnet.MagicDNSEnabled {
					return errors.New("magic_dns_required")
				}
				host, _ := originHost(c.ExpectedOrigin)
				certReady := false
				for _, name := range node.CertDomains {
					if name == host {
						certReady = true
					}
				}
				if !certReady {
					return errors.New("https_certificate_required")
				}
				upCtx, upCancel := context.WithTimeout(ctx, 10*time.Second)
				_, upErr := srv.Up(upCtx)
				upCancel()
				if upErr != nil {
					return errors.New("node_not_ready")
				}
				preparing := snapshot
				preparing.Code = "certificate_preparing"
				listener, err := listenWithPreparedCertificate(ctx, host, lc.CertPair,
					func() error { return writeStatus(c.StatusPath, preparing) },
					func() (net.Listener, error) { return srv.ListenTLS("tcp", ":443") })
				if err != nil {
					if ctx.Err() != nil {
						return nil
					}
					return err
				}
				bounded := limitListener(listener)
				server = &http.Server{Handler: newProxy(c, lc.WhoIs), ConnState: bounded.connState, ReadHeaderTimeout: 5 * time.Second,
					ReadTimeout: 20 * time.Second, WriteTimeout: 25 * time.Second, IdleTimeout: 30 * time.Second,
					MaxHeaderBytes: 16384, ErrorLog: log.New(io.Discard, "", 0)}
				go func() { serveErr <- server.Serve(bounded) }()
				serving = true
			}
			snapshot.Phase = "ready"
		} else if serving {
			return errors.New("node_disconnected")
		}
		if err := writeStatus(c.StatusPath, snapshot); err != nil {
			return err
		}
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
		case <-serveErr:
			return errors.New("private_listener_stopped")
		}
	}
}

func main() {
	configPath := flag.String("config", "", "Path to a private 0600 JSON configuration")
	flag.Parse()
	if *configPath == "" || flag.NArg() != 0 {
		fmt.Fprintln(os.Stderr, "configuration_required")
		os.Exit(2)
	}
	c, err := loadConfig(*configPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "invalid_private_configuration")
		os.Exit(2)
	}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	if err := run(ctx, c); err != nil {
		writeStatus(c.StatusPath, Status{Phase: "error", Code: err.Error()})
		fmt.Fprintln(os.Stderr, "private_frontend_failed")
		os.Exit(1)
	}
	writeStatus(c.StatusPath, Status{Phase: "stopped"})
}
