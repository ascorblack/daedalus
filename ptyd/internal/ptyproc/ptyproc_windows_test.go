//go:build windows

package ptyproc

import (
	"bytes"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// These run only on Windows (the release workflow's windows-latest job): a real pseudoconsole and a
// real job object, which no other system has.

func comspec(t *testing.T) string {
	t.Helper()
	p, err := exec.LookPath("cmd.exe")
	if err != nil {
		t.Skip("no cmd.exe")
	}
	return p
}

// collect reads the console's output in the background, as the terminal's reader does; a
// pseudoconsole whose output nobody reads stops its program.
func collect(p *Proc) (func() string, chan struct{}) {
	var mu sync.Mutex
	var out bytes.Buffer
	done := make(chan struct{})
	go func() {
		defer close(done)
		buf := make([]byte, 4096)
		for {
			n, err := p.Master.Read(buf)
			mu.Lock()
			out.Write(buf[:n])
			mu.Unlock()
			if err != nil {
				return
			}
		}
	}()
	return func() string {
		mu.Lock()
		defer mu.Unlock()
		return out.String()
	}, done
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(20 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func TestAProgramRunsInAConsoleAndItsExitIsReported(t *testing.T) {
	cmd := comspec(t)
	p, err := Start(Spec{Path: cmd, Argv: []string{"cmd.exe", "/d", "/c", "echo marker-42 & exit 7"}, Dir: t.TempDir(), Env: os.Environ(), Cols: 80, Rows: 24})
	if err != nil {
		t.Fatal(err)
	}
	output, read := collect(p)
	exit := p.Wait()
	if exit.Code != 7 || exit.Signal != "" {
		t.Errorf("exit %+v", exit)
	}
	waitFor(t, "the output", func() bool { return strings.Contains(output(), "marker-42") })
	p.Close()
	select {
	case <-read:
	case <-time.After(10 * time.Second):
		t.Fatal("the output never ended after the console closed")
	}
}

func TestTypedInputReachesTheProgramAndResizeIsAccepted(t *testing.T) {
	cmd := comspec(t)
	p, err := Start(Spec{Path: cmd, Argv: []string{"cmd.exe", "/d", "/q", "/k"}, Dir: t.TempDir(), Env: os.Environ(), Cols: 80, Rows: 24})
	if err != nil {
		t.Fatal(err)
	}
	defer p.KillTree(time.Second)
	output, _ := collect(p)
	if err := p.Resize(120, 40, 0, 0); err != nil {
		t.Errorf("resize: %v", err)
	}
	if _, err := io.WriteString(p.Master, "echo typed-%OS%\r"); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the echo", func() bool { return strings.Contains(output(), "typed-Windows_NT") })
	if _, err := p.Foreground(); err == nil {
		t.Error("a console reported a foreground group")
	}
}

// Ending a terminal ends what its program started, even a process that outlives its parent.
func TestKillTreeEndsTheWholeJob(t *testing.T) {
	cmd := comspec(t)
	dir := t.TempDir()
	marker := filepath.Join(dir, "alive")
	// A grandchild that keeps touching a file, started detached from the shell that starts it.
	script := `start "" /b cmd.exe /d /c "for /l %i in (0,0,1) do (echo x> "` + marker + `" & ping -n 2 127.0.0.1 >nul)" & ping -n 600 127.0.0.1 >nul`
	p, err := Start(Spec{Path: cmd, Argv: []string{"cmd.exe", "/d", "/c", script}, Dir: dir, Env: os.Environ(), Cols: 80, Rows: 24})
	if err != nil {
		t.Fatal(err)
	}
	collect(p)
	waitFor(t, "the grandchild", func() bool { _, err := os.Stat(marker); return err == nil })
	exit := p.KillTree(500 * time.Millisecond)
	if exit.Signal == "" {
		t.Errorf("exit %+v does not say it was ended", exit)
	}
	p.Close()
	time.Sleep(500 * time.Millisecond)
	os.Remove(marker)
	time.Sleep(3 * time.Second)
	if _, err := os.Stat(marker); err == nil {
		t.Fatal("a process of the ended terminal is still running")
	}
}

func TestAnInterruptIsATypedCtrlC(t *testing.T) {
	cmd := comspec(t)
	p, err := Start(Spec{Path: cmd, Argv: []string{"cmd.exe", "/d", "/c", "ping -n 600 127.0.0.1"}, Dir: t.TempDir(), Env: os.Environ(), Cols: 80, Rows: 24})
	if err != nil {
		t.Fatal(err)
	}
	defer p.KillTree(time.Second)
	output, _ := collect(p)
	waitFor(t, "ping to start", func() bool { return strings.Contains(output(), "127.0.0.1") })
	sig, _ := ParseSignal("INT")
	if err := p.SignalForeground(sig); err != nil {
		t.Fatal(err)
	}
	select {
	case <-p.Done():
	case <-time.After(15 * time.Second):
		t.Fatal("Ctrl+C did not end ping")
	}
	tstp, ok := ParseSignal("TSTP")
	if !ok {
		t.Fatal("TSTP does not parse")
	}
	if err := p.Signal(tstp); err == nil {
		t.Error("TSTP was accepted on Windows")
	}
}
