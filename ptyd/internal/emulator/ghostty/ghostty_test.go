//go:build cgo

package ghostty

import (
	"bytes"
	"encoding/json"
	"flag"
	"os"
	"testing"

	"github.com/ascorblack/daedalus/ptyd/internal/emulator"

	"github.com/ascorblack/daedalus/ptyd/internal/emulator/conformance"
)

func TestConformance(t *testing.T) { conformance.Run(t, Factory) }

func TestContract(t *testing.T) { conformance.RunContract(t, Factory) }

var update = flag.Bool("update", false, "rewrite the golden snapshots")

type golden struct {
	Name string   `json:"name"`
	Cols int      `json:"cols"`
	Rows int      `json:"rows"`
	VT   string   `json:"vt"`   // the snapshot
	Text []string `json:"text"` // what the screen and history say, as AllText reads them
}

// The snapshots of the conformance cases are kept as data: the browser side replays them into
// xterm.js and compares the text, and a change in how the library formats a screen shows up here as
// a diff to review rather than silently.
func TestGoldenSnapshots(t *testing.T) {
	var want []golden
	for _, c := range conformance.Cases {
		e := conformance.Feed(Factory, c)
		snap, _ := e.Snapshot(emulator.SnapshotOptions{Scrollback: 1000})
		cols, rows := e.Size()
		want = append(want, golden{Name: c.Name, Cols: cols, Rows: rows, VT: string(snap), Text: conformance.AllText(e)})
		e.Close()
	}
	data, err := json.MarshalIndent(want, "", " ")
	if err != nil {
		t.Fatal(err)
	}
	data = append(data, '\n')
	const path = "testdata/snapshots.json"
	if *update {
		if err := os.WriteFile(path, data, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	have, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("%v; run with -update", err)
	}
	if !bytes.Equal(have, data) {
		t.Errorf("the snapshots differ from %s; review the difference and run with -update", path)
	}
}
