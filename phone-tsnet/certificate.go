package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"net"
	"time"
)

const certificateTimeout = 45 * time.Second
const certificateHeartbeat = 2 * time.Second

type certPairFunc func(context.Context, string) ([]byte, []byte, error)

func validateCertificate(host string, certPEM, keyPEM []byte, now time.Time) error {
	pair, err := tls.X509KeyPair(certPEM, keyPEM)
	if err != nil || len(pair.Certificate) == 0 {
		return errors.New("certificate_invalid")
	}
	leaf, err := x509.ParseCertificate(pair.Certificate[0])
	if err != nil || leaf.VerifyHostname(host) != nil || now.Before(leaf.NotBefore) || !now.Before(leaf.NotAfter) {
		return errors.New("certificate_invalid")
	}
	return nil
}

// Fetch through this embedded node's LocalClient, not the system daemon. The
// upstream client obtains/caches the certificate in the isolated tsnet state.
// Never log, return or persist PEM bytes here. In particular, do not make the
// first phone handshake perform potentially slow certificate issuance under the
// deliberately short HTTP handshake deadline.
func prepareCertificate(ctx context.Context, host string, fetch certPairFunc, heartbeat func() error) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if err := heartbeat(); err != nil {
		return errors.New("status_failed")
	}
	certCtx, cancel := context.WithTimeout(ctx, certificateTimeout)
	defer cancel()
	result := make(chan error, 1)
	go func() {
		certPEM, keyPEM, err := fetch(certCtx, host)
		if err != nil {
			result <- errors.New("certificate_unavailable")
			return
		}
		err = validateCertificate(host, certPEM, keyPEM, time.Now())
		clear(keyPEM)
		result <- err
	}()
	ticker := time.NewTicker(certificateHeartbeat)
	defer ticker.Stop()
	for {
		select {
		case <-certCtx.Done():
			if ctx.Err() != nil {
				return ctx.Err()
			}
			return errors.New("certificate_preparation_timeout")
		case err := <-result:
			// A simultaneously cancelled startup must not open a listener.
			if ctx.Err() != nil {
				return ctx.Err()
			}
			if certCtx.Err() != nil {
				return errors.New("certificate_preparation_timeout")
			}
			return err
		case <-ticker.C:
			if err := heartbeat(); err != nil {
				return errors.New("status_failed")
			}
		}
	}
}

// Keep the existing bounded ListenTLS call, but only after a usable certificate
// has been obtained. No listener exists during the certificate preparation phase.
func listenWithPreparedCertificate(ctx context.Context, host string, fetch certPairFunc, heartbeat func() error, listen func() (net.Listener, error)) (net.Listener, error) {
	if err := prepareCertificate(ctx, host, fetch, heartbeat); err != nil {
		return nil, err
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	type listenResult struct {
		listener net.Listener
		err      error
	}
	result := make(chan listenResult, 1)
	go func() { l, err := listen(); result <- listenResult{l, err} }()
	timer := time.NewTimer(12 * time.Second)
	defer timer.Stop()
	select {
	case got := <-result:
		if got.err != nil {
			return nil, errors.New("private_tls_unavailable")
		}
		if err := ctx.Err(); err != nil {
			if got.listener != nil {
				got.listener.Close()
			}
			return nil, err
		}
		return got.listener, nil
	case <-timer.C:
		return nil, errors.New("private_tls_timeout")
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}
