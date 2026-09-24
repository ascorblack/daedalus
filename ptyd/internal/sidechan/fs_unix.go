//go:build unix

package sidechan

import (
	"fmt"
	"os"
	"runtime"
	"strconv"
	"syscall"
)

// openNoFollow opens a file for reading without following a symlink in its last component, and
// without blocking on a FIFO (whose open would otherwise wait for a writer that never comes).
func openNoFollow(p string, dir bool) (*os.File, error) {
	flags := syscall.O_RDONLY | syscall.O_NOFOLLOW | syscall.O_NONBLOCK | syscall.O_CLOEXEC
	if dir {
		flags |= syscall.O_DIRECTORY
	}
	fd, err := syscall.Open(p, flags, 0)
	if err != nil {
		return nil, &os.PathError{Op: "open", Path: p, Err: err}
	}
	// Reads of a regular file never block; the flag only mattered for the open.
	_ = syscall.SetNonblock(fd, false)
	return os.NewFile(uintptr(fd), p), nil
}

// openedPath is the path the kernel has for an open file. Only Linux says it (through /proc); on
// other systems the check made before the open stands alone.
func openedPath(f *os.File) (string, bool) {
	if runtime.GOOS != "linux" {
		return "", false
	}
	p, err := os.Readlink("/proc/self/fd/" + strconv.Itoa(int(f.Fd())))
	if err != nil || len(p) == 0 || p[0] != '/' {
		return "", false
	}
	return p, true
}

// fileID tells one file from another at the same path: its device and inode.
func fileID(st os.FileInfo) string {
	if s, ok := st.Sys().(*syscall.Stat_t); ok {
		return fmt.Sprintf("%d:%d", s.Dev, s.Ino)
	}
	return ""
}
