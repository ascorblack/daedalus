package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestParsePairingURLTakesTheLastAddress(t *testing.T) {
	if got := parsePairingURL(" http://127.0.0.1:8765/app/#pair=abc \n"); got != "http://127.0.0.1:8765/app/#pair=abc" {
		t.Fatalf("got %q", got)
	}
	if got := parsePairingURL("stale\nhttp://127.0.0.1:8765/p/one\nhttp://127.0.0.1:8765/p/two\n"); got != "http://127.0.0.1:8765/p/two" {
		t.Fatalf("got %q", got)
	}
	if got := parsePairingURL("no link here\n"); got != "" {
		t.Fatalf("invented a link: %q", got)
	}
}

func TestPairingFromLogsPrefersTheNewestLine(t *testing.T) {
	logs := strings.Join([]string{
		"daedalus-1  | starting",
		"daedalus-1  | pairing link: http://127.0.0.1:8765/p/first",
		"daedalus-1  | restarting",
		"daedalus-1  | Pairing link: http://127.0.0.1:8765/p/second.",
	}, "\n")
	if got := pairingFromLogs(logs); got != "http://127.0.0.1:8765/p/second" {
		t.Fatalf("got %q", got)
	}
	if got := pairingFromLogs("daedalus-1  | nothing about pairing\n"); got != "" {
		t.Fatalf("invented a link: %q", got)
	}
}

func TestWaitReadyPollsUntilTheAppAnswers(t *testing.T) {
	var hits atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if hits.Add(1) < 3 {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()
	if err := WaitReady(context.Background(), portOf(t, server.URL), 20*time.Second); err != nil {
		t.Fatal(err)
	}
	if hits.Load() < 3 {
		t.Fatalf("returned after %d answers", hits.Load())
	}
}

func TestWaitReadySaysWhatItLastSaw(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	}))
	defer server.Close()
	err := WaitReady(context.Background(), portOf(t, server.URL), 3*time.Second)
	if err == nil {
		t.Fatal("a 404 is not ready")
	}
	if !strings.Contains(err.Error(), "404") {
		t.Fatalf("the error hides what answered: %v", err)
	}
}

func TestAppURLIsLoopbackOnly(t *testing.T) {
	if got := AppURL("8765"); got != "http://127.0.0.1:8765/app/" {
		t.Fatalf("got %q", got)
	}
}

func portOf(t *testing.T, raw string) string {
	t.Helper()
	parsed, err := url.Parse(raw)
	if err != nil {
		t.Fatal(err)
	}
	return parsed.Port()
}
