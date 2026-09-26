//go:build windows

package server

import (
	"os"

	"golang.org/x/sys/windows"
)

// lockDir takes an exclusive lock on path without waiting. Windows releases it when the handle
// closes, which the system does for a process that dies however it dies, so a crashed daemon never
// leaves the directory locked. It is the only guard against a second daemon here: a TCP endpoint
// has no socket file to find alive.
func lockDir(path string) (func(), error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	h := windows.Handle(f.Fd())
	var ol windows.Overlapped
	if err := windows.LockFileEx(h, windows.LOCKFILE_EXCLUSIVE_LOCK|windows.LOCKFILE_FAIL_IMMEDIATELY, 0, 1, 0, &ol); err != nil {
		f.Close()
		return nil, err
	}
	return func() {
		_ = windows.UnlockFileEx(h, 0, 1, 0, &ol)
		f.Close()
	}, nil
}
