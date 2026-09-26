//go:build windows

package chrome

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"syscall"

	"github.com/ascorblack/daedalus/browserd/internal/cdp"
)

// startPiped starts the browser with its debugging pipe on two inherited handles, which Chromium on
// Windows is told with --remote-debugging-io-pipes=<read>,<write>: a Windows program has no file
// descriptors 3 and 4 to find them on. Not yet run on a real Windows.
func startPiped(opts Options, stderr io.Writer, handler func(cdp.Event)) (*exec.Cmd, *cdp.Conn, error) {
	toR, toW, err := os.Pipe()
	if err != nil {
		return nil, nil, err
	}
	fromR, fromW, err := os.Pipe()
	if err != nil {
		toR.Close()
		toW.Close()
		return nil, nil, err
	}
	for _, f := range []*os.File{toR, fromW} {
		if err := syscall.SetHandleInformation(syscall.Handle(f.Fd()), syscall.HANDLE_FLAG_INHERIT, syscall.HANDLE_FLAG_INHERIT); err != nil {
			return nil, nil, err
		}
	}
	args := Args(opts)
	args = append([]string{fmt.Sprintf("--remote-debugging-io-pipes=%d,%d", toR.Fd(), fromW.Fd())}, args...)
	cmd := exec.Command(opts.Path, args...)
	cmd.Env = opts.Env
	cmd.Stderr = stderr
	cmd.SysProcAttr = &syscall.SysProcAttr{
		AdditionalInheritedHandles: []syscall.Handle{syscall.Handle(toR.Fd()), syscall.Handle(fromW.Fd())},
		CreationFlags:              syscall.CREATE_NEW_PROCESS_GROUP,
	}
	if err := cmd.Start(); err != nil {
		toR.Close()
		toW.Close()
		fromR.Close()
		fromW.Close()
		return nil, nil, err
	}
	toR.Close()
	fromW.Close()
	conn := cdp.New(fromR, toW, handler)
	go func() {
		<-conn.Done()
		fromR.Close()
		toW.Close()
	}()
	return cmd, conn, nil
}

// killTree ends the browser's main process; its helpers watch it and end with it.
func killTree(cmd *exec.Cmd) {
	if cmd.Process != nil {
		_ = cmd.Process.Kill()
	}
}
