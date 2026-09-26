package browser

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

func testGroup() *Group {
	m := &Manager{log: slog.New(slog.NewTextHandler(io.Discard, nil)), lim: config.DefaultLimits()}
	b := &Browser{m: m, StartedAt: time.Now()}
	return &Group{ID: "g", Browser: b, m: m, control: Control{Owner: OwnerAgent}, wake: make(chan struct{})}
}

func codeOf(err error) int {
	var we *wire.Error
	if errors.As(err, &we) {
		return we.Code
	}
	return 0
}

func TestGate(t *testing.T) {
	g := testGroup()
	ctx := context.Background()
	if err := g.Gate(ctx, nil, time.Second); err != nil {
		t.Fatalf("the agent while it drives: %v", err)
	}
	if _, err := g.SetControl(OwnerHuman, "", 0, ""); codeOf(err) != wire.CodeInvalidParams {
		t.Fatalf("human control without a holder: %v", err)
	}
	if _, err := g.SetControl(OwnerHuman, "c1", time.Minute, ""); err != nil {
		t.Fatal(err)
	}
	start := time.Now()
	if err := g.Gate(ctx, nil, 100*time.Millisecond); codeOf(err) != CodeHumanDriving {
		t.Fatalf("the agent while a person drives: %v", err)
	}
	if time.Since(start) < 100*time.Millisecond {
		t.Fatal("the agent was refused without waiting")
	}
	if err := g.Gate(ctx, &Origin{Actor: ActorOperator}, time.Millisecond); err != nil {
		t.Fatalf("the operator: %v", err)
	}
	// Given back while the agent waits: the call goes through.
	go func() {
		time.Sleep(50 * time.Millisecond)
		_, _ = g.SetControl(OwnerAgent, "", 0, "")
	}()
	if err := g.Gate(ctx, nil, 5*time.Second); err != nil {
		t.Fatalf("after control came back: %v", err)
	}
	if _, err := g.SetControl(OwnerPaused, "", 0, "reading"); err != nil {
		t.Fatal(err)
	}
	if err := g.Gate(ctx, nil, 5*time.Second); codeOf(err) != CodePaused {
		t.Fatalf("paused: %v", err)
	}
	if err := g.Gate(ctx, &Origin{Actor: "someone"}, time.Second); err == nil {
		t.Fatal("an unknown actor passed")
	}
}

func TestHumanGrantExpiresAndRenews(t *testing.T) {
	g := testGroup()
	if _, err := g.SetControl(OwnerHuman, "c1", 150*time.Millisecond, ""); err != nil {
		t.Fatal(err)
	}
	if !g.RenewHuman("c1", 0) || g.RenewHuman("c2", 0) {
		t.Fatal("renewal by the holder only")
	}
	time.Sleep(300 * time.Millisecond)
	if c := g.Control(); c.Owner != OwnerAgent {
		t.Fatalf("after the grant ran out: %+v", c)
	}
}

func TestControlView(t *testing.T) {
	c := Control{Owner: OwnerHuman, Holder: "c1", Until: time.UnixMilli(5000)}
	if v := c.View("c1"); v["holder"] != "you" || v["until"] != int64(5000) {
		t.Fatalf("to the holder: %v", v)
	}
	if v := c.View("c2"); v["holder"] != "other" {
		t.Fatalf("to another: %v", v)
	}
	if v := c.View(""); v["holder"] != "c1" {
		t.Fatalf("to the host: %v", v)
	}
	if v := (Control{Owner: OwnerAgent}).View("c1"); v["holder"] != nil || v["until"] != nil {
		t.Fatalf("agent: %v", v)
	}
}

func TestCheckURL(t *testing.T) {
	for u, want := range map[string]int{
		"https://example.com/":       0,
		"http://127.0.0.1:8103/x":    0,
		"about:blank":                0,
		"file:///etc/passwd":         wire.CodeForbidden,
		"data:text/html,<p>":         wire.CodeForbidden,
		"blob:https://example.com/x": wire.CodeForbidden,
		"javascript:alert(1)":        wire.CodeForbidden,
		"chrome://settings":          wire.CodeForbidden,
		"view-source:https://a.b/":   wire.CodeForbidden,
		"example.com":                wire.CodeInvalidParams,
		"https:///nohost":            wire.CodeInvalidParams,
	} {
		if got := codeOf(CheckURL(u, ActorAgent)); got != want {
			t.Errorf("%s: %d, want %d", u, got, want)
		}
	}
}

func TestUserAgentDropsHeadless(t *testing.T) {
	ua, meta := userAgent("HeadlessChrome/151.0.7922.34", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/151.0.0.0 Safari/537.36")
	if ua != "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36" {
		t.Fatalf("ua: %s", ua)
	}
	if meta["platform"] != "Linux" || meta["fullVersion"] != "151.0.7922.34" {
		t.Fatalf("hints: %v", meta)
	}
}

func TestValidID(t *testing.T) {
	for id, ok := range map[string]bool{"g1": true, "project-a_b": true, "": false, "a/b": false, "..": false,
		"x:y": false, string(make([]byte, 65)): false} {
		if ValidID(id) != ok {
			t.Errorf("%q", id)
		}
	}
}
