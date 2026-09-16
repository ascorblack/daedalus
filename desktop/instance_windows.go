//go:build windows

package main

import "os"

// processAlive on Windows: FindProcess opens the process, so it fails for a pid nobody holds.
func processAlive(pid int) bool {
	process, err := os.FindProcess(pid)
	if err != nil {
		return false
	}
	_ = process.Release()
	return true
}
