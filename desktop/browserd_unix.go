//go:build !windows

package main

import (
	"os"
	"syscall"
)

// isSetuidRoot reports whether path is an executable owned by root with the setuid bit: what
// Chromium's sandbox helper must be to do its work.
func isSetuidRoot(path string) bool {
	info, err := os.Stat(path)
	if err != nil || !info.Mode().IsRegular() || info.Mode()&os.ModeSetuid == 0 {
		return false
	}
	st, ok := info.Sys().(*syscall.Stat_t)
	return ok && st.Uid == 0
}
