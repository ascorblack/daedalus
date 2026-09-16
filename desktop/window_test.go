package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestDeepLinkTargetFindsTheSession(t *testing.T) {
	app := "http://127.0.0.1:8765/app/"
	for _, one := range []struct {
		link string
		want string
	}{
		{"daedalus://open/abc123", "http://127.0.0.1:8765/app/agents/abc123"},
		{"daedalus://session/abc123", "http://127.0.0.1:8765/app/agents/abc123"},
		{"daedalus:///open/abc123", "http://127.0.0.1:8765/app/agents/abc123"},
		{"DAEDALUS://OPEN/abc123", "http://127.0.0.1:8765/app/agents/abc123"},
		// A link with nothing to open, and one that is not ours at all, both land on the app rather
		// than anywhere a link could aim the launcher.
		{"daedalus://open", app},
		{"daedalus://", app},
		{"https://example.invalid/app/agents/x", app},
		{"nonsense", app},
	} {
		if got := DeepLinkTarget(one.link, app); got != one.want {
			t.Fatalf("%s opened %s, want %s", one.link, got, one.want)
		}
	}
	// A link arrives from outside, so an id that is not one opens the app and nothing else.
	for _, refused := range []string{"daedalus://open/../../etc", "daedalus://open/a b", "daedalus://open/a%2Fb", "daedalus://open/a\u0000b"} {
		if got := DeepLinkTarget(refused, app); got != app {
			t.Fatalf("%s opened %s", refused, got)
		}
	}
}

func TestDeepLinkIsNotACommand(t *testing.T) {
	opts, err := parseArgs([]string{"daedalus://open/abc"})
	if err != nil || opts.command != "" || opts.link != "daedalus://open/abc" {
		t.Fatalf("%+v %v", opts, err)
	}
	if !IsDeepLink("daedalus://open/abc") || IsDeepLink("open") {
		t.Fatal("a link and a command are told apart by the scheme")
	}
}

