package toolsmcp

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// browserSet writes a tool set the way the host does, into a launch directory of its own.
func browserSet(t *testing.T, holdMS int64) string {
	t.Helper()
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "tools"), 0o700); err != nil {
		t.Fatal(err)
	}
	file := map[string]any{
		"server":       "daedalus_browser",
		"instructions": "A real browser the operator can watch.",
		"hold_ms":      holdMS,
		"tools": []map[string]any{
			{"name": "BrowserSnapshot", "description": "See the page.", "inputSchema": map[string]any{"type": "object", "properties": map[string]any{"tab": map[string]any{"type": "string"}}}},
			{"name": "BrowserAct", "description": "Act on the page.", "inputSchema": map[string]any{"type": "object", "properties": map[string]any{"action": map[string]any{"type": "string"}, "element": map[string]any{"type": "string"}}, "required": []string{"action", "element"}}},
			{"name": "", "description": "a tool without a name is left out", "inputSchema": map[string]any{}},
		},
	}
	raw, _ := json.Marshal(file)
	if err := os.WriteFile(filepath.Join(dir, "tools", "browser.json"), raw, 0o600); err != nil {
		t.Fatal(err)
	}
	return dir
}

// serveSet drives ServeSet the way a CLI drives `ptyd tools-mcp --set <set>`.
func serveSet(t *testing.T, set string, env map[string]string) *client {
	t.Helper()
	inR, inW := io.Pipe()
	outR, outW := io.Pipe()
	c := &client{t: t, in: inW, wake: make(chan struct{}, 1), done: make(chan error, 1), stderr: &syncBuffer{}}
	go func() {
		c.done <- ServeSet(context.Background(), set, func(k string) string { return env[k] }, inR, outW, c.stderr)
		outW.Close()
	}()
	go func() {
		sc := bufio.NewScanner(outR)
		sc.Buffer(nil, 8<<20)
		for sc.Scan() {
			var m map[string]any
			if err := json.Unmarshal(sc.Bytes(), &m); err != nil {
				panic(fmt.Sprintf("the server wrote %q", sc.Text()))
			}
			c.mu.Lock()
			c.got = append(c.got, m)
			c.mu.Unlock()
			select {
			case c.wake <- struct{}{}:
			default:
			}
		}
	}()
	t.Cleanup(func() { inW.Close() })
	return c
}

func TestAFileSetServesItsToolsAndHoldsEachCallForTheHost(t *testing.T) {
	f := listener(t)
	c := serveSet(t, "browser", f.env("DAEDALUS_LAUNCH_DIR", browserSet(t, 20000), "DAEDALUS_LAUNCH_ID", "L1"))
	c.request(1, "initialize", map[string]any{"protocolVersion": "2025-06-18", "clientInfo": map[string]any{"name": "claude-code", "version": "2.1.290"}})
	init := c.await(1)["result"].(map[string]any)
	if init["serverInfo"].(map[string]any)["name"] != "daedalus_browser" || init["instructions"] != "A real browser the operator can watch." {
		t.Fatalf("%v", init)
	}
	c.request(2, "tools/list", map[string]any{})
	tools := c.await(2)["result"].(map[string]any)["tools"].([]any)
	var names []string
	for _, raw := range tools {
		names = append(names, raw.(map[string]any)["name"].(string))
	}
	if strings.Join(names, ",") != "BrowserSnapshot,BrowserAct" {
		t.Fatalf("tools %v", names)
	}
	// The two announcements go to the set's own name, and say which set.
	for i := 1; i <= 2; i++ {
		ev := f.rec.hookN(t, i)
		if body := asMap(ev["body"]); ev["name"] != "tools" || body["tool"] != "hello" || body["set"] != "browser" || ev["reply_id"] != nil {
			t.Fatalf("an announcement: %v", ev)
		}
	}

	c.call("s", "BrowserSnapshot", map[string]any{"tab": "t1"})
	ev := f.rec.hookN(t, 3)
	if ev["name"] != "tools" || ev["hold_ms"] != int64(20000) {
		t.Fatalf("%v", ev)
	}
	if got := withoutCallID(t, ev["body"]); got != `{"arguments":{"tab":"t1"},"set":"browser","tool":"BrowserSnapshot"}` {
		t.Fatalf("body %s", got)
	}
	c.call("a", "BrowserAct", map[string]any{"action": "click", "element": "the Buy button", "ref": "e20"})
	act := f.rec.hookN(t, 4)
	if c.has("s") || c.has("a") {
		t.Fatal("a call was answered before the host replied")
	}
	f.reply(t, ev, `{"text":"- button \"Buy\" [ref=e20]"}`)
	if text, isErr := c.result("s"); isErr || text != `- button "Buy" [ref=e20]` {
		t.Fatalf("%q %v", text, isErr)
	}
	f.reply(t, act, `{"text":"needs the operator's approval","error":true}`)
	if text, isErr := c.result("a"); !isErr || !strings.Contains(text, "approval") {
		t.Fatalf("%q %v", text, isErr)
	}

	// Arguments are checked against the schema's required names before anything is posted.
	c.call("m", "BrowserAct", map[string]any{"action": "click"})
	if text, isErr := c.result("m"); !isErr || !strings.Contains(text, "needs element") {
		t.Fatalf("%q %v", text, isErr)
	}
	c.call("x", "BrowserAct", []string{"not", "an", "object"})
	if text, isErr := c.result("x"); !isErr || !strings.Contains(text, "object") {
		t.Fatalf("%q %v", text, isErr)
	}
	c.call("u", "BrowserEvaluate", map[string]any{})
	if code := errorCode(c.await("u")); code != codeInvalidParams {
		t.Fatalf("an unknown tool: %v", code)
	}
	if n := len(f.rec.hooks()); n != 4 {
		t.Fatalf("%d posts; the refused calls must not be posted", n)
	}
}

