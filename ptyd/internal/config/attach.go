package config

import "time"

// The defaults of attachments: how output reaches a browser, how fast, and when a client is given a
// fresh screen instead of the bytes it missed.
const (
	// OutputBatchBytes and OutputBatchDelay shape OUTPUT frames. A frame goes out as soon as 64 KiB
	// are waiting, or once 8 ms have passed since the previous frame. A keystroke's echo after a quiet
	// spell goes out at once; a flood is sent in large frames rather than one per read.
	OutputBatchBytes = 64 << 10
	OutputBatchDelay = 8 * time.Millisecond

	// ResyncBacklogBytes is the backlog past which a client that fell behind gets a snapshot instead
	// of the bytes it missed. Replaying a megabyte of a build log costs the browser far more than one
	// screen, and ends on the same screen.
	ResyncBacklogBytes = 1 << 20

	// DefaultSnapshotScrollback and MaxSnapshotScrollback are the scrollback lines a snapshot carries
	// when the client does not say, and the most it may ask for. A snapshot that does not fit in one
	// frame is taken again with half the scrollback until it does.
	DefaultSnapshotScrollback = 2000
	MaxSnapshotScrollback     = ScrollbackLines

	// PingInterval is how often an attached client hears from the daemon when nothing else happens.
	// The browser treats silence longer than this as a dead connection.
	PingInterval = 20 * time.Second

	// MaxClients bounds the attachments of one terminal. Each costs a goroutine and a few small
	// queues; the bound keeps a looping client from costing the daemon without limit.
	MaxClients = 32

	// ReplyEchoWindow is how long a client's INPUT that repeats, byte for byte, the daemon's own
	// answer to a query the client was shown is taken for the client's answer and dropped. The daemon
	// answers every query; a browser running an app bundle from before it learned to stay silent
	// would otherwise answer a second time, and the program would read the duplicate as typing.
	ReplyEchoWindow = 2 * time.Second

	// AgentTypingQuiet is how long after an agent's last delivered write attached clients are told
	// that the agent stopped typing.
	AgentTypingQuiet = 1500 * time.Millisecond

	// MaxKeyboardTTL bounds a keyboard grant with an expiry. A grant without one lasts until changed.
	MaxKeyboardTTL = 24 * time.Hour
)
