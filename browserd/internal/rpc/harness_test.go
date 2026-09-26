package rpc_test

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/chrome"
	"github.com/ascorblack/daedalus/browserd/internal/config"
	"github.com/ascorblack/daedalus/browserd/internal/rpc"
	"github.com/ascorblack/daedalus/browserd/internal/view"
	"github.com/ascorblack/daedalus/browserd/internal/wire"
	"github.com/ascorblack/daedalus/ptyd/proto/events"
	"github.com/ascorblack/daedalus/ptyd/proto/server"
	"github.com/ascorblack/daedalus/ptyd/proto/server/clienttest"
	protowire "github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// needChromium skips a test that drives a real browser where there is none, and fails it where the
// gate says there must be one (BROWSERD_REQUIRE_CHROMIUM=1, set in the gate's image).
func needChromium(t *testing.T) {
	t.Helper()
	if _, ok := chrome.Find(""); ok {
		return
	}
	if os.Getenv("BROWSERD_REQUIRE_CHROMIUM") != "" {
		t.Fatal("no Chromium, and the gate requires one")
	}
	t.Skip("no Chromium here; the gate image has one")
}

// fixture is the local site the tests browse. Nothing leaves the machine: every page is served here
// and no page links outside it.
func fixture(t *testing.T) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	page := func(path, title, body string) {
		mux.HandleFunc(path, func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			fmt.Fprintf(w, "<!doctype html><html><head><title>%s</title></head><body style=\"margin:0;font:16px sans-serif\">%s</body></html>", title, body)
		})
	}
	page("/still", "Still", "<h1>Still</h1><p>"+strings.Repeat("Nothing moves on this page. ", 50)+"</p>")
	page("/anim", "Anim", `<div style="position:fixed;inset:0;background:conic-gradient(red,yellow,lime,aqua,blue,magenta,red);animation:s 2s linear infinite"></div><style>@keyframes s{to{transform:rotate(360deg)}}</style>`)
	page("/button", "Button", `<button id="b" style="position:absolute;left:100px;top:100px;width:200px;height:60px" onclick="document.title='clicked '+event.isTrusted">Press</button>`)
	page("/popup", "Popup", `<a id="a" href="/still" target="_blank" style="position:absolute;left:100px;top:100px;width:200px;height:60px;display:block">open</a>`)
	page("/dialog", "Dialog", `<button id="b" style="position:absolute;left:100px;top:100px;width:200px;height:60px" onclick="document.title=confirm('Leave?')?'yes':'no'">Ask</button>`)
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv
}

type harness struct {
	t       *testing.T
	client  *clienttest.Client
	manager *browser.Manager
	site    *httptest.Server
	stop    chan struct{}
}

// start runs the daemon in this process on a temporary run directory, with limits changed by edit.
func start(t *testing.T, edit func(*config.Limits)) *harness {
	t.Helper()
	needChromium(t)
	dir := t.TempDir()
	cfg := &config.Config{Env: "test", RunDir: filepath.Join(dir, "run"), StateDir: filepath.Join(dir, "state"),
		Listen: "unix", Limits: config.DefaultLimits()}
	if edit != nil {
		edit(&cfg.Limits)
	}
	if err := os.MkdirAll(cfg.StateDir, 0o700); err != nil {
		t.Fatal(err)
	}
	ep, err := server.Prepare(cfg.RunDir, "unix", "browserd")
	if err != nil {
		t.Fatal(err)
	}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	if os.Getenv("BROWSERD_TEST_LOG") != "" {
		log = slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelDebug}))
	}
	evlog := events.NewLog(config.EventRingSize)
	deb := events.NewDebouncer(evlog, map[string]events.Policy{"tab.updated": {Window: 250 * time.Millisecond, Coalesce: true}})
	var hub *view.Hub
	m := browser.New(browser.Deps{Config: cfg, Log: log, Events: deb, Busy: func(b *browser.Browser) bool { return hub != nil && hub.Busy(b) }})
	hub = view.New(m, evlog, log)
	m.Listen(hub)
	d := &rpc.Daemon{Config: cfg, Instance: "test", StartedAt: time.Now(), Manager: m, Hub: hub, Events: evlog, Log: log}
	srv := server.New(ep.Token, log, d.Hello)
	d.Register(srv)
	h := &harness{t: t, manager: m, site: fixture(t), stop: make(chan struct{})}
	go hub.Run(h.stop)
	go m.RunTitles(h.stop)
	go func() { _ = srv.Serve(ep.Listener) }()
	c, err := clienttest.Dial(cfg.RunDir)
	if err != nil {
		t.Fatal(err)
	}
	h.client = c
	t.Cleanup(func() {
		c.Close()
		close(h.stop)
		m.Shutdown()
		ep.Release()
		_ = ep.Listener.Close()
		srv.Close()
	})
	return h
}

func (h *harness) call(method string, params, result any) error {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	return h.client.Call(ctx, method, params, result)
}

