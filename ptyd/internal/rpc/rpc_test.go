package rpc_test

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/internal/emulator"
	"github.com/ascorblack/daedalus/ptyd/internal/emulator/fake"
	"github.com/ascorblack/daedalus/ptyd/internal/events"
	"github.com/ascorblack/daedalus/ptyd/internal/logx"
	"github.com/ascorblack/daedalus/ptyd/internal/rpc"
	"github.com/ascorblack/daedalus/ptyd/internal/server"
	"github.com/ascorblack/daedalus/ptyd/internal/server/clienttest"
	"github.com/ascorblack/daedalus/ptyd/internal/term"
	"github.com/ascorblack/daedalus/ptyd/internal/wire"
)

type fixture struct {
	dir    string
	client *clienttest.Client
	daemon *rpc.Daemon

	mu       sync.Mutex
	lastFake func() *fake.Emulator
}

// start runs a daemon in-process on a fresh run directory, with the fake emulator so a test can
// script the modes that keys and pastes depend on.
func start(t *testing.T) *fixture {
	t.Helper()
	return startWith(t, nil)
}

// startWith runs the daemon with the given emulator instead, when it is not nil: a long run through
// the fake would keep every byte it was fed.
func startWith(t *testing.T, emu emulator.Factory) *fixture {
	t.Helper()
	base, err := os.MkdirTemp("", "ptyd")
	if err != nil {
		t.Fatal(err)
	}
	f := &fixture{dir: base}
	cfg, err := config.Parse([]string{"--env", "container", "--run-dir", filepath.Join(base, "run"),
		"--state-dir", filepath.Join(base, "state"), "--home", base})
	if err != nil {
		t.Fatal(err)
	}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	journal, err := logx.OpenRotating(filepath.Join(cfg.StateDir, "agent-writes.jsonl"), 1<<20, 2)
	if err != nil {
		t.Fatal(err)
	}
	evlog := events.NewLog(1000)
	var last *fake.Emulator
	if emu == nil {
		emu = fake.Factory(func(e *fake.Emulator) {
			f.mu.Lock()
			last = e
			f.mu.Unlock()
		})
	}
	registry := term.NewRegistry(term.Deps{
		Emulator: emu, Events: events.NewDebouncer(evlog, events.Policies), Journal: logx.NewJournal(journal),
		Clock: term.RealClock{}, Log: log, KillGrace: 200 * time.Millisecond,
	}, 4)
	ep, err := server.Prepare(cfg.RunDir, "unix")
	if err != nil {
		t.Fatal(err)
	}
	f.daemon = &rpc.Daemon{Config: cfg, Instance: "inst1", StartedAt: time.Now().UTC(), Registry: registry,
		Events: evlog, Log: log, EmulatorName: "fake@0", Environ: os.Environ()}
	srv := server.New(ep.Token, log, f.daemon.Hello)
	f.daemon.Register(srv)
	go srv.Serve(ep.Listener)
	f.client, err = clienttest.Dial(cfg.RunDir)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		f.client.Close()
		ep.Listener.Close()
		registry.Shutdown(100 * time.Millisecond)
		srv.Close()
		ep.Release()
		journal.Close()
		os.RemoveAll(base)
	})
	// The fake of the terminal created last: creates are serial in these tests.
	f.lastFake = func() *fake.Emulator {
		f.mu.Lock()
		defer f.mu.Unlock()
		return last
	}
	return f
}

func (f *fixture) call(t *testing.T, method string, params, result any) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	if err := f.client.Call(ctx, method, params, result); err != nil {
		t.Fatalf("%s: %v", method, err)
	}
}

func (f *fixture) callErr(method string, params any) *wire.Error {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	err := f.client.Call(ctx, method, params, nil)
	var we *wire.Error
	if errors.As(err, &we) {
		return we
	}
	return nil
}

type output struct {
	FromSeq int64  `json:"from_seq"`
	ToSeq   int64  `json:"to_seq"`
	Data    string `json:"data"`
	DataB64 string `json:"data_b64"`
	Gap     bool   `json:"gap"`
	HeadSeq int64  `json:"head_seq"`
}

