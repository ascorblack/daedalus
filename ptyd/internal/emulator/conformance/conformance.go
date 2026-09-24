// Package conformance is the behaviour every screen emulator behind the daemon's interface must
// have, as tests any implementation can be run against: the screen a stream leaves, the cursor, the
// modes, and the snapshot round trip (a snapshot fed into a fresh emulator rebuilds the same screen).
//
// The cases are written down here once and kept as data in testdata/cases.json too, so that the
// browser side can check xterm.js against the same streams; `go test -update` rewrites the data and
// the golden snapshots from the code.
package conformance

import (
	"reflect"
	"strings"
	"testing"

	"github.com/ascorblack/daedalus/ptyd/internal/emulator"
)

// Case is a stream and the state it must leave.
type Case struct {
	Name        string           `json:"name"`
	Cols        int              `json:"cols"`
	Rows        int              `json:"rows"`
	Writes      []string         `json:"writes"`
	ResizeTo    *[2]int          `json:"resize_to,omitempty"` // applied after the writes
	Text        []string         `json:"text"`                // every row, history first, as Text returns it
	Cursor      *[2]int          `json:"cursor,omitempty"`    // x, y on the screen
	Modes       map[string]any   `json:"modes,omitempty"`     // the modes the case is about
	Runs        [][]emulator.Run `json:"runs,omitempty"`      // the screen's first rows as runs
	History     *int             `json:"history,omitempty"`   // history lines held
	NoRoundTrip string           `json:"no_round_trip,omitempty"`
}

const (
	esc = "\x1b"
	csi = "\x1b["
)

func ip(n int) *int { return &n }

func xy(x, y int) *[2]int { return &[2]int{x, y} }

