//go:build !unix

package sidechan

import (
	"os"
	"os/exec"
)

// On Windows the program is ended alone; its job object belongs to the Windows port.
func setGroup(cmd *exec.Cmd) {}

func signalGroup(cmd *exec.Cmd, kill bool) {
	if cmd.Process != nil {
		_ = cmd.Process.Kill()
	}
}

func signalOf(ps *os.ProcessState) string { return "" }
