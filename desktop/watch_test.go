package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"testing/iotest"
	"time"
)

// A stream arrives in whatever pieces the network cuts it into, one byte at a time at worst, and a
// frame split in the middle of a line has to come out whole.
func TestFramesSurviveBeingCutAnywhere(t *testing.T) {
	stream := ": keepalive\n\n" +
		"event: hello\ndata: {\"head\":4}\n\n" +
		"id: 5\r\nevent: notify\r\ndata: {\"a\":1}\r\n\r\n" +
		"data:first\ndata: second\n\n" +
		"event: empty\n\n" +
		"id: 7\nevent: notify\ndata: {\"b\":2}\n\n" +
		"event: notify\ndata: {\"c\":3}\n\n"
	var got []string
	err := parseFrames(iotest.OneByteReader(strings.NewReader(stream)), func(event, id, data string) {
		got = append(got, event+"|"+id+"|"+data)
	})
	if err != nil {
		t.Fatal(err)
	}
	want := []string{
		`hello||{"head":4}`,
		`notify|5|{"a":1}`,
		"message||first\nsecond",
		`notify|7|{"b":2}`,
		// A frame without an id of its own reports none: the caller keeps the cursor.
		`notify||{"c":3}`,
	}
	if strings.Join(got, "\n~\n") != strings.Join(want, "\n~\n") {
		t.Fatalf("frames read as\n%q\nwant\n%q", got, want)
	}
}

// A frame cut off by the end of the stream is not dispatched half-read, and a broken reader's error
// is handed back so the watcher reconnects.
func TestAnUnfinishedFrameIsNotDispatched(t *testing.T) {
	calls := 0
	if err := parseFrames(strings.NewReader("event: notify\ndata: {\"a\""), func(string, string, string) { calls++ }); err != nil {
		t.Fatal(err)
	}
	if calls != 0 {
		t.Fatal("a frame with no blank line after it was dispatched")
	}
	broken := io.MultiReader(strings.NewReader("data: x\n\n"), iotest.ErrReader(io.ErrUnexpectedEOF))
	if err := parseFrames(broken, func(string, string, string) {}); err == nil {
		t.Fatal("a broken stream ended as if it had closed cleanly")
	}
}

// notifyFrame is one notify event as the app writes it.
func notifyFrame(seq int64, at time.Time, desktop bool, title, link, session string) string {
	payload := map[string]any{
		"seq": seq, "at": at.UTC().Format("2006-01-02T15:04:05.000Z"), "type": "notify",
		"session_id": session,
		"payload": map[string]any{
			"notification": map[string]any{"id": seq * 10, "title": title, "body": "about " + title, "link": link, "session_id": session},
			"toast":        true,
			"deliver":      map[string]any{"push": false, "desktop": desktop, "telegram": false},
			"merged":       false,
			"summary":      map[string]any{"unseen": 1, "needs_you": 0},
		},
	}
	data, _ := json.Marshal(payload)
	return fmt.Sprintf("id: %d\nevent: notify\ndata: %s\n\n", seq, data)
}