// Cases are the streams. Each says only what it is about; the round trip checks the rest.
var Cases = []Case{
	{Name: "wrapping", Cols: 10, Rows: 5, Writes: []string{"abcdefghijKLM"},
		Text: []string{"abcdefghijKLM"}, Cursor: xy(3, 1)},
	{Name: "pending wrap stays on the last column", Cols: 10, Rows: 5, Writes: []string{"0123456789"},
		Text: []string{"0123456789"}, Cursor: xy(9, 0)},
	{Name: "pending wrap then a character", Cols: 10, Rows: 5, Writes: []string{"0123456789A"},
		Text: []string{"0123456789A"}, Cursor: xy(1, 1)},
	{Name: "carriage return and line feed", Cols: 20, Rows: 5, Writes: []string{"a\r\nb\rc\n"},
		Text: []string{"a", "c"}, Cursor: xy(1, 2)},
	{Name: "cursor movement", Cols: 20, Rows: 6,
		Writes: []string{csi + "5;10Hx" + csi + "2Ay" + csi + "3Dz" + csi + "Bw" + csi + "Ge"},
		Text:   []string{"", "", "        z y", "e        w", "         x"}, Cursor: xy(1, 3)},
	{Name: "graphic rendition", Cols: 20, Rows: 3,
		Writes: []string{csi + "1;31mR" + csi + "0;4;38;2;1;2;3mU" + csi + "7m " + csi + "0m"},
		Text:   []string{"RU"},
		Runs: [][]emulator.Run{{
			{T: "R", FG: 2, B: true}, {T: "U", FG: 0x1010203, U: true}, {T: " ", FG: 0x1010203, U: true, Inv: true},
		}}},
	{Name: "256 colours and background erase", Cols: 6, Rows: 2,
		Writes: []string{csi + "38;5;200m" + csi + "48;5;17mab" + csi + "K" + csi + "0m"},
		Text:   []string{"ab"},
		Runs:   [][]emulator.Run{{{T: "ab", FG: 201, BG: 18}, {T: "    ", BG: 18}}}},
	{Name: "alternate screen, inside", Cols: 20, Rows: 5, Writes: []string{"main" + csi + "?1049halt"},
		Text: []string{"    alt"}, Cursor: xy(7, 0), Modes: map[string]any{"AltScreen": true}},
	{Name: "alternate screen, left again", Cols: 20, Rows: 5, Writes: []string{"main" + csi + "?1049halt" + csi + "?1049l"},
		Text: []string{"main"}, Cursor: xy(4, 0), Modes: map[string]any{"AltScreen": false}},
	{Name: "scroll region", Cols: 10, Rows: 5,
		Writes: []string{"1\r\n2\r\n3\r\n4\r\n5" + csi + "2;4r" + csi + "4;1H\n"},
		Text:   []string{"1", "3", "4", "", "5"}, Cursor: xy(0, 3), History: ip(0)},
	// Lines scrolled off a region that starts at the top go into the history, as in xterm (Codex
	// keeps its conversation this way). xterm.js drops them.
	{Name: "scroll region at the top feeds the history", Cols: 10, Rows: 4,
		Writes: []string{"a\r\nb\r\nc\r\nd" + csi + "1;3r" + csi + "3;1H\n\n"},
		Text:   []string{"a", "b", "c", "", "", "d"}, History: ip(2)},
	{Name: "insert and delete characters", Cols: 20, Rows: 3,
		Writes: []string{"abcdef" + csi + "1;3H" + csi + "2@XY" + csi + "1;1H" + csi + "P"},
		Text:   []string{"bXYcdef"}, Cursor: xy(0, 0)},
	{Name: "insert and delete lines", Cols: 10, Rows: 5,
		Writes: []string{"1\r\n2\r\n3\r\n4\r\n5" + csi + "2;1H" + csi + "L" + csi + "4;1H" + csi + "2M"},
		Text:   []string{"1", "", "2"}, Cursor: xy(0, 3)},
	{Name: "erase in line", Cols: 20, Rows: 3, Writes: []string{"abcdef" + csi + "1;4H" + csi + "K\r\nxyz" + csi + "1K"},
		Text: []string{"abc"}, Cursor: xy(3, 1)},
	{Name: "erase in display", Cols: 20, Rows: 4, Writes: []string{"a\r\nb\r\nc" + csi + "2;1H" + csi + "J"},
		Text: []string{"a"}, Cursor: xy(0, 1)},
	{Name: "tab stops", Cols: 20, Rows: 3, Writes: []string{"a\tb" + csi + "5G" + esc + "Hc\r\t\tX"},
		Text: []string{"a   c   X"}, Cursor: xy(9, 0)},
	{Name: "saved cursor", Cols: 10, Rows: 4, Writes: []string{csi + "3;3H" + esc + "7" + csi + "1;1Hx" + esc + "8y"},
		Text: []string{"x", "", "  y"}, Cursor: xy(3, 2)},
	{Name: "origin mode", Cols: 10, Rows: 5, Writes: []string{csi + "2;4r" + csi + "?6h" + csi + "1;1Hx"},
		Text: []string{"", "x"}, Cursor: xy(1, 1), Modes: map[string]any{"Origin": true}},
	{Name: "line drawing character set", Cols: 10, Rows: 2, Writes: []string{esc + "(0qx" + esc + "(Bq"},
		Text: []string{"─│q"}},
	{Name: "grapheme clusters", Cols: 20, Rows: 2, Writes: []string{"👍🏻|🇺🇸|é|👩‍💻|"},
		Text: []string{"👍🏻|🇺🇸|é|👩‍💻|"}, Cursor: xy(11, 0)},
	{Name: "grapheme cluster split across writes", Cols: 20, Rows: 2, Writes: []string{"\xf0\x9f\x91", "\x8d\xf0\x9f", "\x8f\xbb|"},
		Text: []string{"👍🏻|"}, Cursor: xy(3, 0)},
	{Name: "wide character at the margin", Cols: 5, Rows: 3, Writes: []string{"abcd中"},
		Text: []string{"abcd中"}, Cursor: xy(2, 1)},
	{Name: "history", Cols: 10, Rows: 3, Writes: []string{"1\r\n2\r\n3\r\n4\r\n5\r\n6\r\n7\r\n8\r\n9\r\n10\r\n"},
		Text: []string{"1", "2", "3", "4", "5", "6", "7", "8", "9", "10"}, History: ip(8), Cursor: xy(0, 2)},
	{Name: "cursor-key mode", Cols: 10, Rows: 2, Writes: []string{csi + "?1h"}, Text: []string{},
		Modes: map[string]any{"AppCursor": true}},
	{Name: "bracketed paste and focus events", Cols: 10, Rows: 2, Writes: []string{csi + "?2004h" + csi + "?1004h"},
		Text: []string{}, Modes: map[string]any{"BracketedPaste": true, "FocusEvents": true}},
	{Name: "mouse tracking with SGR reports", Cols: 10, Rows: 2, Writes: []string{csi + "?1002h" + csi + "?1006h"},
		Text: []string{}, Modes: map[string]any{"Mouse": emulator.Mouse{Mode: 1002, Encoding: 1006}}},
	{Name: "mouse tracking turned off keeps nothing on", Cols: 10, Rows: 2,
		Writes: []string{csi + "?1000h" + csi + "?1003h" + csi + "?1006h" + csi + "?1003l"},
		Text:   []string{}, Modes: map[string]any{"Mouse": emulator.Mouse{}}},
	{Name: "kitty keyboard push and pop", Cols: 10, Rows: 2, Writes: []string{csi + ">1u" + csi + ">3u" + csi + "<u"},
		Text: []string{}, Modes: map[string]any{"KittyFlags": 1}},
	{Name: "cursor shape and visibility", Cols: 10, Rows: 2, Writes: []string{csi + "5 q" + csi + "?25l"},
		Text: []string{}, Modes: map[string]any{"CursorStyle": 5}},
	{Name: "autowrap and insert mode", Cols: 10, Rows: 2, Writes: []string{csi + "?7l" + csi + "4h"},
		Text: []string{}, Modes: map[string]any{"Wrap": false, "Insert": true}},
	{Name: "reflow when widened", Cols: 10, Rows: 5, Writes: []string{"abcdefghijklmno"}, ResizeTo: &[2]int{20, 5},
		Text: []string{"abcdefghijklmno"}, Cursor: xy(15, 0)},
	{Name: "reflow when narrowed", Cols: 10, Rows: 5, Writes: []string{"abcdefghijklmno\r\n$ "}, ResizeTo: &[2]int{5, 5},
		Text: []string{"abcdefghijklmno", "$"}},
	{Name: "full reset", Cols: 10, Rows: 3, Writes: []string{"abc" + csi + "?1h" + csi + "?2004h" + esc + "c"},
		Text: []string{}, Cursor: xy(0, 0), Modes: map[string]any{"AppCursor": false, "BracketedPaste": false}},
}

