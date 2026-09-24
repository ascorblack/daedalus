// Package emulator is the headless terminal the daemon keeps for every terminal, behind one
// interface so that the implementation can change without touching the rest of the daemon.
//
// An Emulator is not safe for concurrent use. The terminal owns it from one goroutine, which feeds
// it, resizes it and asks it questions in order; that is also what makes a snapshot consistent with
// the output offset it is taken at.
package emulator

// Emulator is a headless terminal: bytes in, a screen out.
type Emulator interface {
	// Feed consumes output from the PTY. It never blocks and never fails: whatever the bytes are,
	// the screen afterwards is some screen.
	Feed(p []byte)
	Resize(cols, rows int)
	Size() (cols, rows int)
	Cursor() Cursor
	Modes() Modes
	// History reports the scrollback lines held and the absolute row number of the oldest one.
	// Absolute rows count every line since the terminal started, so a row keeps its number as older
	// ones fall off the scrollback.
	History() (lines int, firstAbsRow int64)
	// Snapshot returns VT bytes that reproduce the scrollback (o.Scrollback lines of it), both
	// screens, the cursor, the pen, the modes, the scroll region and the character sets when written
	// into a freshly reset terminal of the same size.
	Snapshot(o SnapshotOptions) ([]byte, SnapshotInfo)
	// Text returns the rows from..to (absolute, half-open) as text, soft-wrapped rows joined.
	Text(from, to int64) []string
	// Runs returns the rows from..to (absolute, half-open) as styled runs, for previews.
	Runs(from, to int64) [][]Run
	Close()
}

// Options are what a new emulator is created with.
type Options struct {
	Cols, Rows int
	// ScrollbackLines bounds the history. Lines, not bytes: a byte cap silently holds a few hundred
	// lines of coloured output.
	ScrollbackLines int
	// GraphemeClusters starts the emulator with grapheme clustering on (mode 2027), which is how the
	// browser's width provider measures text. With it off, the two would disagree about the width of
	// every emoji sequence and every combining cluster, and a snapshot would misplace the rest of the
	// row. The browser cannot follow an application that turns the mode off again; none of the
	// programs this is for does.
	GraphemeClusters bool
}

// Factory creates an emulator. The daemon's production factory is chosen at build time.
type Factory func(o Options) Emulator

// Cursor is the cursor's position on the visible screen, and its absolute row.
type Cursor struct {
	X, Y    int
	Visible bool
	AbsRow  int64 // history lines + Y
}

// Mouse is the mouse reporting the application asked for.
type Mouse struct {
	Mode     int // 0 off, else 9, 1000, 1002 or 1003
	Encoding int // 0 default, else 1005, 1006 or 1015
}

// Modes are the terminal modes that change what the daemon sends or reports.
type Modes struct {
	AltScreen      bool
	BracketedPaste bool
	AppCursor      bool // DECCKM: arrows send ESC O x
	AppKeypad      bool // DECKPAM
	Mouse          Mouse
	FocusEvents    bool
	KittyFlags     int
	CursorStyle    int // DECSCUSR
	Origin         bool
	Wrap           bool
	Insert         bool
	SyncOutput     bool // mode 2026
}

// SnapshotOptions select what a snapshot carries.
type SnapshotOptions struct {
	Scrollback int // lines of history to include
}

// SnapshotInfo describes a snapshot's content.
type SnapshotInfo struct {
	Cols, Rows  int
	FirstAbsRow int64 // absolute row of the snapshot's first line
}

// Run is a stretch of a row with one style. A colour of 0 is the default colour, so that the common
// case costs nothing in JSON; 1..256 are palette entries 0..255 plus one; 0x1000000 + RGB is true
// colour.
type Run struct {
	T   string `json:"t"`
	FG  int    `json:"fg,omitempty"`
	BG  int    `json:"bg,omitempty"`
	B   bool   `json:"b,omitempty"`
	I   bool   `json:"i,omitempty"`
	U   bool   `json:"u,omitempty"`
	D   bool   `json:"d,omitempty"`
	Inv bool   `json:"inv,omitempty"`
}
