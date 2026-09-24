package fake

import (
	"testing"

	"github.com/ascorblack/daedalus/ptyd/internal/emulator/conformance"
)

func TestContract(t *testing.T) { conformance.RunContract(t, Factory(nil)) }
