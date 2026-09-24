package term

import (
	"github.com/ascorblack/daedalus/ptyd/internal/answer"
	"github.com/ascorblack/daedalus/ptyd/internal/emulator"
	"github.com/ascorblack/daedalus/ptyd/internal/wire"
)

// Screen is what `terminal.read_screen` reports, taken at one output offset.
type Screen struct {
	Cols, Rows  int
	Cursor      emulator.Cursor
	AltScreen   bool
	Seq         int64 // the output offset the screen shows everything up to
	FirstAbsRow int64 // the absolute row of the first line
	Lines       []string
	Runs        [][]emulator.Run
	VT          []byte
}

// ScreenFormat selects what ReadScreen fills.
type ScreenFormat int

const (
	ScreenText ScreenFormat = iota
	ScreenRuns
	ScreenVT
)

// Snapshot returns VT bytes that rebuild the screen with at most scrollback lines of history, and
// the output offset the stream continues from. The snapshot is shrunk, by dropping history, until it
// fits maxBytes (0 for no limit), because a frame carries at most a mebibyte.
func (t *Terminal) Snapshot(scrollback, maxBytes int) (data []byte, info emulator.SnapshotInfo, seq int64, err error) {
	err = t.WithEmulator(func(e emulator.Emulator) {
		data, info = fitSnapshot(e, scrollback, maxBytes)
		seq = t.fed.Load()
	})
	return data, info, seq, err
}

func fitSnapshot(e emulator.Emulator, scrollback, maxBytes int) ([]byte, emulator.SnapshotInfo) {
	for {
		data, info := e.Snapshot(emulator.SnapshotOptions{Scrollback: scrollback})
		if maxBytes <= 0 || len(data) <= maxBytes || scrollback == 0 {
			return data, info
		}
		// Scale the history by how far over the limit it is, and at least halve it, so that a huge
		// history converges in a few formats instead of many.
		scrollback = min(scrollback/2, int(int64(scrollback)*int64(maxBytes)/int64(len(data))))
	}
}

// ReadScreen returns the visible screen with the given lines of history above it, or only its last
// tailRows lines when tailRows is positive.
func (t *Terminal) ReadScreen(format ScreenFormat, scrollback, tailRows, maxBytes int) (Screen, error) {
	var s Screen
	err := t.WithEmulator(func(e emulator.Emulator) {
		s.Cols, s.Rows = e.Size()
		s.Cursor = e.Cursor()
		s.AltScreen = e.Modes().AltScreen
		s.Seq = t.fed.Load()
		end := s.Cursor.AbsRow - int64(s.Cursor.Y) + int64(s.Rows)
		from := end - int64(s.Rows) - int64(scrollback)
		if tailRows > 0 {
			from = max(from, end-int64(tailRows))
		}
		_, first := e.History()
		if s.AltScreen {
			// The alternate screen has no history, and the primary's is not readable under it.
			first = end - int64(s.Rows)
		}
		from = max(from, first)
		s.FirstAbsRow = from
		switch format {
		case ScreenText:
			s.Lines = e.Text(from, end)
		case ScreenRuns:
			s.Runs = e.Runs(from, end)
		case ScreenVT:
			s.VT, _ = fitSnapshot(e, scrollback, maxBytes)
		}
	})
	return s, err
}

// Preview is the last rows of the screen as styled runs, for a list of terminals: under a shell the
// rows ending at the cursor, where the prompt is, and on the alternate screen its bottom rows.
func (t *Terminal) Preview(rows int) [][]emulator.Run {
	var out [][]emulator.Run
	_ = t.WithEmulator(func(e emulator.Emulator) {
		c := e.Cursor()
		_, screenRows := e.Size()
		end := c.AbsRow + 1
		top := c.AbsRow - int64(c.Y)
		if e.Modes().AltScreen {
			end = top + int64(screenRows)
		}
		out = e.Runs(max(end-int64(rows), top), end)
	})
	return out
}

// TitleCwd returns the title and working directory the program last reported.
func (t *Terminal) TitleCwd() (title, cwd string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.title, t.cwd
}

// applyTheme hands the viewer's colours to the emulator when they changed, so that colour queries
// are answered with what the person sees (unless the program set its own). Without a viewer the
// last one's colours stay: they are the best guess of who will look next. Runs on the emulator
// goroutine.
func (t *Terminal) applyTheme(e emulator.Emulator, th *wire.Theme) {
	if th == nil || *th == t.theme {
		return
	}
	q, ok := e.(emulator.Querier)
	if !ok {
		return
	}
	t.theme = *th
	q.SetTheme(answer.Theme(th.FG, th.BG, th.Cursor))
}
