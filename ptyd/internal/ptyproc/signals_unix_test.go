//go:build unix

package ptyproc

import (
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"syscall"
	"testing"
	"time"
)

// A daemon that was started with the hangup and the interrupt ignored still starts its programs with
// both at their defaults. The ignoring is inherited only at a process's start, so the test runs
// itself again as a process started that way.
func TestProgramsStartWithTheDefaultSignals(t *testing.T) {
	if os.Getenv("PTYPROC_IGNORING_CHILD") != "" {
		ignoringChild(t)
		return
	}
	signal.Ignore(syscall.SIGHUP, syscall.SIGINT)
	cmd := exec.Command(os.Args[0], "-test.run=^TestProgramsStartWithTheDefaultSignals$", "-test.v")
	cmd.Env = append(os.Environ(), "PTYPROC_IGNORING_CHILD=1")
	out, err := cmd.CombinedOutput()
	signal.Reset(syscall.SIGHUP, syscall.SIGINT)
	if err != nil || !strings.Contains(string(out), "--- PASS") {
		t.Fatalf("%v\n%s", err, out)
	}
}

func ignoringChild(t *testing.T) {
	if !signal.Ignored(syscall.SIGHUP) || !signal.Ignored(syscall.SIGINT) {
		t.Fatal("the test process did not start with the signals ignored")
	}
	sleep, err := exec.LookPath("sleep")
	if err != nil {
		t.Skip("no sleep")
	}
	for _, sig := range []syscall.Signal{syscall.SIGHUP, syscall.SIGINT} {
		p, err := Start(Spec{Path: sleep, Argv: []string{"sleep", "30"}, Env: os.Environ(), Cols: 80, Rows: 24})
		if err != nil {
			t.Fatal(err)
		}
		go func() {
			// The program's output must be read, or it can stop on a full PTY.
			buf := make([]byte, 1024)
			for {
				if _, err := p.Master.Read(buf); err != nil {
					return
				}
			}
		}()
		if err := p.Signal(sig); err != nil {
			t.Fatal(err)
		}
		select {
		case <-p.Done():
		case <-time.After(5 * time.Second):
			_ = syscall.Kill(p.Pid, syscall.SIGKILL)
			t.Fatalf("the program ignored SIG%s", SignalName(sig))
		}
		if e := p.Wait(); e.Signal != SignalName(sig) {
			t.Errorf("SIG%s: the program ended with %+v", SignalName(sig), e)
		}
		p.Close()
	}
}
