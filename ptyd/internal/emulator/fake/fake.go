// Package fake is the test double of the emulator: it records what it was fed and answers with
// whatever the test scripted.
package fake

import (
	"sync"

	"github.com/ascorblack/daedalus/ptyd/internal/emulator"
)

// Emulator records feeds and resizes. Its answers are fields the test sets. Unlike a real emulator
// it is safe for concurrent use, so a test can script it while the terminal's goroutine uses it.
type Emulator struct {
	mu       sync.Mutex
	fed      []byte
	cols     int
	rows     int
	cursor   emulator.Cursor
	modes    emulator.Modes
	snapshot []byte
	text     []string
	closed   bool
}

// New returns a fake of the given size.
func New(o emulator.Options) *Emulator {
	return &Emulator{cols: o.Cols, rows: o.Rows, cursor: emulator.Cursor{Visible: true}}
}

// Factory is an emulator.Factory that hands every new fake to created, when it is not nil, so a
// test can script the emulator of a terminal it is about to start.
func Factory(created func(*Emulator)) emulator.Factory {
	return func(o emulator.Options) emulator.Emulator {
		e := New(o)
		if created != nil {
			created(e)
		}
		return e
	}
}

func (e *Emulator) Feed(p []byte) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.fed = append(e.fed, p...)
}

func (e *Emulator) Resize(cols, rows int) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.cols, e.rows = cols, rows
}

func (e *Emulator) Size() (int, int) {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.cols, e.rows
}

func (e *Emulator) Cursor() emulator.Cursor {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.cursor
}

func (e *Emulator) Modes() emulator.Modes {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.modes
}

func (e *Emulator) History() (int, int64) { return 0, 0 }

func (e *Emulator) Snapshot(emulator.SnapshotOptions) ([]byte, emulator.SnapshotInfo) {
	e.mu.Lock()
	defer e.mu.Unlock()
	return append([]byte(nil), e.snapshot...), emulator.SnapshotInfo{Cols: e.cols, Rows: e.rows}
}

func (e *Emulator) Text(from, to int64) []string {
	e.mu.Lock()
	defer e.mu.Unlock()
	var out []string
	for r := from; r < to && r >= 0 && r < int64(len(e.text)); r++ {
		out = append(out, e.text[r])
	}
	return out
}

func (e *Emulator) Runs(from, to int64) [][]emulator.Run { return nil }

func (e *Emulator) Close() {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.closed = true
}

// Fed returns a copy of everything fed so far.
func (e *Emulator) Fed() []byte {
	e.mu.Lock()
	defer e.mu.Unlock()
	return append([]byte(nil), e.fed...)
}

// Closed reports whether Close was called.
func (e *Emulator) Closed() bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.closed
}

// SetModes scripts the modes.
func (e *Emulator) SetModes(m emulator.Modes) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.modes = m
}

// SetCursor scripts the cursor.
func (e *Emulator) SetCursor(c emulator.Cursor) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.cursor = c
}

// SetSnapshot scripts the snapshot bytes.
func (e *Emulator) SetSnapshot(vt []byte) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.snapshot = append([]byte(nil), vt...)
}

// SetText scripts the rows Text returns, indexed by absolute row.
func (e *Emulator) SetText(rows []string) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.text = append([]string(nil), rows...)
}
