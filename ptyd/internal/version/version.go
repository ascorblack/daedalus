// Package version names the build and the wire protocol it speaks.
package version

// Version is the build's own version. Release builds set it with
// `-ldflags "-X github.com/ascorblack/daedalus/ptyd/internal/version.Version=<v>"`; a plain
// `go build` reports "dev", which the host shows as it is rather than guessing.
var Version = "dev"

// Protocol is the version of the socket protocol. It changes only when an existing method, frame or
// event changes meaning; additions keep it. The host refuses a protocol it does not know, because a
// half-understood terminal service is worse than one reported as unavailable.
const Protocol = 1
