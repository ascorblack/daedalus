module github.com/ascorblack/daedalus/ptyd

// Go 1.26: the screen emulator this daemon embeds is built with it, and the two must not drift.
go 1.26.0

require (
	// Allocating a PTY and making it the child's controlling terminal.
	github.com/creack/pty v1.1.24
	// The Go bindings of libghostty-vt, the screen emulator, pinned to one commit. The library
	// itself is built from the commit in libghostty/pins.env; the two are bumped together.
	go.mitchellh.com/libghostty v0.0.0-20260920220152-31b65cdc24cf
	// Windows: the pseudoconsole, job objects and access lists, which the standard library leaves out.
	golang.org/x/sys v0.48.0
)
