package main

import (
	"encoding/json"
	"errors"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"tailscale.com/ipn/ipnstate"
)

type OwnStatus struct {
	NodeID   string `json:"node_id"`
	Identity string `json:"identity"`
	DNSName  string `json:"dns_name"`
}
type PeerStatus struct {
	NodeID   string `json:"node_id"`
	Hostname string `json:"hostname"`
	OS       string `json:"os"`
	Online   bool   `json:"online"`
}
type Status struct {
	Version   int          `json:"version"`
	Phase     string       `json:"phase"`
	PID       int          `json:"pid"`
	UpdatedAt string       `json:"updated_at"`
	Code      string       `json:"code,omitempty"`
	AuthURL   string       `json:"auth_url,omitempty"`
	Own       *OwnStatus   `json:"own,omitempty"`
	Peers     []PeerStatus `json:"peers,omitempty"`
}

func safeAuthURL(raw string) string {
	u, err := url.Parse(raw)
	if err != nil || len(raw) > 4096 || u.Scheme != "https" || u.Host != "login.tailscale.com" || u.User != nil || u.Fragment != "" || strings.ContainsAny(raw, "\r\n\x00") {
		return ""
	}
	return raw
}

func statusFromNode(st *ipnstate.Status) Status {
	out := Status{Phase: "starting"}
	if st == nil {
		return out
	}
	switch st.BackendState {
	case "NeedsLogin":
		out.Phase = "login_required"
		out.AuthURL = safeAuthURL(st.AuthURL)
	case "NeedsMachineAuth":
		out.Phase = "device_approval_required"
	case "Running":
		out.Phase = "authenticated"
		if st.Self != nil {
			profile := st.User[st.Self.UserID]
			out.Own = &OwnStatus{NodeID: string(st.Self.ID), Identity: profile.LoginName, DNSName: strings.TrimSuffix(st.Self.DNSName, ".")}
			for _, peer := range st.Peer {
				// Only owned, untagged nodes are eligible phone candidates.
				if peer == nil || peer.UserID != st.Self.UserID || peer.AltSharerUserID != 0 || (peer.Tags != nil && peer.Tags.Len() != 0) || peer.ID == "" {
					continue
				}
				out.Peers = append(out.Peers, PeerStatus{string(peer.ID), peer.HostName, peer.OS, peer.Online})
			}
			sort.Slice(out.Peers, func(i, j int) bool { return out.Peers[i].NodeID < out.Peers[j].NodeID })
		}
	}
	return out
}

func writeStatus(path string, s Status) error {
	if err := privatePath(filepath.Dir(path), true); err != nil {
		return err
	}
	if _, err := os.Lstat(path); err == nil {
		if err := privatePath(path, false); err != nil {
			return err
		}
	} else if !os.IsNotExist(err) {
		return errors.New("status_unavailable")
	}
	s.Version = 1
	s.PID = os.Getpid()
	s.UpdatedAt = time.Now().UTC().Format(time.RFC3339)
	b, err := json.Marshal(s)
	if err != nil {
		return errors.New("status_failed")
	}
	f, err := os.CreateTemp(filepath.Dir(path), ".phone-status-")
	if err != nil {
		return errors.New("status_failed")
	}
	name := f.Name()
	defer os.Remove(name)
	defer f.Close()
	if err = f.Chmod(0600); err == nil {
		_, err = f.Write(append(b, '\n'))
	}
	if err == nil {
		err = f.Sync()
	}
	if err == nil {
		err = f.Close()
	}
	if err == nil {
		err = os.Rename(name, path)
	}
	if err != nil {
		return errors.New("status_failed")
	}
	return nil
}
