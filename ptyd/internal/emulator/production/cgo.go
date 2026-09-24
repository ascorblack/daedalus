//go:build cgo

package production

import (
	"github.com/ascorblack/daedalus/ptyd/internal/emulator"
	"github.com/ascorblack/daedalus/ptyd/internal/emulator/ghostty"
)

// Factory and Name are the screen emulator.
var (
	Factory emulator.Factory = ghostty.Factory
	Name                     = ghostty.Name
)

// HasScreen reports whether snapshots, text and the screen queries are available.
const HasScreen = true
