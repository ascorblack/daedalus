package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestSideChannelSettings(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "c.json")
	write := func(body string) {
		if err := os.WriteFile(p, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	args := func(extra ...string) []string {
		return append([]string{"--env", "x", "--run-dir", dir, "--state-dir", dir, "--config", p}, extra...)
	}
	write(`{"exec":{"allow":["bun"]},"fs":{"roots":["/srv/extra"],"deny":["**/*.pem"]}}`)
	c, err := Parse(args())
	if err != nil {
		t.Fatal(err)
	}
	if len(c.Side.ExecAllow) != 1 || c.Side.FSRoots[0] != "/srv/extra" || c.Side.FSDeny[0] != "**/*.pem" || c.HooksListen != "127.0.0.1:0" {
		t.Fatalf("%+v %q", c.Side, c.HooksListen)
	}
	for _, body := range []string{`{"exec":{"allow":["/bin/sh"]}}`, `{"fs":{"roots":["relative"]}}`, `{"fs":{"deny":[""]}}`, `{"fs":{"allow":[]}}`} {
		write(body)
		if _, err := Parse(args()); err == nil {
			t.Errorf("%s was accepted", body)
		}
	}
	write(`{}`)
	for _, addr := range []string{"0.0.0.0:0", "192.0.2.1:9000", ":9000", "localhost"} {
		if _, err := Parse(args("--hooks-listen", addr)); err == nil {
			t.Errorf("--hooks-listen %s was accepted", addr)
		}
	}
	if _, err := Parse(args("--hooks-listen", "[::1]:0")); err != nil {
		t.Errorf("IPv6 loopback: %v", err)
	}
}