// waitOutput reads stripped output from 0 until it contains want.
func (f *fixture) waitOutput(t *testing.T, id, want string) string {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for {
		var out output
		f.call(t, "terminal.read_output", map[string]any{"id": id, "since_seq": 0, "strip": true}, &out)
		if strings.Contains(out.Data, want) {
			return out.Data
		}
		if time.Now().After(deadline) {
			t.Fatalf("output of %s never contained %q: %q", id, want, out.Data)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func TestBashEchoKillAndExitEvent(t *testing.T) {
	f := start(t)
	var sub struct {
		Instance string `json:"instance"`
		FromSeq  int64  `json:"from_seq"`
		Resync   bool   `json:"resync"`
	}
	f.call(t, "events.subscribe", map[string]any{"after_seq": 0}, &sub)
	if sub.Instance != "inst1" || sub.FromSeq != 1 || sub.Resync {
		t.Fatalf("subscribe: %+v", sub)
	}
	var created struct {
		ID          string `json:"id"`
		Pid         int    `json:"pid"`
		Cwd         string `json:"cwd"`
		CwdFallback bool   `json:"cwd_fallback"`
	}
	f.call(t, "terminal.create", map[string]any{"id": "b1", "argv": []string{"bash", "--norc", "--noprofile"},
		"cwd": f.dir, "cols": 100, "rows": 30, "env": map[string]string{"PS1": "$ "}}, &created)
	if created.ID != "b1" || created.Pid == 0 || created.Cwd != f.dir || created.CwdFallback {
		t.Fatalf("create: %+v", created)
	}
	if _, err := f.client.WaitEvent("terminal.created", "b1", 5*time.Second); err != nil {
		t.Fatal(err)
	}
	var receipt term.Receipt
	f.call(t, "terminal.write", map[string]any{"id": "b1", "text": "echo hi\r",
		"origin": map[string]any{"kind": "agent", "actor": "test"}}, &receipt)
	if receipt.Bytes != 8 {
		t.Fatalf("receipt %+v", receipt)
	}
	f.waitOutput(t, "b1", "\nhi\n")

	var killed struct {
		ExitCode int     `json:"exit_code"`
		Signal   *string `json:"signal"`
	}
	f.call(t, "terminal.kill", map[string]any{"id": "b1", "grace_ms": 500}, &killed)
	ev, err := f.client.WaitEvent("terminal.exited", "b1", 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	var data struct {
		ExitCode int     `json:"exit_code"`
		Signal   *string `json:"signal"`
		Seq      int64   `json:"seq"`
	}
	_ = json.Unmarshal(ev.Data, &data)
	if data.ExitCode != killed.ExitCode || (data.Signal == nil) != (killed.Signal == nil) || data.Seq == 0 {
		t.Fatalf("exit event %s, kill reply %+v", ev.Data, killed)
	}
	// bash exits on SIGHUP: the grace was enough and nothing needed SIGKILL.
	if killed.Signal == nil || *killed.Signal != "HUP" {
		t.Fatalf("bash ended with %+v", killed)
	}

	var info term.Info
	f.call(t, "terminal.get", map[string]any{"id": "b1"}, &info)
	if info.Status != "exited" || info.ExitCode == nil {
		t.Fatalf("get after exit: %+v", info)
	}
	if we := f.callErr("terminal.write", map[string]any{"id": "b1", "text": "x",
		"origin": map[string]any{"kind": "agent", "actor": "t"}}); we == nil || we.Code != wire.CodeExited {
		t.Fatalf("write after exit: %v", we)
	}
	f.call(t, "terminal.forget", map[string]any{"id": "b1"}, nil)
	if we := f.callErr("terminal.get", map[string]any{"id": "b1"}); we == nil || we.Code != wire.CodeNotFound {
		t.Fatalf("get after forget: %v", we)
	}
}

func TestCreateValidation(t *testing.T) {
	f := start(t)
	cases := []struct {
		params map[string]any
		code   int
	}{
		{map[string]any{"id": "bad id!"}, wire.CodeInvalidParams},
		{map[string]any{"id": "a", "cols": 9, "rows": 5}, wire.CodeInvalidSize},
		{map[string]any{"id": "a", "cols": 501, "rows": 24}, wire.CodeInvalidSize},
		{map[string]any{"id": "a", "sandbox": map[string]any{"writable": []string{"/x"}}}, wire.CodeUnsupported},
		{map[string]any{"id": "a", "argv": []string{"no-such-program-here"}}, wire.CodeInvalidParams},
		{map[string]any{"id": "a", "cwd": "relative"}, wire.CodeInvalidParams},
		{map[string]any{"id": "a", "argv": []string{"sh"}, "shell": map[string]any{}}, wire.CodeInvalidParams},
		{map[string]any{"id": "a", "unknown_option": true}, wire.CodeInvalidParams},
		{map[string]any{"id": "a", "ring_bytes": 5}, wire.CodeInvalidParams},
	}
	for _, c := range cases {
		if we := f.callErr("terminal.create", c.params); we == nil || we.Code != c.code {
			t.Errorf("%v: %v, want code %d", c.params, we, c.code)
		}
	}
	// A missing folder falls back to home and says so.
	var created struct {
		Cwd         string `json:"cwd"`
		CwdFallback bool   `json:"cwd_fallback"`
		Shell       string `json:"shell"`
	}
	f.call(t, "terminal.create", map[string]any{"id": "fb", "cwd": "/no/such/folder"}, &created)
	if !created.CwdFallback || created.Cwd != f.dir || created.Shell == "" {
		t.Fatalf("fallback: %+v", created)
	}
	if we := f.callErr("terminal.create", map[string]any{"id": "fb"}); we == nil || we.Code != wire.CodeForbidden {
		t.Fatalf("duplicate id: %v", we)
	}
}

func TestWriteKindsFollowTheModes(t *testing.T) {
	f := start(t)
	f.call(t, "terminal.create", map[string]any{"id": "c", "argv": []string{"cat", "-v"}}, nil)
	emu := f.lastFake()
	// cat -v shows control characters, so the bytes written are visible in the output.
	emu.SetModes(emulator.Modes{AppCursor: true, BracketedPaste: true})
	origin := map[string]any{"kind": "agent", "actor": "t"}
	f.call(t, "terminal.write", map[string]any{"id": "c", "keys": []string{"Up", "Enter"}, "origin": origin}, nil)
	f.waitOutput(t, "c", "^[OA")
	f.call(t, "terminal.write", map[string]any{"id": "c", "paste": "x\x1b[201~y", "origin": origin, "wait": "none"}, nil)
	f.call(t, "terminal.write", map[string]any{"id": "c", "keys": []string{"Enter"}, "origin": origin}, nil)
	f.waitOutput(t, "c", "^[[200~xy^[[201~")
	emu.SetModes(emulator.Modes{})
	f.call(t, "terminal.write", map[string]any{"id": "c", "keys": []string{"Up", "Enter"}, "origin": origin}, nil)
	f.waitOutput(t, "c", "^[[A")
	raw := base64.StdEncoding.EncodeToString([]byte{0x01, '\r'})
	f.call(t, "terminal.write", map[string]any{"id": "c", "bytes_b64": raw, "origin": origin}, nil)
	f.waitOutput(t, "c", "^A")

	bad := []map[string]any{
		{"id": "c", "origin": origin},
		{"id": "c", "text": "a", "keys": []string{"Enter"}, "origin": origin},
		{"id": "c", "text": "a", "origin": map[string]any{"kind": "human", "actor": "t"}},
		{"id": "c", "keys": []string{"Hyper-q"}, "origin": origin},
		{"id": "c", "text": "a", "origin": origin, "wait": "sometimes"},
	}
	for _, p := range bad {
		if we := f.callErr("terminal.write", p); we == nil || we.Code != wire.CodeInvalidParams {
			t.Errorf("%v: %v", p, we)
		}
	}
}

func TestReadOutputRawAndGaps(t *testing.T) {
	f := start(t)
	f.call(t, "terminal.create", map[string]any{"id": "p", "argv": []string{"sh", "-c", `printf '\033[31mred\033[0m\n'; sleep 30`},
		"ring_bytes": 1 << 20}, nil)
	f.waitOutput(t, "p", "red")
	var out output
	f.call(t, "terminal.read_output", map[string]any{"id": "p", "since_seq": 0}, &out)
	raw, _ := base64.StdEncoding.DecodeString(out.DataB64)
	if string(raw) != "\x1b[31mred\x1b[0m\r\n" || out.FromSeq != 0 || out.ToSeq != int64(len(raw)) || out.Gap {
		t.Fatalf("raw read %+v (%q)", out, raw)
	}
	// Reading from the head returns nothing, and the offsets say where the stream is.
	f.call(t, "terminal.read_output", map[string]any{"id": "p", "since_seq": out.ToSeq}, &out)
	if out.DataB64 != "" || out.FromSeq != out.HeadSeq {
		t.Fatalf("read at the head %+v", out)
	}
}

func TestResizeSignalAndStats(t *testing.T) {
	f := start(t)
	f.call(t, "terminal.create", map[string]any{"id": "s", "argv": []string{"sh"}, "env": map[string]string{"PS1": "$ "}}, nil)
	if we := f.callErr("terminal.resize", map[string]any{"id": "s", "cols": 9, "rows": 5}); we == nil || we.Code != wire.CodeInvalidSize {
		t.Fatalf("9x5: %v", we)
	}
	f.call(t, "terminal.resize", map[string]any{"id": "s", "cols": 120, "rows": 40}, nil)
	origin := map[string]any{"kind": "agent", "actor": "t"}
	f.call(t, "terminal.write", map[string]any{"id": "s", "text": "stty size; sleep 1000\r", "origin": origin}, nil)
	f.waitOutput(t, "s", "40 120")
	var info term.Info
	deadline := time.Now().Add(5 * time.Second)
	for !info.Busy {
		if time.Now().After(deadline) {
			t.Fatal("never busy")
		}
		f.call(t, "terminal.get", map[string]any{"id": "s"}, &info)
	}
	if info.Cols != 120 || info.Rows != 40 || info.SizeOwner == nil || *info.SizeOwner != "host" {
		t.Fatalf("info %+v", info)
	}

	var stats struct {
		Supported bool `json:"supported"`
		Terminals []struct {
			ID        string `json:"id"`
			Processes int    `json:"processes"`
			RSSBytes  int64  `json:"rss_bytes"`
		} `json:"terminals"`
		Machine struct {
			MemTotalBytes int64 `json:"mem_total_bytes"`
			CPUs          int   `json:"cpus"`
		} `json:"machine"`
	}
	f.call(t, "terminal.stats", map[string]any{"ids": []string{"s"}}, &stats)
	if stats.Supported {
		if len(stats.Terminals) != 1 || stats.Terminals[0].Processes < 2 || stats.Terminals[0].RSSBytes <= 0 {
			t.Fatalf("stats %+v", stats)
		}
		if stats.Machine.MemTotalBytes <= 0 || stats.Machine.CPUs <= 0 {
			t.Fatalf("machine %+v", stats.Machine)
		}
	}

	f.call(t, "terminal.signal", map[string]any{"id": "s", "signal": "INT"}, nil)
	deadline = time.Now().Add(5 * time.Second)
	for info.Busy {
		if time.Now().After(deadline) {
			t.Fatal("the interrupt did not reach the foreground job")
		}
		f.call(t, "terminal.get", map[string]any{"id": "s"}, &info)
	}
	if we := f.callErr("terminal.signal", map[string]any{"id": "s", "signal": "BOGUS"}); we == nil || we.Code != wire.CodeInvalidParams {
		t.Fatalf("bogus signal: %v", we)
	}
}

func TestDaemonInfoAndLimits(t *testing.T) {
	f := start(t)
	var info map[string]any
	f.call(t, "daemon.info", nil, &info)
	for _, k := range []string{"version", "protocol", "instance", "env", "os", "arch", "pid", "started_at", "uptime_s",
		"capabilities", "hooks", "limits", "counts", "machine"} {
		if _, ok := info[k]; !ok {
			t.Errorf("daemon.info lacks %s", k)
		}
	}
	if info["env"] != "container" || info["instance"] != "inst1" {
		t.Fatalf("info %v", info)
	}
	for i := 0; i < 4; i++ {
		f.call(t, "terminal.create", map[string]any{"id": "t" + string(rune('a'+i)), "argv": []string{"sleep", "100"}}, nil)
	}
	if we := f.callErr("terminal.create", map[string]any{"id": "over", "argv": []string{"sleep", "100"}}); we == nil || we.Code != wire.CodeLimit {
		t.Fatalf("over the limit: %v", we)
	}
	var list struct {
		Terminals []term.Info `json:"terminals"`
	}
	f.call(t, "terminal.list", map[string]any{"ids": []string{"tb", "tc"}}, &list)
	if len(list.Terminals) != 2 || list.Terminals[0].ID != "tb" {
		t.Fatalf("list %+v", list)
	}
}

func TestSubscriptionResumesWithoutLoss(t *testing.T) {
	f := start(t)
	f.call(t, "terminal.create", map[string]any{"id": "e1", "argv": []string{"true"}}, nil)
	f.call(t, "terminal.create", map[string]any{"id": "e2", "argv": []string{"true"}}, nil)
	deadline := time.Now().Add(5 * time.Second)
	for f.daemon.Events.Last() < 4 {
		if time.Now().After(deadline) {
			t.Fatal("the terminals never exited")
		}
		time.Sleep(10 * time.Millisecond)
	}
	// A host that saw up to event 2 resumes from 3, in order.
	var sub struct {
		FromSeq int64 `json:"from_seq"`
		Resync  bool  `json:"resync"`
	}
	f.call(t, "events.subscribe", map[string]any{"after_seq": 2}, &sub)
	if sub.FromSeq != 3 || sub.Resync {
		t.Fatalf("resume: %+v", sub)
	}
	e, err := f.client.WaitEvent("terminal.exited", "", 5*time.Second)
	if err != nil || e.Seq < 3 {
		t.Fatalf("first event after resume: %+v %v", e, err)
	}
	// A cursor from before a restart of the daemon is told to resync.
	f.call(t, "events.subscribe", map[string]any{"after_seq": 999}, &sub)
	if !sub.Resync || sub.FromSeq != 1 {
		t.Fatalf("stale cursor: %+v", sub)
	}
	f.call(t, "events.unsubscribe", nil, nil)
}
