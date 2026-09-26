//go:build cgo && unix

package rpc_test

import (
	"encoding/base64"
	"strings"
	"testing"

	"github.com/ascorblack/daedalus/ptyd/internal/emulator/production"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

func TestScreenMethods(t *testing.T) {
	f := startWith(t, production.Factory)
	f.call(t, "terminal.create", map[string]any{"id": "s1", "cols": 40, "rows": 6,
		"argv": []string{"sh", "-c", `i=0; while [ $i -lt 20 ]; do echo "row $i"; i=$((i+1)); done; printf '\033]2;the title\007\033[1;32mgreen\033[0m'; sleep 30`}}, nil)
	f.waitOutput(t, "s1", "green")

	var waited struct {
		Matched string `json:"matched"`
		Match   string `json:"match"`
		Seq     int64  `json:"seq"`
	}
	f.call(t, "terminal.wait_for", map[string]any{"id": "s1", "regex": `gr\w+`, "timeout_ms": 5000}, &waited)
	if waited.Matched != "regex" || waited.Match != "green" || waited.Seq == 0 {
		t.Fatalf("wait_for: %+v", waited)
	}

	var screen struct {
		Cols, Rows  int
		Title       string   `json:"title"`
		Lines       []string `json:"lines"`
		FirstAbsRow int64    `json:"first_abs_row"`
		Cursor      struct {
			X, Y   int
			AbsRow int64 `json:"abs_row"`
		} `json:"cursor"`
		AltScreen bool `json:"alt_screen"`
	}
	f.call(t, "terminal.read_screen", map[string]any{"id": "s1", "scrollback": 3}, &screen)
	if screen.Cols != 40 || screen.Rows != 6 || screen.Title != "the title" || screen.AltScreen {
		t.Fatalf("read_screen: %+v", screen)
	}
	want := "row 12|row 13|row 14|row 15|row 16|row 17|row 18|row 19|green"
	if strings.Join(screen.Lines, "|") != want {
		t.Fatalf("lines %q", screen.Lines)
	}
	if screen.Cursor.X != 5 || screen.Cursor.Y != 5 || screen.Cursor.AbsRow != 20 || screen.FirstAbsRow != 12 {
		t.Fatalf("cursor %+v, first row %d", screen.Cursor, screen.FirstAbsRow)
	}

	var tail struct {
		Lines []string `json:"lines"`
	}
	f.call(t, "terminal.read_screen", map[string]any{"id": "s1", "scrollback": 100, "tail_rows": 2}, &tail)
	if strings.Join(tail.Lines, "|") != "row 19|green" {
		t.Fatalf("tail %q", tail.Lines)
	}

	var runs struct {
		Runs [][]struct {
			T  string `json:"t"`
			FG int    `json:"fg"`
			B  bool   `json:"b"`
		} `json:"runs"`
	}
	f.call(t, "terminal.read_screen", map[string]any{"id": "s1", "format": "runs"}, &runs)
	last := runs.Runs[len(runs.Runs)-1]
	if len(last) != 1 || last[0].T != "green" || last[0].FG != 3 || !last[0].B {
		t.Fatalf("runs %+v", runs.Runs)
	}

	var snap struct {
		Cols, Rows  int
		Seq         int64  `json:"seq"`
		FirstAbsRow int64  `json:"first_abs_row"`
		DataB64     string `json:"data_b64"`
	}
	f.call(t, "terminal.snapshot", map[string]any{"id": "s1", "scrollback": 2}, &snap)
	vt, err := base64.StdEncoding.DecodeString(snap.DataB64)
	if err != nil || snap.Cols != 40 || snap.Rows != 6 || snap.FirstAbsRow != 13 || !strings.Contains(string(vt), "green") {
		t.Fatalf("snapshot %+v %v", snap, err)
	}

	var list struct {
		Terminals []struct {
			ID      string `json:"id"`
			Preview [][]struct {
				T string `json:"t"`
			} `json:"preview"`
		} `json:"terminals"`
	}
	f.call(t, "terminal.list", map[string]any{"preview_rows": 2}, &list)
	if len(list.Terminals) != 1 || len(list.Terminals[0].Preview) != 2 || list.Terminals[0].Preview[1][0].T != "green" {
		t.Fatalf("list preview %+v", list.Terminals)
	}

	for _, c := range []struct {
		method string
		params map[string]any
		code   int
	}{
		{"terminal.read_screen", map[string]any{"id": "s1", "format": "html"}, wire.CodeInvalidParams},
		{"terminal.read_screen", map[string]any{"id": "s1", "scrollback": 10001}, wire.CodeInvalidParams},
		{"terminal.snapshot", map[string]any{"id": "nope"}, wire.CodeNotFound},
		{"terminal.wait_for", map[string]any{"id": "s1", "timeout_ms": 100}, wire.CodeInvalidParams},
		{"terminal.wait_for", map[string]any{"id": "s1", "regex": "(", "timeout_ms": 100}, wire.CodeInvalidParams},
		{"terminal.wait_for", map[string]any{"id": "s1", "regex": "x"}, wire.CodeInvalidParams},
		{"terminal.wait_for", map[string]any{"id": "s1", "command_done": true, "timeout_ms": 100}, wire.CodeUnsupported},
	} {
		if we := f.callErr(c.method, c.params); we == nil || we.Code != c.code {
			t.Errorf("%s %v: %+v, want code %d", c.method, c.params, we, c.code)
		}
	}
}
