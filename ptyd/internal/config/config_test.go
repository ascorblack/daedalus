package config

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func TestParse(t *testing.T) {
	dir := t.TempDir()
	c, err := Parse([]string{"--env", "host", "--run-dir", dir + "/run", "--state-dir", dir + "/state", "--home", dir})
	if err != nil {
		t.Fatal(err)
	}
	if c.Env != "host" || c.Listen != DefaultListen(runtime.GOOS) || c.Limits.RingBytes != DefaultRingBytes || c.Shell == "" {
		t.Fatalf("%+v", c)
	}
	bad := [][]string{
		{"--run-dir", "r", "--state-dir", "s"},
		{"--env", "x", "--state-dir", "s"},
		{"--env", "x", "--run-dir", "r", "--state-dir", "s", "--listen", "tcp:0.0.0.0:1"},
		{"--env", "x", "--run-dir", "r", "--state-dir", "s", "extra"},
	}
	for _, args := range bad {
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
	args := func(path string) []string {
		return []string{"--env", "x", "--run-dir", dir, "--state-dir", dir, "--config", path}
	}
	c, err := Parse(args(write(`{"limits":{"max_terminals":7,"ring_bytes":2097152,"input_idle_ms":500,"kill_grace_ms":100}}`)))
	if err != nil {
		t.Fatal(err)
	}
	if c.Limits.MaxTerminals != 7 || c.Limits.RingBytes != 2<<20 || c.Limits.InputIdle != 500*time.Millisecond || c.Limits.KillGrace != 100*time.Millisecond {
		t.Fatalf("%+v", c.Limits)
	}
	for _, body := range []string{`{"limits":{"max_terminal":7}}`, `{"limits":{"ring_bytes":5}}`, `{"limits":{"kill_grace_ms":999999}}`, `nope`} {
		if _, err := Parse(args(write(body))); err == nil || !strings.Contains(err.Error(), "config") {
			t.Errorf("%s: %v", body, err)
		}
	}
}

func TestAWindowsHostPrefersPowerShell7(t *testing.T) {
	found := func(names ...string) func(string) (string, error) {
		return func(name string) (string, error) {
			for _, n := range names {
				if n == name {
					return `C:\Program Files\PowerShell\7\` + name, nil
				}
			}
			return "", os.ErrNotExist
		}
	}
	if got := WindowsShell(found("pwsh.exe", "powershell.exe")); got != "pwsh.exe" {
		t.Errorf("with pwsh installed: %q", got)
	}
	if got := WindowsShell(found("powershell.exe")); got != "powershell.exe" {
		t.Errorf("without pwsh: %q", got)
	}
	if got := WindowsShell(found()); got != "powershell.exe" {
		t.Errorf("with nothing found: %q", got)
	}
}

func TestWindowsListensOnLoopbackTCP(t *testing.T) {
	if got := DefaultListen("windows"); got != "tcp:127.0.0.1:0" {
		t.Errorf("windows: %q", got)
	}
	for _, goos := range []string{"linux", "darwin"} {
		if got := DefaultListen(goos); got != "unix" {
			t.Errorf("%s: %q", goos, got)
		}
	}
}
