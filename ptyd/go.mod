module github.com/ascorblack/daedalus/ptyd

// Go 1.26: the screen emulator this daemon embeds is built with it, and the two must not drift.
go 1.26.0

// The one dependency: allocating a PTY and making it the child's controlling terminal, pinned
// exactly.
require github.com/creack/pty v1.1.24
