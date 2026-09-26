module github.com/ascorblack/daedalus/browserd

// The Go of ptyd, whose shared packages this module builds against.
go 1.26.0

// The socket framing, the run directory and handshake, the event log and the process table,
// shared with the terminal daemon so the host speaks one protocol to both.
require github.com/ascorblack/daedalus/ptyd v0.0.0

require golang.org/x/sys v0.48.0 // indirect

replace github.com/ascorblack/daedalus/ptyd => ../ptyd
