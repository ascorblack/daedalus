package basic

import (
	"testing"

	"github.com/ascorblack/daedalus/ptyd/internal/emulator"
	"github.com/ascorblack/daedalus/ptyd/internal/emulator/conformance"
)

func TestModesFollowTheStream(t *testing.T) {
	e := New(emulator.Options{Cols: 80, Rows: 24})
	if m := e.Modes(); !m.Wrap || m.AltScreen || m.BracketedPaste {
		t.Fatalf("initial %+v", m)
	}
	// Split mid-sequence on purpose: the scanner inside holds the halves together.
	e.Feed([]byte("\x1b[?1049;2004h\x1b[?1h\x1b="))
	e.Feed([]byte("\x1b[?100"))
	e.Feed([]byte("2h\x1b[?1006h\x1b[>5u\x1b[>9u"))
	m := e.Modes()
	if !m.AltScreen || !m.BracketedPaste || !m.AppCursor || !m.AppKeypad || m.Mouse.Mode != 1002 ||
		m.Mouse.Encoding != 1006 || m.KittyFlags != 9 {
		t.Fatalf("after setting: %+v", m)
	}
	e.Feed([]byte("\x1b[<u"))
	if e.Modes().KittyFlags != 5 {
		t.Fatalf("pop: %d", e.Modes().KittyFlags)
	}
	e.Feed([]byte("\x1b[<5u"))
	if e.Modes().KittyFlags != 0 {
		t.Fatalf("pop past the bottom: %d", e.Modes().KittyFlags)
	}
	e.Feed([]byte("\x1b[=3;1u\x1b[>1u\x1b[<u"))
	if e.Modes().KittyFlags != 3 {
		t.Fatalf("pop to a set value: %d", e.Modes().KittyFlags)
	}
	e.Feed([]byte("\x1b[!p"))
	if m := e.Modes(); m.AppCursor || !m.AltScreen {
		t.Fatalf("soft reset: %+v", m)
	}
	e.Feed([]byte("\x1bc"))
	if m := e.Modes(); m.AltScreen || m.BracketedPaste || m.Mouse.Mode != 0 || m.KittyFlags != 0 {
		t.Fatalf("hard reset: %+v", m)
	}
}

func TestContract(t *testing.T) { conformance.RunContract(t, Factory) }
