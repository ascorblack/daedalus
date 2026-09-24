//go:build cgo

package ghostty

import (
	"bytes"
	"flag"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ascorblack/daedalus/ptyd/internal/emulator"

	"github.com/ascorblack/daedalus/ptyd/internal/emulator/conformance"
)

func TestConformance(t *testing.T) { conformance.Run(t, Factory) }

func TestContract(t *testing.T) { conformance.RunContract(t, Factory) }

var update = flag.Bool("update", false, "rewrite the golden snapshots")

// The snapshots of the conformance cases are kept as files: the browser side replays them into
// xterm.js and compares the text, and a change in how the library formats a screen shows up here as
// a diff to review rather than silently.
func TestGoldenSnapshots(t *testing.T) {
	for _, c := range conformance.Cases {
		name := slug(c.Name)
		e := conformance.Feed(Factory, c)
		snap, _ := e.Snapshot(emulator.SnapshotOptions{Scrollback: 1000})
		text := strings.Join(conformance.AllText(e), "\n") + "\n"
		e.Close()
		vt, txt := filepath.Join("testdata", "snapshots", name+".vt"), filepath.Join("testdata", "snapshots", name+".txt")
		if *update {
			if err := os.MkdirAll(filepath.Dir(vt), 0o755); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(vt, snap, 0o644); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(txt, []byte(text), 0o644); err != nil {
				t.Fatal(err)
			}
		}
		haveVT, err1 := os.ReadFile(vt)
		haveTxt, err2 := os.ReadFile(txt)
		if err1 != nil || err2 != nil {
			t.Fatalf("%s: missing golden files; run with -update", c.Name)
		}
		if !bytes.Equal(haveVT, snap) || string(haveTxt) != text {
			t.Errorf("%s: snapshot differs from %s; review and run with -update", c.Name, vt)
		}
	}
}

func slug(s string) string {
	var b strings.Builder
	for _, r := range strings.ToLower(s) {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9':
			b.WriteRune(r)
		case b.Len() > 0 && !strings.HasSuffix(b.String(), "-"):
			b.WriteByte('-')
		}
	}
	return strings.TrimSuffix(b.String(), "-")
}
