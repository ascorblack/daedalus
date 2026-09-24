//go:build unix

package sidechan

import (
	"os"
	"os/exec"
	"syscall"

	"github.com/ascorblack/daedalus/ptyd/internal/ptyproc"
)

// setGroup starts the program as the leader of a new process group, so the whole group can be ended
// together, and never with a controlling terminal of the daemon's.
func setGroup(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
}

// signalGroup sends the hangup (or, with kill, SIGKILL) to the program's process group.
func signalGroup(cmd *exec.Cmd, kill bool) {
	if cmd.Process == nil {
		return
	}
	sig := syscall.SIGHUP
	if kill {
		sig = syscall.SIGKILL
	}
	// The group id is the leader's pid. A leader that already exited leaves the group behind it,
	// which is exactly what still has to be ended.
	_ = syscall.Kill(-cmd.Process.Pid, sig)
}

// signalOf names the signal that ended the program, as terminals' exits name theirs.
func signalOf(ps *os.ProcessState) string {
	if ws, ok := ps.Sys().(syscall.WaitStatus); ok && ws.Signaled() {
		return ptyproc.SignalName(ws.Signal())
	}
	return ""
}
