//go:build !windows

package main

import (
	"os"
	"syscall"
)

// processAlive answers whether the launcher that wrote the handover file is still running. Signal 0
// is the portable "does this process exist and may I signal it" question and changes nothing.
func processAlive(pid int) bool {
	process, err := os.FindProcess(pid)
	if err != nil {
		return false
	}
	return process.Signal(syscall.Signal(0)) == nil
}
