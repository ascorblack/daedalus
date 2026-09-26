package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestParse(t *testing.T) {
	dir := t.TempDir()
	c, err := Parse([]string{"--env", "container", "--run-dir", dir + "/run", "--state-dir", dir + "/state"})
	if err != nil {
		t.Fatal(err)
	}
	if c.Limits.MaxBrowsers != 2 || c.Limits.FPSCap != 15 || c.Limits.IdleClose != 10*time.Minute {
		t.Fatalf("defaults: %+v", c.Limits)
	}
	for _, args := range [][]string{
		{"--run-dir", "r", "--state-dir", "s"},
		{"--env", "x", "--state-dir", "s"},
		{"--env", "x", "--run-dir", "r", "--state-dir", "s", "--listen", "tcp:0.0.0.0:1"},
		{"--env", "x", "--run-dir", "r", "--state-dir", "s", "extra"},
	} {
		if _, err := Parse(args); err == nil {
			t.Errorf("%v was accepted", args)
		}
	}
}

func TestConfigFile(t *testing.T) {
	dir := t.TempDir()
	write := func(body string) string {
		p := filepath.Join(dir, "c.json")
		if err := os.WriteFile(p, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
		return p
	}
	base := []string{"--env", "x", "--run-dir", dir + "/r", "--state-dir", dir + "/s", "--config"}
	c, err := Parse(append(base, write(`{"limits": {"max_browsers": 4, "idle_close_ms": 1000, "fps_cap": 10},
		"chromium": {"path": "/opt/c/chrome", "args": ["--lang=ru"], "no_sandbox": true}}`)))
	if err != nil {
		t.Fatal(err)
	}
	if c.Limits.MaxBrowsers != 4 || c.Limits.IdleClose != time.Second || c.Limits.FPSCap != 10 ||
		c.Chromium.Path != "/opt/c/chrome" || !c.Chromium.NoSandbox || c.Chromium.Args[0] != "--lang=ru" {
		t.Fatalf("file: %+v %+v", c.Limits, c.Chromium)
	}
	// The flag wins over the file.
	c, err = Parse(append(append(base, filepath.Join(dir, "c.json")), "--chromium", "/other"))
	if err != nil || c.Chromium.Path != "/other" {
		t.Fatalf("flag over file: %v %v", c, err)
	}
	for _, bad := range []string{`{"limits": {"max_browser": 4}}`, `{"limits": {"max_browsers": 0}}`,
		`{"limits": {"fps_cap": 61}}`, `{"chromium": {"path": "relative"}}`, `{"extra": 1}`} {
		if _, err := Parse(append(base, write(bad))); err == nil || !strings.Contains(err.Error(), "c.json") {
			t.Errorf("%s: %v", bad, err)
		}
	}
}