func TestGeometryKeepsWhatIsUsable(t *testing.T) {
	paths := setupTempInstall(t)
	if got := ReadGeometry(paths); got != DefaultGeometry() {
		t.Fatalf("a first start opened %+v, want the default", got)
	}
	WriteGeometry(paths, Geometry{Width: 1400, Height: 900, X: 40, Y: 20})
	if got := ReadGeometry(paths); got.Width != 1400 || got.Height != 900 || got.X != 40 || got.Y != 20 {
		t.Fatalf("the window came back as %+v", got)
	}
	// A window reported as a sliver, or off the side of the world, keeps what it had.
	kept := Geometry{Width: 1400, Height: 900}.With(2, 2, -900, -900)
	if kept.Width != 1400 || kept.Height != 900 || kept.X != 0 || kept.Y != 0 {
		t.Fatalf("nonsense was believed: %+v", kept)
	}
	if err := os.WriteFile(geometryFile(paths), []byte("{not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	if got := ReadGeometry(paths); got != DefaultGeometry() {
		t.Fatalf("a broken file opened %+v, want the default", got)
	}
}

func TestSecondLaunchFocusesTheFirst(t *testing.T) {
	paths := setupTempInstall(t)
	if err := WriteSetup(paths, Setup{}); err != nil {
		t.Fatal(err)
	}
	// No launcher is running: there is nothing to focus and nothing to wait for.
	if FocusRunning(context.Background(), paths, "") {
		t.Fatal("an empty folder answered as a running launcher")
	}
	server := NewServer(NewApp(paths), 0)
	if err := server.Start(); err != nil {
		t.Fatal(err)
	}
	defer server.Stop(context.Background())

	focused := make(chan string, 1)
	server.OnFocus(func(_ context.Context, url string) { focused <- url })
	if !FocusRunning(context.Background(), paths, "daedalus://open/abc123") {
		t.Fatal("the running launcher did not answer")
	}
	select {
	case url := <-focused:
		if !strings.HasSuffix(url, "/app/agents/abc123") {
			t.Fatalf("the link arrived as %s", url)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the link never arrived")
	}

	// The file is the handover, and the token in it is what makes the answer ours. Without it the
	// endpoint refuses, so another program on the port cannot be mistaken for the launcher.
	var found instance
	data, err := os.ReadFile(filepath.Join(paths.Data, "launcher.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(data, &found); err != nil {
		t.Fatal(err)
	}
	if found.Port != server.Port() || found.Token != server.Token() {
		t.Fatalf("the handover names %d, the page listens on %d", found.Port, server.Port())
	}
	if got := post(t, server.URL()+"focus", map[string]string{csrfHeader: "not-the-token"}, nil); got != http.StatusForbidden {
		t.Fatalf("focus without the token answered %d", got)
	}
	server.Stop(context.Background())
	if _, err := os.Stat(filepath.Join(paths.Data, "launcher.json")); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("a stopped launcher still claims the installation")
	}
}

func TestFindChromiumLooksWhereTheInstallersPut(t *testing.T) {
	// PATH first: an operator who put a browser there means it.
	onPath := func(name string) (string, error) {
		if name == "google-chrome" {
			return "/opt/bin/google-chrome", nil
		}
		return "", errors.New("not on PATH")
	}
	if got := findChromium("linux", "/home/x", onPath, func(string) bool { return false }); got != "/opt/bin/google-chrome" {
		t.Fatalf("PATH was not tried first: %s", got)
	}
	// Then the install locations, which is the case that matters: a program started from Finder or
	// from Explorer has a PATH with no browser in it.
	none := func(string) (string, error) { return "", errors.New("not on PATH") }
	edge := "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
	if got := findChromium("darwin", "/Users/x", none, func(path string) bool { return path == edge }); got != edge {
		t.Fatalf("the installed browser was not found: %s", got)
	}
	if got := findChromium("windows", "", none, func(path string) bool { return strings.HasSuffix(path, `Google\Chrome\Application\chrome.exe`) }); got == "" {
		t.Fatal("chrome in Program Files was not found")
	}
	// None installed is not an error: the default browser is the last step of the chain.
	if got := findChromium("linux", "", none, func(string) bool { return false }); got != "" {
		t.Fatalf("a machine with no Chromium answered %s", got)
	}
}

func TestWatchAnnouncesOnlyWhatIsNew(t *testing.T) {
	base := "http://127.0.0.1:8765"
	waiting := map[string]bool{}
	unread := -1
	first := stackStatus{InboxUnread: 3}
	first.Sessions = append(first.Sessions, sessionLine("s1", "Reading the logs", "waiting"))
	// The first answer is a baseline: an installation that was already waiting does not announce it
	// the moment the launcher starts.
	if got := changes(first, &unread, waiting, base); len(got) != 0 {
		t.Fatalf("the first poll announced %+v", got)
	}
	second := stackStatus{InboxUnread: 5}
	second.Sessions = append(second.Sessions, sessionLine("s1", "Reading the logs", "waiting"), sessionLine("s2", "Rewriting the parser", "waiting"))
	got := changes(second, &unread, waiting, base)
	if len(got) != 2 {
		t.Fatalf("want the inbox and the new session, got %+v", got)
	}
	if !strings.Contains(got[0].Body, "2 new entries") {
		t.Fatalf("the inbox line reads %q", got[0].Body)
	}
	if !strings.Contains(got[1].Body, "Rewriting the parser") || !strings.HasSuffix(got[1].Link, "/app/agents/s2") {
		t.Fatalf("the waiting line reads %+v", got[1])
	}
	// Reading the inbox is not news, and a session that is still waiting is not news twice.
	if got := changes(stackStatus{InboxUnread: 1, Sessions: second.Sessions}, &unread, waiting, base); len(got) != 0 {
		t.Fatalf("nothing happened and it announced %+v", got)
	}
	// Once it has stopped waiting and waits again, it is news again.
	running := stackStatus{InboxUnread: 1}
	running.Sessions = append(running.Sessions, sessionLine("s2", "Rewriting the parser", "running"))
	changes(running, &unread, waiting, base)
	again := stackStatus{InboxUnread: 1}
	again.Sessions = append(again.Sessions, sessionLine("s2", "Rewriting the parser", "waiting"))
	if got := changes(again, &unread, waiting, base); len(got) != 1 {
		t.Fatalf("a session waiting again announced %+v", got)
	}
}

// sessionLine is one row of what /api/status says about the sessions.
func sessionLine(id, title, status string) struct {
	ID     string `json:"id"`
	Title  string `json:"title"`
	Status string `json:"status"`
} {
	return struct {
		ID     string `json:"id"`
		Title  string `json:"title"`
		Status string `json:"status"`
	}{ID: id, Title: title, Status: status}
}