// Run checks an emulator with a screen against every case.
func Run(t *testing.T, factory emulator.Factory) {
	for _, c := range Cases {
		t.Run(c.Name, func(t *testing.T) {
			e := Feed(factory, c)
			defer e.Close()
			check(t, "", e, c)
			if c.NoRoundTrip != "" {
				t.Skip(c.NoRoundTrip)
			}
			RoundTrip(t, factory, e)
		})
	}
}

// Feed builds an emulator for a case and plays its stream into it.
func Feed(factory emulator.Factory, c Case) emulator.Emulator {
	e := factory(emulator.Options{Cols: c.Cols, Rows: c.Rows, ScrollbackLines: 1000, ScrollbackBytes: 16 << 20,
		GraphemeClusters: true})
	for _, w := range c.Writes {
		e.Feed([]byte(w))
	}
	if c.ResizeTo != nil {
		e.Resize(c.ResizeTo[0], c.ResizeTo[1])
	}
	return e
}

// AllText is every row the emulator holds, history first.
func AllText(e emulator.Emulator) []string {
	_, first := e.History()
	c := e.Cursor()
	_, rows := e.Size()
	end := c.AbsRow - int64(c.Y) + int64(rows)
	out := e.Text(first, end)
	if out == nil {
		out = []string{}
	}
	return out
}

