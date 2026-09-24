//go:build unix

package hooks

import (
	"bytes"
	"strings"
	"testing"
	"time"
)

func TestHookCommandAlwaysExitsZero(t *testing.T) {
	f := start(t)
	r, _ := f.reg.Register(Spec{ID: "L1", TerminalID: "t1"})
	run := func(env map[string]string, stdin string, args ...string) (int, string, string) {
		var out, errOut bytes.Buffer
		code := Hook(args, func(k string) string { return env[k] }, strings.NewReader(stdin), &out, &errOut)
		return code, out.String(), errOut.String()
	}
	env := map[string]string{"DAEDALUS_HOOK_URL": r.HookURL, "DAEDALUS_HOOK_TOKEN": r.Token}

	if code, out, errOut := run(env, `{"hook_event_name":"Stop"}`, "grok"); code != 0 || out != "" || errOut != "" {
		t.Fatalf("a plain post: %d %q %q", code, out, errOut)
	}
	ev := f.rec.wait(t, "hook", 1)[0]
	if ev["name"] != "grok" || ev["terminal_id"] != "t1" || ev["reply_id"] != nil {
		t.Fatalf("%v", ev)
	}

	// Held and answered: the reply is the hook's output, for the CLI to act on.
	go func() {
		for len(f.rec.of("hook")) < 2 {
			time.Sleep(5 * time.Millisecond)
		}
		ev := f.rec.of("hook")[1]
		f.reg.Reply("L1", ev["reply_id"].(string), Reply{Status: 200, Body: []byte(`{"decision":"allow"}`)})
	}()
	if code, out, _ := run(env, `{}`, "PermissionRequest", "--wait-ms", "10000"); code != 0 || out != `{"decision":"allow"}` {
		t.Fatalf("held: %d %q", code, out)
	}

	// Every failure is 0 with nothing on stdout, where hook-post would say 1 or 2: a CLI reads exit 2
	// from a command hook as a refusal of what it was about to do.
	cases := []struct {
		name string
		env  map[string]string
		args []string
		says string
	}{
		{"wrong token", map[string]string{"DAEDALUS_HOOK_URL": r.HookURL, "DAEDALUS_HOOK_TOKEN": "wrong"}, []string{"Stop"}, "401"},
		{"no launch", nil, []string{"Stop"}, "not a launch"},
		{"no source", env, nil, "source"},
		{"a bad source", env, []string{"a/b"}, "source"},
		{"two sources", env, []string{"Stop", "Start"}, "one source"},
		{"a bad hold", env, []string{"Stop", "--wait-ms", "-1"}, "--wait-ms"},
		{"an unknown flag", env, []string{"Stop", "--nope"}, "usage"},
		{"nothing listening", map[string]string{"DAEDALUS_HOOK_URL": "http://127.0.0.1:1/hook/L1", "DAEDALUS_HOOK_TOKEN": "x"}, []string{"Stop"}, "refused"},
	}
	for _, c := range cases {
		started := time.Now()
		code, out, errOut := run(c.env, `{}`, c.args...)
		if code != 0 || out != "" || !strings.Contains(errOut, c.says) {
			t.Errorf("%s: %d %q %q", c.name, code, out, errOut)
		}
		if took := time.Since(started); took > 3*time.Second {
			t.Errorf("%s took %s", c.name, took)
		}
	}
	if code, out, errOut := run(env, strings.Repeat("x", 1<<20+1), "Stop"); code != 0 || out != "" || !strings.Contains(errOut, "megabyte") {
		t.Errorf("an oversized payload: %d %q %q", code, out, errOut)
	}
	f.reg.Unregister("L1", "unregistered")
	if code, out, errOut := run(env, `{}`, "Stop"); code != 0 || out != "" || !strings.Contains(errOut, "410") {
		t.Errorf("an ended launch: %d %q %q", code, out, errOut)
	}
	if n := len(f.rec.of("hook")); n != 2 {
		t.Errorf("%d hook events; the refused posts published something", n)
	}
}