// The whole round: the app's own header, a replay after the launcher was away folded into one line,
// a live notification shown with where it goes, what the router did not mark for the desktop and a
// repeated event left alone, and a reconnect that carries the cursor.
func TestTheWatcherShowsWhatTheAppMarksForTheDesktop(t *testing.T) {
	now := time.Now()
	var mu sync.Mutex
	var cursors []string
	var queries []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Daedalus-Token") != "the-token" || r.Header.Get("Authorization") != "" {
			http.Error(w, "unauthorised", http.StatusUnauthorized)
			return
		}
		mu.Lock()
		cursors = append(cursors, r.Header.Get("Last-Event-ID"))
		queries = append(queries, r.URL.RawQuery)
		round := len(cursors)
		mu.Unlock()
		w.Header().Set("Content-Type", "text/event-stream")
		flusher := w.(http.Flusher)
		fmt.Fprintf(w, "event: hello\ndata: {\"head\":9,\"server_time\":%q}\n\n", now.UTC().Format(time.RFC3339Nano))
		if round == 1 {
			// The replay: two for the desktop from ten minutes ago, one the router kept off it.
			fmt.Fprint(w, notifyFrame(3, now.Add(-10*time.Minute), true, "old one", "/app/agents/s1", "s1"))
			fmt.Fprint(w, notifyFrame(4, now.Add(-9*time.Minute), false, "not for the desktop", "", ""))
			fmt.Fprint(w, notifyFrame(5, now.Add(-8*time.Minute), true, "old two", "", ""))
			flusher.Flush()
			// Live.
			fmt.Fprint(w, notifyFrame(6, now, true, "Finished", "/app/agents/a1b2", "a1b2"))
			fmt.Fprint(w, notifyFrame(6, now, true, "Finished", "/app/agents/a1b2", "a1b2"))
			fmt.Fprint(w, notifyFrame(7, now, false, "quiet", "/app/agents/a1b2", "a1b2"))
			fmt.Fprint(w, ": keepalive\n\n")
			flusher.Flush()
			return // the app restarts
		}
		fmt.Fprint(w, notifyFrame(8, now, true, "Task moved", "/app/board/t9", ""))
		flusher.Flush()
		<-r.Context().Done()
	}))
	defer server.Close()

	shown := make(chan Notification, 16)
	w := &watcher{
		base: server.URL, token: "the-token", client: "launcher-42", lang: LangEN,
		show: func(n Notification) { shown <- n }, log: func(string, ...any) {},
		backoff: 10 * time.Millisecond,
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { w.run(ctx); close(done) }()

	var got []Notification
	for len(got) < 3 {
		select {
		case n := <-shown:
			got = append(got, n)
		case <-time.After(10 * time.Second):
			t.Fatalf("only %d notifications were shown: %+v", len(got), got)
		}
	}
	cancel()
	<-done
	select {
	case extra := <-shown:
		t.Fatalf("something was shown twice or should not have been shown: %+v", extra)
	default:
	}

	if got[0].Body != "2 notifications while the launcher was away" || got[0].Link != server.URL+"/app/inbox" {
		t.Fatalf("the replay was not folded into one line: %+v", got[0])
	}
	if got[1].Title != "Finished" || got[1].Link != server.URL+"/app/agents/a1b2" || got[1].Session != "a1b2" || got[1].Action != "Open" {
		t.Fatalf("the live notification reads %+v", got[1])
	}
	if got[2].Title != "Task moved" || got[2].Link != server.URL+"/app/board/t9" || got[2].Session != "" {
		t.Fatalf("the notification after the reconnect reads %+v", got[2])
	}
	mu.Lock()
	defer mu.Unlock()
	if len(cursors) < 2 || cursors[0] != "" || cursors[1] != "7" {
		t.Fatalf("Last-Event-ID went as %q, want none and then 7", cursors)
	}
	if queries[0] != "types=notify&kind=launcher&client=launcher-42" {
		t.Fatalf("the stream was asked for with %q", queries[0])
	}
}

// A replay with nothing live after it is still told, once its burst is over.
func TestAReplayAloneIsFoldedAfterItEnds(t *testing.T) {
	now := time.Now()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprintf(w, "event: hello\ndata: {\"server_time\":%q}\n\n", now.UTC().Format(time.RFC3339Nano))
		fmt.Fprint(w, notifyFrame(3, now.Add(-time.Hour), true, "old", "", ""))
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	}))
	defer server.Close()
	shown := make(chan Notification, 4)
	w := &watcher{base: server.URL, token: "t", client: "launcher-1", lang: LangRU, show: func(n Notification) { shown <- n }, log: func(string, ...any) {}}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go w.run(ctx)
	select {
	case n := <-shown:
		if n.Body != "1 уведомление, пока лаунчер был закрыт" {
			t.Fatalf("the away line reads %q", n.Body)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("a replay with nothing after it was never told")
	}
}

