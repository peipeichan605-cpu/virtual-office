package main

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
	"math/big"
	"net"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// Test-only short-lived keys/certificates are generated in memory. No real node,
// authentication, certificate service or TLS listener is used by these tests.
func certificateFixture(t *testing.T, host string, before, after time.Time) ([]byte, []byte) {
	t.Helper()
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	cert := &x509.Certificate{SerialNumber: big.NewInt(1), DNSNames: []string{host}, NotBefore: before, NotAfter: after,
		KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}}
	der, err := x509.CreateCertificate(rand.Reader, cert, cert, public, private)
	if err != nil {
		t.Fatal(err)
	}
	key, err := x509.MarshalPKCS8PrivateKey(private)
	if err != nil {
		t.Fatal(err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: key})
}

func TestCertificateValidation(t *testing.T) {
	now := time.Now()
	validCert, validKey := certificateFixture(t, "office.test.ts.net", now.Add(-time.Hour), now.Add(time.Hour))
	_, otherKey := certificateFixture(t, "office.test.ts.net", now.Add(-time.Hour), now.Add(time.Hour))
	expiredCert, expiredKey := certificateFixture(t, "office.test.ts.net", now.Add(-2*time.Hour), now.Add(-time.Hour))
	futureCert, futureKey := certificateFixture(t, "office.test.ts.net", now.Add(time.Hour), now.Add(2*time.Hour))
	for _, tc := range []struct {
		name, host string
		cert, key  []byte
		valid      bool
	}{
		{"valid", "office.test.ts.net", validCert, validKey, true},
		{"wrong_host", "other.test.ts.net", validCert, validKey, false},
		{"mismatched_key", "office.test.ts.net", validCert, otherKey, false},
		{"expired", "office.test.ts.net", expiredCert, expiredKey, false},
		{"not_yet_valid", "office.test.ts.net", futureCert, futureKey, false},
		{"malformed", "office.test.ts.net", []byte("invalid"), validKey, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			err := validateCertificate(tc.host, tc.cert, tc.key, now)
			if (err == nil) != tc.valid {
				t.Fatalf("valid=%v error=%v", tc.valid, err)
			}
			if err != nil && err.Error() != "certificate_invalid" {
				t.Fatal("unexpected error details")
			}
		})
	}
}

func TestCertificatePreparationPrecedesListeningAndKeepsPrivateHeartbeat(t *testing.T) {
	c, _ := configFixture(t)
	cert, key := certificateFixture(t, "office.test.ts.net", time.Now().Add(-time.Hour), time.Now().Add(time.Hour))
	release := make(chan struct{})
	heartbeats := make(chan struct{}, 4)
	var listens atomic.Int32
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() {
		_, err := listenWithPreparedCertificate(ctx, "office.test.ts.net", func(callCtx context.Context, host string) ([]byte, []byte, error) {
			if host != "office.test.ts.net" {
				return nil, nil, errors.New("wrong_host")
			}
			if deadline, ok := callCtx.Deadline(); !ok || time.Until(deadline) > certificateTimeout {
				return nil, nil, errors.New("missing_bound")
			}
			select {
			case <-release:
				return append([]byte(nil), cert...), append([]byte(nil), key...), nil
			case <-callCtx.Done():
				return nil, nil, callCtx.Err()
			}
		}, func() error {
			err := writeStatus(c.StatusPath, Status{Phase: "authenticated", Code: "certificate_preparing"})
			heartbeats <- struct{}{}
			return err
		}, func() (net.Listener, error) { listens.Add(1); return &queuedListener{}, nil })
		done <- err
	}()
	for i := 0; i < 2; i++ {
		select {
		case <-heartbeats:
		case <-ctx.Done():
			t.Fatal("heartbeat missing")
		}
	}
	if listens.Load() != 0 {
		t.Fatal("listener opened before certificate was available")
	}
	b, err := os.ReadFile(c.StatusPath)
	if err != nil {
		t.Fatal(err)
	}
	var status Status
	if json.Unmarshal(b, &status) != nil {
		t.Fatal("invalid status")
	}
	if status.Phase != "authenticated" || status.Code != "certificate_preparing" || status.AuthURL != "" {
		t.Fatal("incorrect preparation status")
	}
	updated, err := time.Parse(time.RFC3339, status.UpdatedAt)
	if err != nil || time.Since(updated) > 3*time.Second {
		t.Fatal("stale preparation heartbeat")
	}
	if err := privatePath(c.StatusPath, false); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(b), "PRIVATE KEY") || strings.Contains(string(b), "CERTIFICATE") {
		t.Fatal("PEM leaked into status")
	}
	close(release)
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-ctx.Done():
		t.Fatal("listener did not follow valid certificate")
	}
	if listens.Load() != 1 {
		t.Fatal("incorrect listener count")
	}
}

func TestCertificateFailureNeverListens(t *testing.T) {
	validCert, validKey := certificateFixture(t, "office.test.ts.net", time.Now().Add(-time.Hour), time.Now().Add(time.Hour))
	wrongCert, wrongKey := certificateFixture(t, "other.test.ts.net", time.Now().Add(-time.Hour), time.Now().Add(time.Hour))
	for _, name := range []string{"provider_error", "invalid_certificate", "heartbeat_failure", "cancelled", "deadline"} {
		t.Run(name, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			if name == "cancelled" {
				cancel()
			}
			if name == "deadline" {
				var deadlineCancel context.CancelFunc
				ctx, deadlineCancel = context.WithTimeout(ctx, 10*time.Millisecond)
				defer deadlineCancel()
			}
			listened := false
			_, err := listenWithPreparedCertificate(ctx, "office.test.ts.net", func(ctx context.Context, _ string) ([]byte, []byte, error) {
				switch name {
				case "provider_error":
					return nil, nil, errors.New("provider-response-secret")
				case "invalid_certificate":
					return wrongCert, append([]byte(nil), wrongKey...), nil
				case "deadline":
					<-ctx.Done()
					return nil, nil, ctx.Err()
				}
				return validCert, append([]byte(nil), validKey...), nil
			}, func() error {
				if name == "heartbeat_failure" {
					return errors.New("private-path-secret")
				}
				return nil
			}, func() (net.Listener, error) { listened = true; return &queuedListener{}, nil })
			if err == nil || listened {
				t.Fatal("certificate failure opened listener")
			}
			if strings.Contains(err.Error(), "secret") {
				t.Fatal("raw error leaked")
			}
		})
	}
}

func TestCertificateRequestBoundAndCachedSuccess(t *testing.T) {
	cert, key := certificateFixture(t, "office.test.ts.net", time.Now().Add(-time.Hour), time.Now().Add(time.Hour))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	listens := 0
	l, err := listenWithPreparedCertificate(ctx, "office.test.ts.net", func(callCtx context.Context, host string) ([]byte, []byte, error) {
		deadline, ok := callCtx.Deadline()
		remaining := time.Until(deadline)
		if !ok || remaining <= 44*time.Second || remaining > 45*time.Second {
			t.Error("certificate deadline not45seconds")
		}
		return cert, append([]byte(nil), key...), nil
	}, func() error { return nil }, func() (net.Listener, error) { listens++; return &queuedListener{}, nil })
	if err != nil || l == nil || listens != 1 {
		t.Fatal("cached certificate not used", err, listens)
	}
}
