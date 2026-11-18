package main

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"tailscale.com/ipn/ipnstate"
	"tailscale.com/tailcfg"
)

func completeSelfStatus() *ipnstate.Status {
	return &ipnstate.Status{BackendState: "Running", Self: &ipnstate.PeerStatus{ID: "nOffice", UserID: 42, DNSName: "office.test.ts.net."}, User: map[tailcfg.UserID]tailcfg.UserProfile{42: {ID: 42, LoginName: "owner@example.invalid"}}}
}

func TestStartupIdentityIncompleteThenExactMatch(t *testing.T) {
	initial := completeSelfStatus()
	initial.User = nil
	complete := completeSelfStatus()
	fetches := 0
	heartbeats := 0
	got, err := waitForStartupIdentity(context.Background(), proxyConfig(), initial, func(ctx context.Context) (*ipnstate.Status, error) {
		fetches++
		deadline, ok := ctx.Deadline()
		if !ok || time.Until(deadline) > 3*time.Second {
			t.Error("status query not bounded")
		}
		return complete, nil
	}, func(status Status) error {
		heartbeats++
		if status.Phase != "authenticated" || status.Code != "initializing_identity" {
			t.Error("premature ready status")
		}
		return nil
	})
	if err != nil || got != complete || fetches != 1 || heartbeats != 1 {
		t.Fatal("initialization failed", err, fetches, heartbeats)
	}
	if validSelf(proxyConfig(), statusFromNode(got).Own) != nil {
		t.Fatal("incomplete identity accepted")
	}
}

func TestStartupIdentityRejectsPopulatedMismatchesImmediately(t *testing.T) {
	for _, name := range []string{"wrong_dns_missing_profile", "wrong_user_missing_dns", "wrong_dns", "wrong_user"} {
		t.Run(name, func(t *testing.T) {
			node := completeSelfStatus()
			switch name {
			case "wrong_dns_missing_profile":
				node.Self.DNSName = "other.test.ts.net."
				node.User = nil
			case "wrong_user_missing_dns":
				node.Self.DNSName = ""
				node.User[42] = tailcfg.UserProfile{ID: 42, LoginName: "other@example.invalid"}
			case "wrong_dns":
				node.Self.DNSName = "other.test.ts.net."
			case "wrong_user":
				node.User[42] = tailcfg.UserProfile{ID: 42, LoginName: "other@example.invalid"}
			}
			_, err := waitForStartupIdentity(context.Background(), proxyConfig(), node, func(context.Context) (*ipnstate.Status, error) { t.Error("mismatch was retried"); return nil, nil }, func(Status) error { t.Error("mismatch was reported as initializing"); return nil })
			if err == nil || err.Error() != "node_identity_mismatch" {
				t.Fatal("mismatch accepted", err)
			}
		})
	}
}

func TestStartupIdentityTimeoutWithoutMetadata(t *testing.T) {
	start := time.Now()
	heartbeats := 0
	_, err := waitForStartupIdentity(context.Background(), proxyConfig(), &ipnstate.Status{BackendState: "Running"}, func(ctx context.Context) (*ipnstate.Status, error) {
		return &ipnstate.Status{BackendState: "Running"}, nil
	}, func(status Status) error {
		heartbeats++
		if status.Phase == "ready" {
			t.Error("ready before metadata")
		}
		return nil
	})
	elapsed := time.Since(start)
	if err == nil || err.Error() != "node_identity_initialization_timeout" || elapsed < 14*time.Second || elapsed > 17*time.Second || heartbeats < 2 {
		t.Fatal("wait not bounded", err, elapsed, heartbeats)
	}
}

func TestStartupIdentityCancellationAndFailure(t *testing.T) {
	for _, name := range []string{"cancelled", "cancel_during_heartbeat", "heartbeat_failure", "not_running", "unsafe_network"} {
		t.Run(name, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			node := &ipnstate.Status{BackendState: "Running"}
			if name == "cancelled" {
				cancel()
			}
			if name == "not_running" {
				node.BackendState = "NeedsLogin"
			}
			if name == "unsafe_network" {
				node.TUN = true
			}
			_, err := waitForStartupIdentity(ctx, proxyConfig(), node, func(context.Context) (*ipnstate.Status, error) { t.Error("unexpected fetch"); return nil, nil }, func(Status) error {
				if name == "cancel_during_heartbeat" {
					cancel()
				}
				if name == "heartbeat_failure" {
					return errors.New("private-secret")
				}
				return nil
			})
			if err == nil || strings.Contains(err.Error(), "private-secret") {
				t.Fatal("failure not closed", err)
			}
		})
	}
}

func TestServingIdentityStillRejectsMissingMetadata(t *testing.T) {
	for _, own := range []*OwnStatus{nil, {}, {NodeID: "nOffice", DNSName: "office.test.ts.net"}, {NodeID: "nOffice", Identity: "owner@example.invalid"}} {
		if validSelf(proxyConfig(), own) == nil {
			t.Fatal("post-ready metadata requirement weakened")
		}
	}
}

func TestStartupIdentityOnlyMissingFieldsCanWait(t *testing.T) {
	for _, own := range []*OwnStatus{nil, {}, {Identity: "owner@example.invalid", DNSName: "office.test.ts.net"}, {NodeID: "nOffice", DNSName: "office.test.ts.net"}, {NodeID: "nOffice", Identity: "owner@example.invalid"}} {
		ready, err := selfMetadataReady(proxyConfig(), own)
		if ready || err != nil {
			t.Fatal("missing metadata did not remain closed and pending", ready, err)
		}
	}
	complete := statusFromNode(completeSelfStatus()).Own
	if ready, err := selfMetadataReady(proxyConfig(), complete); !ready || err != nil {
		t.Fatal("complete exact metadata rejected", ready, err)
	}
}
