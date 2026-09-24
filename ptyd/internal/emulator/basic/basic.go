// Package basic is the emulator a build without a screen emulator runs with: it keeps no screen,
// only the size and the modes, which it reads from the stream with the daemon's own scanner.
//
// The modes are what the daemon cannot work without even before it has a screen: a paste must be
// bracketed when the application turned bracketed paste on, and an arrow key must be sent in the
// form DECCKM selects. Everything that needs a grid (snapshots, text, the cursor) answers empty.
package basic

import (
	"github.com/ascorblack/daedalus/ptyd/internal/emulator"
	"github.com/ascorblack/daedalus/ptyd/internal/scan"
)

// Name is reported in `daemon.info` as the emulator in use.
const Name = "basic@1"

// maxKittyStack matches the depth xterm.js and Ghostty keep; a push beyond it drops the oldest.
const maxKittyStack = 16

type Emulator struct {
	cols, rows int
	sc         *scan.Scanner
	modes      emulator.Modes
	kitty      []int // the stack below the current flags
}

// New returns a basic emulator.
func New(o emulator.Options) emulator.Emulator {
	e := &Emulator{cols: o.Cols, rows: o.Rows, sc: scan.New()}
	e.reset()
	return e
}

// Factory is New as an emulator.Factory.
var Factory emulator.Factory = New

func (e *Emulator) reset() {
	e.modes = emulator.Modes{Wrap: true}
	e.kitty = e.kitty[:0]
}

func (e *Emulator) Feed(p []byte) {
	_, marks := e.sc.Scan(p)
	for _, m := range marks {
		switch m.Kind {
		case scan.KindMode:
			e.mode(m.Mode, m.Set)
		case scan.KindReset:
			if m.Letter == 'c' {
				e.reset()
			} else {
				// DECSTR resets the input modes and keeps the screen and the kitty stack.
				e.modes.AppCursor, e.modes.AppKeypad, e.modes.Insert, e.modes.Origin = false, false, false, false
				e.modes.Wrap = true
			}
		case scan.KindKitty:
			e.kittyOp(m)
		}
	}
}

func (e *Emulator) mode(n int, set bool) {
	m := &e.modes
	switch n {
	case 1:
		m.AppCursor = set
	case 6:
		m.Origin = set
	case 7:
		m.Wrap = set
	case 66:
		m.AppKeypad = set
	case 47, 1047, 1049:
		m.AltScreen = set
	case 2004:
		m.BracketedPaste = set
	case 9, 1000, 1002, 1003:
		if set {
			m.Mouse.Mode = n
		} else if m.Mouse.Mode == n {
			m.Mouse.Mode = 0
		}
	case 1005, 1006, 1015:
		if set {
			m.Mouse.Encoding = n
		} else if m.Mouse.Encoding == n {
			m.Mouse.Encoding = 0
		}
	case 1004:
		m.FocusEvents = set
	case 2026:
		m.SyncOutput = set
	}
}

func (e *Emulator) kittyOp(m scan.Mark) {
	switch m.Op {
	case '>':
		if len(e.kitty) == maxKittyStack {
			e.kitty = e.kitty[1:]
		}
		e.kitty = append(e.kitty, e.modes.KittyFlags)
		e.modes.KittyFlags = m.Flags
	case '<':
		n := max(1, m.Value)
		if n > len(e.kitty) {
			// Popping past the bottom of the stack resets to no flags.
			e.kitty = e.kitty[:0]
			e.modes.KittyFlags = 0
			return
		}
		e.modes.KittyFlags = e.kitty[len(e.kitty)-n]
		e.kitty = e.kitty[:len(e.kitty)-n]
	case '=':
		switch m.Value {
		case 2:
			e.modes.KittyFlags |= m.Flags
		case 3:
			e.modes.KittyFlags &^= m.Flags
		default:
			e.modes.KittyFlags = m.Flags
		}
	}
}

func (e *Emulator) Resize(cols, rows int)                { e.cols, e.rows = cols, rows }
func (e *Emulator) Size() (int, int)                     { return e.cols, e.rows }
func (e *Emulator) Cursor() emulator.Cursor              { return emulator.Cursor{Visible: true} }
func (e *Emulator) Modes() emulator.Modes                { return e.modes }
func (e *Emulator) History() (int, int64)                { return 0, 0 }
func (e *Emulator) Text(from, to int64) []string         { return nil }
func (e *Emulator) Runs(from, to int64) [][]emulator.Run { return nil }
func (e *Emulator) Close()                               {}
func (e *Emulator) Snapshot(emulator.SnapshotOptions) ([]byte, emulator.SnapshotInfo) {
	return nil, emulator.SnapshotInfo{Cols: e.cols, Rows: e.rows}
}