func (h *harness) must(method string, params, result any) {
	h.t.Helper()
	if err := h.call(method, params, result); err != nil {
		h.t.Fatalf("%s: %v", method, err)
	}
}

// code is the daemon's error code of err, or 0.
func code(err error) int {
	var we *protowire.Error
	if errors.As(err, &we) {
		return we.Code
	}
	return 0
}

type opened struct {
	Group struct {
		ID string `json:"id"`
	} `json:"group"`
	Tab struct {
		ID    string `json:"id"`
		URL   string `json:"url"`
		Title string `json:"title"`
	} `json:"tab"`
	Created bool `json:"created"`
}

func (h *harness) open(group, profile, path string) opened {
	h.t.Helper()
	var o opened
	params := map[string]any{"group_id": group, "profile": profile}
	if path != "" {
		params["url"] = h.site.URL + path
	}
	h.must("browser.open", params, &o)
	return o
}

// view is one live view read straight off the socket's channel.
type viewConn struct {
	h       *harness
	channel uint32
	id      string
	frames  chan wire.Frame
	events  chan map[string]any
}

// attach opens a view and sends its ATTACH. Every frame the channel carries is decoded and sorted
// into frames and events; a test reads what it needs.
func (h *harness) attach(group string, att wire.Attach) *viewConn {
	h.t.Helper()
	var r struct {
		Channel  uint32 `json:"channel"`
		ClientID string `json:"client_id"`
	}
	h.must("view.attach", map[string]any{"group_id": group, "client": map[string]any{"kind": "human", "label": "test"}}, &r)
	v := &viewConn{h: h, channel: r.Channel, id: r.ClientID, frames: make(chan wire.Frame, 1024), events: make(chan map[string]any, 1024)}
	demux(h.client).add(r.Channel, v)
	b, err := wire.EncodeJSON(wire.TypeAttach, att)
	if err != nil {
		h.t.Fatal(err)
	}
	if err := h.client.Send(r.Channel, b); err != nil {
		h.t.Fatal(err)
	}
	return v
}

func (v *viewConn) ack(n uint32) {
	if err := v.h.client.Send(v.channel, wire.EncodeAck(n)); err != nil {
		v.h.t.Fatal(err)
	}
}

func (v *viewConn) input(in map[string]any) {
	b, _ := json.Marshal(in)
	if err := v.h.client.Send(v.channel, append([]byte{wire.TypeInput}, b...)); err != nil {
		v.h.t.Fatal(err)
	}
}

// frame waits for the next frame.
func (v *viewConn) frame(timeout time.Duration) (wire.Frame, bool) {
	select {
	case f := <-v.frames:
		return f, true
	case <-time.After(timeout):
		return wire.Frame{}, false
	}
}

// waitEvent waits for an event of type typ that passes ok.
func (v *viewConn) waitEvent(typ string, timeout time.Duration, ok func(map[string]any) bool) map[string]any {
	v.h.t.Helper()
	deadline := time.After(timeout)
	for {
		select {
		case e := <-v.events:
			if e["type"] == typ && (ok == nil || ok(e)) {
				return e
			}
		case <-deadline:
			v.h.t.Fatalf("no %s event within %s", typ, timeout)
			return nil
		}
	}
}

// demuxer reads the client's channel frames once and hands each channel's to its view.
type demuxer struct {
	views chan registration
}

type registration struct {
	channel uint32
	v       *viewConn
}

var demuxers = map[*clienttest.Client]*demuxer{}

func demux(c *clienttest.Client) *demuxer {
	if d := demuxers[c]; d != nil {
		return d
	}
	d := &demuxer{views: make(chan registration, 16)}
	demuxers[c] = d
	go func() {
		views := map[uint32]*viewConn{}
		var early []protowire.Frame
		for {
			select {
			case r := <-d.views:
				views[r.channel] = r.v
				rest := early[:0]
				for _, f := range early {
					if f.Channel == r.channel {
						deliver(r.v, f.Payload)
					} else {
						rest = append(rest, f)
					}
				}
				early = rest
			case f, ok := <-c.Frames:
				if !ok {
					return
				}
				if v := views[f.Channel]; v != nil {
					deliver(v, f.Payload)
				} else {
					early = append(early, f)
				}
			case <-c.Closed():
				return
			}
		}
	}()
	return d
}

func (d *demuxer) add(channel uint32, v *viewConn) { d.views <- registration{channel, v} }

func deliver(v *viewConn, payload []byte) {
	if len(payload) == 0 {
		close(v.events)
		return
	}
	f, err := wire.Decode(payload)
	if err != nil {
		return
	}
	switch f.Type {
	case wire.TypeFrame:
		select {
		case v.frames <- f:
		default:
		}
	case wire.TypeEvent:
		var e map[string]any
		if json.Unmarshal(f.JSON, &e) == nil {
			select {
			case v.events <- e:
			default:
			}
		}
	}
}
