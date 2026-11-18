package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func configFixture(t *testing.T) (Config, string) {
	t.Helper()
	dir := t.TempDir()
	os.Chmod(dir, 0700)
	state := filepath.Join(dir, "state")
	if err := os.Mkdir(state, 0700); err != nil {
		t.Fatal(err)
	}
	c := proxyConfig()
	c.Mode = "serve"
	c.Hostname = "office-mobile"
	c.StateDir = state
	c.StatusPath = filepath.Join(dir, "status.json")
	c.BackendPort = 4319
	c.CorePort = 4318
	return c, filepath.Join(dir, "config.json")
}
func saveConfig(t *testing.T, c Config, p string) {
	t.Helper()
	b, e := json.Marshal(c)
	if e != nil {
		t.Fatal(e)
	}
	if e = os.WriteFile(p, b, 0600); e != nil {
		t.Fatal(e)
	}
}
func TestConfigValidAndLoginOnly(t *testing.T) {
	c, p := configFixture(t)
	saveConfig(t, c, p)
	if _, e := loadConfig(p); e != nil {
		t.Fatal(e)
	}
	c.Mode = "login"
	c.ExpectedOrigin = ""
	c.ExpectedIdentity = ""
	c.AllowedNodeIDs = nil
	saveConfig(t, c, p)
	if _, e := loadConfig(p); e != nil {
		t.Fatal(e)
	}
}
func TestConfigRejectsUnsafeTargets(t *testing.T) {
	cases := []struct {
		name string
		edit func(*Config)
	}{
		{"core_port", func(c *Config) { c.BackendPort = 4318 }},
		{"privileged_port", func(c *Config) { c.BackendPort = 443 }},
		{"no_phone", func(c *Config) { c.AllowedNodeIDs = nil }},
		{"invalid_phone", func(c *Config) { c.AllowedNodeIDs = []string{"*"} }},
		{"duplicate_phone", func(c *Config) { c.AllowedNodeIDs = []string{"x", "x"} }},
		{"public_origin", func(c *Config) { c.ExpectedOrigin = "https://example.com" }},
		{"origin_user", func(c *Config) { c.ExpectedOrigin = "https://user@office.test.ts.net" }},
		{"origin_port", func(c *Config) { c.ExpectedOrigin = "https://office.test.ts.net:443" }},
		{"origin_path", func(c *Config) { c.ExpectedOrigin += "/" }},
		{"identity_crlf", func(c *Config) { c.ExpectedIdentity += "\r\nForged: yes" }},
		{"bad_mode", func(c *Config) { c.Mode = "funnel" }},
		{"status_in_state", func(c *Config) { c.StatusPath = filepath.Join(c.StateDir, "tailscaled.state") }},
		{"relative_state", func(c *Config) { c.StateDir = "state" }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			c, p := configFixture(t)
			tc.edit(&c)
			saveConfig(t, c, p)
			if _, e := loadConfig(p); e == nil {
				t.Fatal("accepted unsafe config")
			}
		})
	}
}
func TestConfigRejectsDuplicatesUnknownsAndUnsafeFiles(t *testing.T) {
	for _, suffix := range []string{`,"mode":"login"}`, `,"MODE":"login"}`, `,"auth_key":"secret"}`, `} {}`} {
		c, p := configFixture(t)
		saveConfig(t, c, p)
		b, _ := os.ReadFile(p)
		os.WriteFile(p, []byte(strings.TrimSuffix(string(b), "}")+suffix), 0600)
		if _, e := loadConfig(p); e == nil {
			t.Fatal(suffix)
		}
	}
	for _, kind := range []string{"config_permissions", "parent_permissions", "state_permissions", "config_symlink", "state_symlink", "status_symlink"} {
		t.Run(kind, func(t *testing.T) {
			c, p := configFixture(t)
			saveConfig(t, c, p)
			switch kind {
			case "config_permissions":
				os.Chmod(p, 0644)
			case "parent_permissions":
				os.Chmod(filepath.Dir(p), 0755)
			case "state_permissions":
				os.Chmod(c.StateDir, 0755)
			case "config_symlink":
				os.Rename(p, p+".real")
				os.Symlink(p+".real", p)
			case "state_symlink":
				os.Rename(c.StateDir, c.StateDir+".real")
				os.Symlink(c.StateDir+".real", c.StateDir)
			case "status_symlink":
				os.Symlink(p, c.StatusPath)
			}
			if _, e := loadConfig(p); e == nil {
				t.Fatal("accepted unsafe file")
			}
		})
	}
}

func TestStatusPrivateAtomicAndOfficialAuthURL(t *testing.T) {
	c, _ := configFixture(t)
	if e := writeStatus(c.StatusPath, Status{Phase: "login_required", AuthURL: "https://login.tailscale.com/a/private"}); e != nil {
		t.Fatal(e)
	}
	if e := privatePath(c.StatusPath, false); e != nil {
		t.Fatal(e)
	}
	for _, u := range []string{"http://login.tailscale.com/a/x", "https://login.tailscale.com.attacker.invalid/a", "https://user:pass@login.tailscale.com/a", "https://login.tailscale.com:443/a", "https://login.tailscale.com/a#secret", "https://evil.invalid"} {
		if safeAuthURL(u) != "" {
			t.Error(u)
		}
	}
	if safeAuthURL("https://login.tailscale.com/a/abc") == "" {
		t.Fatal("valid login rejected")
	}
	os.Remove(c.StatusPath)
	os.Symlink(filepath.Join(filepath.Dir(c.StatusPath), "missing"), c.StatusPath)
	if e := writeStatus(c.StatusPath, Status{Phase: "ready"}); e == nil {
		t.Fatal("followed symlink")
	}
}
