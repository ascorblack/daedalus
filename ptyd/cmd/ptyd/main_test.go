package main

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/server"
	"github.com/ascorblack/daedalus/ptyd/internal/server/clienttest"
)

// The test binary doubles as the daemon: run with this variable set, it is `ptyd` itself. That tests
// the real main — flags, signals, shutdown — without a separate build step.
const runMain = "PTYD_TEST_RUN_MAIN"

func TestMain(m *testing.M) {
	if os.Getenv(runMain) == "1" {
		os.Args = append([]string{"ptyd"}, strings.Fields(os.Getenv("PTYD_TEST_ARGS"))...)
		main()
		os.Exit(0)
	}
	os.Exit(m.Run())
}

func daemon(t *testing.T, args ...string) *exec.Cmd {
	t.Helper()
	cmd := exec.Command(os.Args[0], "-test.run=^$")
	cmd.Env = append(os.Environ(), runMain+"=1", "PTYD_TEST_ARGS="+strings.Join(args, " "))
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = cmd.Process.Kill()
		_, _ = cmd.Process.Wait()
		if t.Failed() {
			t.Logf("daemon stderr:\n%s", stderr.String())
		}
	})
	return cmd
}

func waitFile(t *testing.T, path string) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for {
		if _, err := os.Stat(path); err == nil {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("%s never appeared", path)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func TestServeEndToEnd(t *testing.T) {
	base, err := os.MkdirTemp("", "ptyd")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(base)
	run, state := filepath.Join(base, "run"), filepath.Join(base, "state")
	args := []string{"serve", "--env", "container", "--run-dir", run, "--state-dir", state, "--home", base}
	cmd := daemon(t, args...)
	waitFile(t, filepath.Join(run, server.EndpointFile))

	c, err := clienttest.Dial(run)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	call := func(method string, params, result any) {
		t.Helper()
		if err := c.Call(ctx, method, params, result); err != nil {
			t.Fatalf("%s: %v", method, err)
		}
	}
	call("events.subscribe", map[string]any{"after_seq": 0}, nil)
	call("terminal.create", map[string]any{"id": "bash1", "argv": []string{"bash", "--norc", "--noprofile"},
		"env": map[string]string{"PS1": "$ "}}, nil)
	call("terminal.write", map[string]any{"id": "bash1", "text": "echo hi\r",
		"origin": map[string]any{"kind": "agent", "actor": "test"}}, nil)
	deadline := time.Now().Add(10 * time.Second)
	for {
		var out struct {
			Data string `json:"data"`
		}
		call("terminal.read_output", map[string]any{"id": "bash1", "since_seq": 0, "strip": true}, &out)
		if strings.Contains(out.Data, "\nhi\n") {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("no hi in %q", out.Data)
		}
		time.Sleep(20 * time.Millisecond)
	}
	var killed struct {
		ExitCode int    `json:"exit_code"`
		Signal   string `json:"signal"`
	}
	call("terminal.kill", map[string]any{"id": "bash1"}, &killed)
	ev, err := c.WaitEvent("terminal.exited", "bash1", 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	var exited struct {
		ExitCode int    `json:"exit_code"`
		Signal   string `json:"signal"`
	}
	_ = json.Unmarshal(ev.Data, &exited)
	if exited != killed || killed.Signal != "HUP" {
		t.Fatalf("exit event %+v, kill reply %+v", exited, killed)
	}

	// A second daemon on the same directory refuses and leaves the first one alone.
	second := exec.Command(os.Args[0], "-test.run=^$")
	second.Env = append(os.Environ(), runMain+"=1", "PTYD_TEST_ARGS="+strings.Join(args, " "))
	out, err := second.CombinedOutput()
	if err == nil || !strings.Contains(string(out), "another ptyd holds") {
		t.Fatalf("second instance: %v %s", err, out)
	}
	call("daemon.info", nil, nil)

	// A terminal still running at shutdown is ended with it.
	call("terminal.create", map[string]any{"id": "sleeper", "argv": []string{"sleep", "1000"}}, nil)
	var info struct {
		Pid int `json:"pid"`
	}
	call("terminal.get", map[string]any{"id": "sleeper"}, &info)

	// An agent's write was journalled; the log never saw the text.
	journal, _ := os.ReadFile(filepath.Join(state, "agent-writes.jsonl"))
	if !strings.Contains(string(journal), `"text":"echo hi\r"`) {
		t.Fatalf("journal %s", journal)
	}

	if err := cmd.Process.Signal(syscall.SIGTERM); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("daemon exit: %v", err)
		}
	case <-time.After(15 * time.Second):
		t.Fatal("the daemon did not stop on SIGTERM")
	}
	for _, f := range []string{server.EndpointFile, server.TokenFile, server.SocketFile} {
		if _, err := os.Stat(filepath.Join(run, f)); !os.IsNotExist(err) {
			t.Errorf("%s left after shutdown", f)
		}
	}
	if syscall.Kill(info.Pid, 0) == nil {
		data, _ := os.ReadFile("/proc/" + strconv.Itoa(info.Pid) + "/stat")
		if !bytes.Contains(data, []byte(") Z")) {
			t.Errorf("the terminal's program %d survived the daemon", info.Pid)
		}
	}
}