// The age of an event is measured on the app's clock, which the hello carries: a desktop whose clock
// runs five minutes ahead does not take every live notification for a replay.
func TestAgeIsMeasuredOnTheAppsClock(t *testing.T) {
	appNow := time.Date(2026, 9, 25, 10, 0, 0, 0, time.UTC)
	var shown []Notification
	w := &watcher{base: "http://127.0.0.1:1", lang: LangEN, show: func(n Notification) { shown = append(shown, n) }, now: func() time.Time { return appNow.Add(5 * time.Minute) }}
	w.hello(fmt.Sprintf(`{"server_time":%q}`, appNow.Format(time.RFC3339Nano)))
	frame := notifyFrame(1, appNow, true, "live", "", "")
	data := frame[strings.Index(frame, "data: ")+6 : len(frame)-2]
	if w.notify(data) || len(shown) != 1 {
		t.Fatalf("a live notification was taken for a replay: %+v", shown)
	}
}

// Only a path inside the app becomes a click target: the path came over the network.
func TestALinkStaysInsideTheApp(t *testing.T) {
	w := &watcher{base: "http://127.0.0.1:8765"}
	cases := map[[2]string]string{
		{"/app/agents/s1", "s1"}:           "http://127.0.0.1:8765/app/agents/s1",
		{"", "s1"}:                         "http://127.0.0.1:8765/app/agents/s1",
		{"", ""}:                           "",
		{"https://elsewhere.test/", ""}:    "",
		{"//elsewhere.test/app/", ""}:      "",
		{"/app/../api/settings", ""}:       "",
		{"/api/settings", ""}:              "",
		{"", "s1/../../x"}:                 "",
		{"/app//elsewhere.test", "s1"}:     "",
		{"/app/board/t9", "not a;session"}: "http://127.0.0.1:8765/app/board/t9",
	}
	for in, want := range cases {
		if got := w.link(in[0], in[1]); got != want {
			t.Errorf("link(%q, %q) = %q, want %q", in[0], in[1], got, want)
		}
	}
	if sessionOf("/app/board/t9", "s1") != "" || sessionOf("/app/agents/s1", "s1") != "s1" || sessionOf("", "s1") != "s1" {
		t.Fatal("a session link was not told apart from another link")
	}
}

// Three Russian plural forms, and the two English ones.
func TestTheAwayLineCountsInBothLanguages(t *testing.T) {
	cases := []struct {
		lang  Lang
		count int
		want  string
	}{
		{LangEN, 1, "1 notification while"},
		{LangEN, 3, "3 notifications while"},
		{LangRU, 1, "1 уведомление,"},
		{LangRU, 21, "21 уведомление,"},
		{LangRU, 3, "3 уведомления,"},
		{LangRU, 12, "12 уведомлений,"},
		{LangRU, 5, "5 уведомлений,"},
		{LangRU, 111, "111 уведомлений,"},
	}
	for _, c := range cases {
		if got := awayLine(c.lang, c.count); !strings.HasPrefix(got, c.want) {
			t.Errorf("%s %d reads %q", c.lang, c.count, got)
		}
	}
}

// The memory of what was shown is bounded, and forgets the oldest first.
func TestTheMemoryOfShownEventsIsBounded(t *testing.T) {
	var r recentIDs
	for id := int64(1); id <= watchRemembered+10; id++ {
		if !r.add(id) {
			t.Fatalf("%d was taken for a repeat", id)
		}
	}
	if r.add(watchRemembered + 10) {
		t.Fatal("a repeat was taken for new")
	}
	if !r.add(1) {
		t.Fatal("the oldest id was never forgotten")
	}
	if len(r.order) != watchRemembered || len(r.set) != watchRemembered {
		t.Fatalf("the memory holds %d and %d", len(r.order), len(r.set))
	}
}
