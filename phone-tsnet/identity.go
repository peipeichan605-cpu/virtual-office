package main

import (
	"context"
	"errors"
	"time"

	"tailscale.com/ipn/ipnstate"
)

const identityPreparationTimeout = 15 * time.Second
const identityPreparationPoll = 2 * time.Second

type statusFunc func(context.Context) (*ipnstate.Status, error)

// A Running backend is not a promise that every self/profile field is already
// present. Missing metadata can settle during startup; populated mismatches can
// never be treated as an initialization condition.
func selfMetadataReady(c Config, own *OwnStatus) (bool, error) {
	host, _ := originHost(c.ExpectedOrigin)
	if own != nil && ((own.DNSName != "" && own.DNSName != host) || (own.Identity != "" && own.Identity != c.ExpectedIdentity)) {
		return false, errors.New("node_identity_mismatch")
	}
	if own == nil || own.NodeID == "" || own.DNSName == "" || own.Identity == "" {
		return false, nil
	}
	return true, validSelf(c, own)
}

// Only called BEFORE a listener exists. Once serving, main retains its original
// immediate validSelf failure for either missing or mismatched metadata.
func waitForStartupIdentity(ctx context.Context, c Config, initial *ipnstate.Status, fetch statusFunc, heartbeat func(Status) error) (*ipnstate.Status, error) {
	waitCtx, cancel := context.WithTimeout(ctx, identityPreparationTimeout)
	defer cancel()
	node := initial
	for {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		if waitCtx.Err() != nil {
			return nil, errors.New("node_identity_initialization_timeout")
		}
		if node == nil || node.BackendState != "Running" {
			return nil, errors.New("node_not_ready")
		}
		if node.TUN || node.ExitNodeStatus != nil {
			return nil, errors.New("unexpected_network_mode")
		}
		if node.Self != nil && node.Self.Tags != nil && node.Self.Tags.Len() != 0 {
			return nil, errors.New("tagged_node_not_supported")
		}
		snapshot := statusFromNode(node)
		ready, err := selfMetadataReady(c, snapshot.Own)
		if err != nil {
			return nil, err
		}
		if ready {
			return node, nil
		}
		snapshot.Code = "initializing_identity"
		if heartbeat(snapshot) != nil {
			return nil, errors.New("status_failed")
		}
		timer := time.NewTimer(identityPreparationPoll)
		select {
		case <-waitCtx.Done():
			timer.Stop()
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			return nil, errors.New("node_identity_initialization_timeout")
		case <-timer.C:
		}
		queryCtx, queryCancel := context.WithTimeout(waitCtx, 3*time.Second)
		node, err = fetch(queryCtx)
		queryCancel()
		if err != nil {
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			if waitCtx.Err() != nil {
				return nil, errors.New("node_identity_initialization_timeout")
			}
			return nil, errors.New("node_status_unavailable")
		}
	}
}
