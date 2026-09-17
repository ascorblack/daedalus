//go:build windows

package main

// Windows has no process groups in the POSIX sense and no signals to send them. A child started in
// its own process group can be told to end with a console control event, and `taskkill /T` is what
// walks the tree the supervisor and the bot make — the nearest thing the platform has to killing a
// session.

import (
	"os/exec"
	"strconv"
	"syscall"
)

func setProcessGroup(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: syscall.CREATE_NEW_PROCESS_GROUP}
}

// terminateGroup asks the tree to end politely, which on Windows is a CTRL_BREAK to the process
// group the child was started in. `taskkill` without /F was what this used to send, and it posts
// WM_CLOSE to top-level windows: a console-less Python has none, so nothing arrived, the forty
// second wait always expired and the bot was hard-killed with no chance to drain a run. A break
// event is what the supervisor's own signal handler is waiting for.
func terminateGroup(cmd *exec.Cmd) {
	if cmd.Process == nil {
		return
	}
	if err := generateConsoleCtrlEvent(syscall.CTRL_BREAK_EVENT, uint32(cmd.Process.Pid)); err != nil {
		_ = exec.Command("taskkill", "/T", "/PID", strconv.Itoa(cmd.Process.Pid)).Run()
	}
}

func killGroup(cmd *exec.Cmd) {
	if cmd.Process == nil {
		return
	}
	if err := exec.Command("taskkill", "/T", "/F", "/PID", strconv.Itoa(cmd.Process.Pid)).Run(); err != nil {
		_ = cmd.Process.Kill()
	}
}

var (
	kernel32                    = syscall.NewLazyDLL("kernel32.dll")
	procGenerateConsoleCtrlEven = kernel32.NewProc("GenerateConsoleCtrlEvent")
)

func generateConsoleCtrlEvent(event, groupID uint32) error {
	if r, _, err := procGenerateConsoleCtrlEven.Call(uintptr(event), uintptr(groupID)); r == 0 {
		return err
	}
	return nil
}
