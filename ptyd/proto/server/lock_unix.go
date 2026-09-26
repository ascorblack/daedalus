//go:build unix

package server

import (
	"os"
	"syscall"
)

// lockDir takes an exclusive, non-blocking lock on path. The kernel drops it when the process dies,
// however it dies, so a crashed daemon never leaves a directory locked. Two daemons starting at the
// same moment cannot both pass the socket check below it; they cannot both hold this.
func lockDir(path string) (func(), error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		return nil, err
	}
	return func() {
		_ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
		f.Close()
	}, nil
}
