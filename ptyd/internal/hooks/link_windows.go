//go:build windows

package hooks

import (
	"io"
	"os"
)

// hookCommandName carries the extension Windows finds a program by.
const hookCommandName = "hook-post.exe"

// linkHookCommand makes the hook command the daemon under another name. A symbolic link needs a
// privilege an ordinary Windows account does not have (or Developer Mode), so it is a hard link,
// which needs none; where that fails too (the state directory on another volume) it is a copy. A
// copy is the same program as long as the daemon runs, and the directory is rebuilt at every start.
func linkHookCommand(ptyd, command string) error {
	if err := os.Link(ptyd, command); err == nil {
		return nil
	}
	src, err := os.Open(ptyd)
	if err != nil {
		return err
	}
	defer src.Close()
	dst, err := os.OpenFile(command, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o700)
	if err != nil {
		return err
	}
	if _, err := io.Copy(dst, src); err != nil {
		dst.Close()
		os.Remove(command)
		return err
	}
	return dst.Close()
}