func TestASilentHostIsAnErrorForAFileSetNeverASuccess(t *testing.T) {
	f := listener(t)
	c := serveSet(t, "browser", f.env("DAEDALUS_LAUNCH_DIR", browserSet(t, 20000), "DAEDALUS_TOOLS_HOLD_MS", "150"))
	c.call(1, "BrowserSnapshot", map[string]any{})
	ev := f.rec.hookN(t, 1)
	if ev["hold_ms"] != int64(150) {
		t.Fatalf("the environment's hold wins over the file's: %v", ev)
	}
	if text, isErr := c.result(1); !isErr || !strings.Contains(text, "may or may not have happened") {
		t.Fatalf("%q %v", text, isErr)
	}
}

func TestACancelledToolCallReleasesItsHold(t *testing.T) {
	f := listener(t)
	c := serveSet(t, "browser", f.env("DAEDALUS_LAUNCH_DIR", browserSet(t, 60000)))
	c.call(7, "BrowserAct", map[string]any{"action": "click", "element": "the Pay button"})
	f.rec.hookN(t, 1)
	if f.reg.Held() != 1 {
		t.Fatalf("%d held", f.reg.Held())
	}
	c.send(map[string]any{"jsonrpc": "2.0", "method": "notifications/cancelled", "params": map[string]any{"requestId": 7}})
	deadline := time.Now().Add(5 * time.Second)
	for f.reg.Held() != 0 {
		if time.Now().After(deadline) {
			t.Fatal("the held post outlived its cancelled call")
		}
		time.Sleep(10 * time.Millisecond)
	}
	c.request(8, "ping", nil)
	c.await(8)
	if c.has(7) {
		t.Fatal("a cancelled call was answered")
	}
}

func TestAnUnknownSetStillServesAndSaysWhy(t *testing.T) {
	f := listener(t)
	for _, tc := range []struct{ set, dir, want string }{
		{"browser", t.TempDir(), "no tool set"},
		{"browser", "", "DAEDALUS_LAUNCH_DIR"},
		{"Bad/Name", t.TempDir(), "not a tool set's name"},
	} {
		env := f.env()
		if tc.dir != "" {
			env["DAEDALUS_LAUNCH_DIR"] = tc.dir
		}
		c := serveSet(t, tc.set, env)
		c.request(1, "tools/list", map[string]any{})
		if tools := c.await(1)["result"].(map[string]any)["tools"].([]any); len(tools) != 0 {
			t.Fatalf("%s: %v", tc.set, tools)
		}
		c.call(2, "BrowserSnapshot", map[string]any{})
		if text, isErr := c.result(2); !isErr || !strings.Contains(text, tc.want) {
			t.Fatalf("%s: %q %v", tc.set, text, isErr)
		}
		if !strings.Contains(c.stderr.String(), "ptyd tools-mcp --set "+tc.set) {
			t.Fatalf("not logged: %q", c.stderr.String())
		}
	}
}

func TestTheTeamSetIgnoresNothingOfItsWireAndAFileSetIgnoresTheTeamRoute(t *testing.T) {
	f := listener(t)
	// A file set never takes the host's team route: that route knows only the team's two tools.
	c := serveSet(t, "browser", f.env("DAEDALUS_LAUNCH_DIR", browserSet(t, 20000), "DAEDALUS_TEAM_URL", "http://127.0.0.1:9/api/team/x", "DAEDALUS_TEAM_TOKEN", "t"))
	c.call(1, "BrowserSnapshot", map[string]any{})
	ev := f.rec.hookN(t, 1)
	if ev["name"] != "tools" {
		t.Fatalf("%v", ev)
	}
	f.reply(t, ev, `{"text":"ok"}`)
	if text, _ := c.result(1); text != "ok" {
		t.Fatalf("%q", text)
	}
}