func check(t *testing.T, label string, e emulator.Emulator, c Case) {
	t.Helper()
	if got := AllText(e); !reflect.DeepEqual(got, c.Text) {
		t.Errorf("%stext = %q, want %q", label, got, c.Text)
	}
	if c.Cursor != nil {
		if cur := e.Cursor(); cur.X != c.Cursor[0] || cur.Y != c.Cursor[1] {
			t.Errorf("%scursor = %d,%d, want %d,%d", label, cur.X, cur.Y, c.Cursor[0], c.Cursor[1])
		}
	}
	if c.History != nil {
		if n, _ := e.History(); n != *c.History {
			t.Errorf("%shistory = %d lines, want %d", label, n, *c.History)
		}
	}
	modes := reflect.ValueOf(e.Modes())
	for name, want := range c.Modes {
		got := modes.FieldByName(name)
		if !got.IsValid() {
			t.Fatalf("no mode %s", name)
		}
		w := reflect.ValueOf(want)
		if w.Kind() == reflect.Int && got.Kind() == reflect.Int {
			if got.Int() != w.Int() {
				t.Errorf("%smode %s = %v, want %v", label, name, got.Interface(), want)
			}
			continue
		}
		if !reflect.DeepEqual(got.Interface(), want) {
			t.Errorf("%smode %s = %v, want %v", label, name, got.Interface(), want)
		}
	}
	if c.Runs != nil {
		c0 := e.Cursor()
		top := c0.AbsRow - int64(c0.Y)
		if got := e.Runs(top, top+int64(len(c.Runs))); !reflect.DeepEqual(got, c.Runs) {
			t.Errorf("%sruns = %+v, want %+v", label, got, c.Runs)
		}
	}
}

// RoundTrip feeds e's snapshot into a fresh emulator of the same size and demands the same text,
// cursor, modes and styled screen.
func RoundTrip(t *testing.T, factory emulator.Factory, e emulator.Emulator) {
	t.Helper()
	cols, rows := e.Size()
	snap, info := e.Snapshot(emulator.SnapshotOptions{Scrollback: 1000})
	if info.Cols != cols || info.Rows != rows {
		t.Fatalf("snapshot says %dx%d, the emulator is %dx%d", info.Cols, info.Rows, cols, rows)
	}
	f := factory(emulator.Options{Cols: cols, Rows: rows, ScrollbackLines: 1000, ScrollbackBytes: 16 << 20,
		GraphemeClusters: true})
	defer f.Close()
	f.Feed(snap)
	if a, b := AllText(e), AllText(f); !reflect.DeepEqual(a, b) {
		t.Errorf("round trip text = %q, want %q\nsnapshot %q", b, a, snap)
	}
	if a, b := e.Cursor(), f.Cursor(); a.X != b.X || a.Y != b.Y || a.Visible != b.Visible {
		t.Errorf("round trip cursor = %+v, want %+v\nsnapshot %q", b, a, snap)
	}
	if a, b := e.Modes(), f.Modes(); a != b {
		t.Errorf("round trip modes = %+v, want %+v\nsnapshot %q", b, a, snap)
	}
	ea, fa := e.Cursor(), f.Cursor()
	et, ft := ea.AbsRow-int64(ea.Y), fa.AbsRow-int64(fa.Y)
	if a, b := e.Runs(et, et+int64(rows)), f.Runs(ft, ft+int64(rows)); !reflect.DeepEqual(a, b) {
		t.Errorf("round trip screen runs differ:\n got %+v\nwant %+v", b, a)
	}
	if n1, _ := e.History(); true {
		if n2, _ := f.History(); n1 != n2 {
			t.Errorf("round trip history = %d lines, want %d", n2, n1)
		}
	}
}

// RunContract checks what every emulator must do, screen or not: take any bytes without failing,
// follow resizes, and close.
func RunContract(t *testing.T, factory emulator.Factory) {
	e := factory(emulator.Options{Cols: 80, Rows: 24, ScrollbackLines: 100, ScrollbackBytes: 1 << 20, GraphemeClusters: true})
	defer e.Close()
	for _, c := range Cases {
		for _, w := range c.Writes {
			e.Feed([]byte(w))
		}
	}
	e.Feed([]byte(strings.Repeat("\x1b[\x1b]\x9b\xff\xc2", 100)))
	e.Resize(100, 30)
	if cols, rows := e.Size(); cols != 100 || rows != 30 {
		t.Fatalf("size after resize = %dx%d", cols, rows)
	}
	_, _ = e.Snapshot(emulator.SnapshotOptions{Scrollback: 10})
	_ = e.Text(0, 10)
	_ = e.Runs(0, 10)
	_ = e.Cursor()
	_ = e.Modes()
}
