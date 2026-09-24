package config

import "time"

// Every default of the daemon, named once. The host reads the effective values back from
// `daemon.info`, so a client never has to hard-code one of these.
const (
	// DefaultRingBytes is the output kept per terminal for reattachment and for agents' reads. Eight
	// megabytes is several thousand screens of ordinary output, and the buffer grows to it only as
	// output arrives, so an idle shell costs a few kilobytes.
	DefaultRingBytes = 8 << 20
	MinRingBytes     = 1 << 20
	MaxRingBytes     = 64 << 20

	// ExitedRingBytes is what is left of the ring once a terminal has exited: enough to read the end
	// of a finished command's output, without holding eight megabytes for an hour per dead terminal.
	ExitedRingBytes = 64 << 10

	// DefaultMaxTerminals bounds the running terminals of one daemon. The host enforces its own,
	// smaller, operator-facing cap; this one exists so that a runaway client cannot exhaust the
	// machine's pseudo-terminals.
	DefaultMaxTerminals = 128

	// The flow-control window of an attached client: acknowledge every AckBytes, stop sending past
	// WindowBytes unacknowledged.
	DefaultAckBytes    = 64 << 10
	DefaultWindowBytes = 256 << 10

	// The smallest and largest terminal sizes accepted. The minimum refuses the sizes a hidden browser
	// pane measures itself at (9x5 was the classic), which would otherwise be applied to a live
	// program. The maximum bounds the grid an emulator allocates: every cell costs memory, and
	// XTWINOPS or a client could otherwise ask for 9999x9999.
	MinCols = 20
	MinRows = 4
	MaxCols = 500
	MaxRows = 300

	DefaultCols = 80
	DefaultRows = 24

	// ScrollbackLines is the history each terminal's emulator keeps. Lines, not bytes: the emulator's
	// memory per terminal is bounded by this times the width, whatever the output.
	ScrollbackLines = 10000
	// ScrollbackBytes is the memory the history may take whatever its lines hold: 10 000 lines of a
	// 200-column terminal take about 15 MB, and of a 500-column one full of distinct styles several
	// times that. The emulator's own default is 10 000 bytes, a page or two, so it is always set.
	ScrollbackBytes = 64 << 20

	// DefaultInputIdle is how long an agent's write waits after the last human keystroke.
	DefaultInputIdle = 10 * time.Second

	// DefaultKillGrace is the time between SIGHUP and SIGKILL when a terminal is ended.
	DefaultKillGrace = 2 * time.Second
	MaxKillGrace     = 30 * time.Second

	// ExitedKeep is how long an exited terminal stays listed with its screen before it is forgotten.
	ExitedKeep = time.Hour

	// EventRingSize is how many events are kept for a subscriber that reconnects. At a few events a
	// minute per terminal this is hours of history, which covers any restart of the host.
	EventRingSize = 20000

	// StatsInterval is how often process-tree statistics are sampled and published. The load bar that
	// reads them is an estimate; sampling faster would cost more than it tells.
	StatsInterval = 10 * time.Second

	// MaxWriteBytes bounds one agent write. Prompts longer than this go to a file.
	MaxWriteBytes = 1 << 20

	// MaxReadOutputBytes bounds one read_output reply, which travels as one frame.
	MaxReadOutputBytes = 1 << 20

	// MaxSnapshotBytes is the largest snapshot sent in one frame (a mebibyte, less room for the
	// frame's own header and, in a reply, base64). A larger one is cut down by dropping history.
	MaxSnapshotBytes = 700 << 10
	// MaxRunsRows bounds read_screen in runs: every cell is read one by one.
	MaxRunsRows = 1000
	// MaxWaitTimeout bounds terminal.wait_for.
	MaxWaitTimeout = 30 * time.Minute

	// MaxWriteTimeout bounds how long an agent write may wait for the keyboard.
	MaxWriteTimeout = 10 * time.Minute

	// LogToDiskBytes is where a terminal's on-disk output log rotates; one older file is kept.
	LogToDiskBytes = 32 << 20

	// AgentJournalBytes and AgentJournalFiles rotate the journal of agent writes: the current file and
	// two older ones.
	AgentJournalBytes = 16 << 20
	AgentJournalFiles = 3
)
