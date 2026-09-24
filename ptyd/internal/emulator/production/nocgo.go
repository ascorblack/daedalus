//go:build !cgo

package production

import (
	"github.com/ascorblack/daedalus/ptyd/internal/emulator"
	"github.com/ascorblack/daedalus/ptyd/internal/emulator/basic"
)

// Without cgo there is no screen emulator: the modes-only one keeps writes correct, and
// `daemon.info` names it, so a host can tell such a build from the real one.
var (
	Factory emulator.Factory = basic.Factory
	Name                     = basic.Name
)

// HasScreen reports whether snapshots, text and the screen queries are available.
const HasScreen = false
