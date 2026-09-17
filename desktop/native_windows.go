//go:build windows

package main

// Windows has no process groups in the POSIX sense and no signals to send them. A child started in
// its own process group can be ended with the group, and `taskkill /T` is what walks the tree the
// supervisor and the bot make — the nearest thing the platform has to killing a session.

import (
	"os/exec"
	"strconv"
	"syscall"
)

func setProcessGroup(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: syscall.CREATE_NEW_PROCESS_GROUP}
}

// terminateGroup ends the tree politely. taskkill without /F closes the processes rather than
// tearing them down, which gives the supervisor its chance to drain the bot.
func terminateGroup(cmd *exec.Cmd) {
	if cmd.Process == nil {
		return
	}
	_ = exec.Command("taskkill", "/T", "/PID", strconv.Itoa(cmd.Process.Pid)).Run()
}

func killGroup(cmd *exec.Cmd) {
	if cmd.Process == nil {
		return
	}
	if err := exec.Command("taskkill", "/T", "/F", "/PID", strconv.Itoa(cmd.Process.Pid)).Run(); err != nil {
		_ = cmd.Process.Kill()
	}
}
