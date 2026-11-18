package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// Config contains no auth keys. Only the installation's own isolated tsnet
// identity can be used; login happens through the provider's browser flow.
type Config struct {
	Mode             string   `json:"mode"`
	StateDir         string   `json:"state_dir"`
	StatusPath       string   `json:"status_path"`
	Hostname         string   `json:"hostname"`
	BackendPort      int      `json:"backend_port"`
	CorePort         int      `json:"core_port"`
	ExpectedOrigin   string   `json:"expected_origin"`
	ExpectedIdentity string   `json:"expected_identity"`
	AllowedNodeIDs   []string `json:"allowed_node_ids"`
}

var dnsLabel = regexp.MustCompile(`^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$`)
var stableID = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)

func privatePath(path string, dir bool) error {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path {
		return errors.New("invalid_private_path")
	}
	fi, err := os.Lstat(path)
	if err != nil {
		return errors.New("private_path_unavailable")
	}
	if fi.Mode()&os.ModeSymlink != 0 || fi.IsDir() != dir || (!dir && !fi.Mode().IsRegular()) || !ownedByCurrentUser(fi) {
		return errors.New("unsafe_private_path")
	}
	want := os.FileMode(0600)
	if dir {
		want = 0700
	}
	if fi.Mode().Perm() != want {
		return errors.New("unsafe_private_permissions")
	}
	return nil
}

func originHost(origin string) (string, error) {
	u, err := url.Parse(origin)
	if err != nil || u.Scheme != "https" || u.User != nil || u.Port() != "" || u.Path != "" || u.RawQuery != "" || u.Fragment != "" || u.Opaque != "" || u.Host != strings.ToLower(u.Host) || !strings.HasSuffix(u.Host, ".ts.net") {
		return "", errors.New("invalid_origin")
	}
	labels := strings.Split(u.Host, ".")
	if len(labels) < 4 {
		return "", errors.New("invalid_origin")
	}
	for _, label := range labels {
		if !dnsLabel.MatchString(label) {
			return "", errors.New("invalid_origin")
		}
	}
	return u.Host, nil
}

func loadConfig(path string) (Config, error) {
	var c Config
	if err := privatePath(filepath.Dir(path), true); err != nil {
		return c, err
	}
	if err := privatePath(path, false); err != nil {
		return c, err
	}
	f, err := os.Open(path)
	if err != nil {
		return c, errors.New("config_unavailable")
	}
	defer f.Close()
	b, err := io.ReadAll(io.LimitReader(f, 16385))
	if err != nil || len(b) > 16384 {
		return c, errors.New("invalid_config")
	}
	// Reject duplicate top-level fields; silently overriding a security field is
	// not a valid configuration. All nested data here is an array of strings.
	d := json.NewDecoder(bytes.NewReader(b))
	t, err := d.Token()
	if err != nil || t != json.Delim('{') {
		return c, errors.New("invalid_config")
	}
	seen := map[string]bool{}
	keys := map[string]bool{"mode": true, "state_dir": true, "status_path": true, "hostname": true, "backend_port": true, "core_port": true, "expected_origin": true, "expected_identity": true, "allowed_node_ids": true}
	for d.More() {
		k, err := d.Token()
		if err != nil {
			return c, errors.New("invalid_config")
		}
		name, ok := k.(string)
		if !ok || !keys[name] || seen[name] {
			return c, errors.New("duplicate_or_unknown_config_field")
		}
		seen[name] = true
		var v json.RawMessage
		if d.Decode(&v) != nil {
			return c, errors.New("invalid_config")
		}
	}
	d = json.NewDecoder(bytes.NewReader(b))
	d.DisallowUnknownFields()
	if d.Decode(&c) != nil {
		return c, errors.New("invalid_config")
	}
	var extra any
	if d.Decode(&extra) != io.EOF {
		return c, errors.New("invalid_config")
	}
	if c.BackendPort == 0 {
		c.BackendPort = 4319
	}
	if c.CorePort == 0 {
		c.CorePort = 4318
	}
	if c.Mode != "login" && c.Mode != "serve" {
		return c, errors.New("invalid_mode")
	}
	if !dnsLabel.MatchString(c.Hostname) {
		return c, errors.New("invalid_hostname")
	}
	if c.BackendPort < 1024 || c.BackendPort > 65535 || c.CorePort < 1024 || c.CorePort > 65535 || c.BackendPort == c.CorePort {
		return c, errors.New("invalid_backend_port")
	}
	if err := privatePath(c.StateDir, true); err != nil {
		return c, err
	}
	if !filepath.IsAbs(c.StatusPath) || filepath.Clean(c.StatusPath) != c.StatusPath || c.StatusPath == path || c.StatusPath == c.StateDir || strings.HasPrefix(c.StatusPath, c.StateDir+string(os.PathSeparator)) {
		return c, errors.New("invalid_status_path")
	}
	if err := privatePath(filepath.Dir(c.StatusPath), true); err != nil {
		return c, err
	}
	if _, err := os.Lstat(c.StatusPath); err == nil {
		if err := privatePath(c.StatusPath, false); err != nil {
			return c, err
		}
	} else if !os.IsNotExist(err) {
		return c, errors.New("invalid_status_path")
	}
	if c.Mode == "serve" {
		if _, err := originHost(c.ExpectedOrigin); err != nil {
			return c, err
		}
		if strings.TrimSpace(c.ExpectedIdentity) != c.ExpectedIdentity || c.ExpectedIdentity == "" || len(c.ExpectedIdentity) > 254 || strings.ContainsAny(c.ExpectedIdentity, "\r\n\x00") {
			return c, errors.New("invalid_identity")
		}
		if len(c.AllowedNodeIDs) == 0 || len(c.AllowedNodeIDs) > 16 {
			return c, errors.New("phone_node_required")
		}
		ids := map[string]bool{}
		for _, id := range c.AllowedNodeIDs {
			if !stableID.MatchString(id) || ids[id] {
				return c, errors.New("invalid_node_id")
			}
			ids[id] = true
		}
	}
	return c, nil
}
