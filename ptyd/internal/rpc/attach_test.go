package rpc_test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"runtime"
	"strconv"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/emulator/basic"
	"github.com/ascorblack/daedalus/ptyd/internal/server/clienttest"
	"github.com/ascorblack/daedalus/ptyd/internal/term"
	"github.com/ascorblack/daedalus/ptyd/internal/wire"
)

type attached struct {
	Channel  uint32 `json:"channel"`
	ClientID string `json:"client_id"`
}

// nextFrame reads the next frame of channel ch, skipping other channels' frames.
func nextFrame(t *testing.T, c *clienttest.Client, ch uint32) wire.Frame {
	t.Helper()
	deadline := time.After(10 * time.Second)
	for {
		select {
		case f := <-c.Frames:
			if f.Channel == ch {
				return f
			}
		case <-deadline:
			t.Fatalf("no frame on channel %d", ch)
		}
	}
}

// untilEvent reads browser frames of ch until an EVENT of type typ, returning it and whatever came
// before it.
func untilEvent(t *testing.T, c *clienttest.Client, ch uint32, typ string) (map[string]any, []wire.BrowserFrame) {
	t.Helper()
	var before []wire.BrowserFrame
	for {
		f := nextFrame(t, c, ch)
		if len(f.Payload) == 0 {
			t.Fatalf("channel %d closed waiting for %s", ch, typ)
		}
		bf, err := wire.DecodeBrowser(f.Payload)
		if err != nil {
			t.Fatalf("a malformed frame: %v", err)
		}
		if bf.Type == wire.TypeEvent {
			var m map[string]any
			_ = json.Unmarshal(bf.Data, &m)
			if m["type"] == typ {
				return m, before
			}
		}
		before = append(before, bf)
	}
}

func TestAttachOverTheSocket(t *testing.T) {
	f := start(t)
	f.call(t, "terminal.create", map[string]any{"id": "att", "argv": []string{"cat"}}, nil)
	var a attached
	f.call(t, "terminal.attach", map[string]any{"id": "att", "client": map[string]any{"kind": "human", "label": "laptop", "via": "web"}}, &a)
	if a.Channel == 0 || a.ClientID == "" {
		t.Fatalf("attach result %+v", a)
	}
	frame, _ := wire.EncodeAttach(wire.Attach{})
	if err := f.client.Send(a.Channel, frame); err != nil {
		t.Fatal(err)
	}
	hello, _ := untilEvent(t, f.client, a.Channel, "hello")
	if hello["client_id"] != a.ClientID || hello["window_bytes"] == nil {
		t.Fatalf("hello %v", hello)
	}
	if resync, _ := untilEvent(t, f.client, a.Channel, "resync"); resync["reason"] != "attach" {
		t.Fatalf("resync %v", resync)
	}
	if err := f.client.Send(a.Channel, wire.EncodeInput([]byte("typed here\r"))); err != nil {
		t.Fatal(err)
	}
	var got []byte
	for !bytes.Contains(got, []byte("typed here\r\ntyped here")) {
		fr := nextFrame(t, f.client, a.Channel)
		if bf, err := wire.DecodeBrowser(fr.Payload); err == nil && bf.Type == wire.TypeOutput {
			got = append(got, bf.Data...)
		}
	}
	var info term.Info
	f.call(t, "terminal.get", map[string]any{"id": "att"}, &info)
	if len(info.Clients) != 1 || info.Clients[0].ID != a.ClientID || info.Clients[0].Via != "web" || info.LastHumanInputAt == nil {
		t.Fatalf("info %+v", info)
	}

	// Detaching closes the channel from the daemon's side, with an empty frame.
	f.call(t, "terminal.detach", map[string]any{"channel": a.Channel}, nil)
	for {
		fr := nextFrame(t, f.client, a.Channel)
		if len(fr.Payload) == 0 {
			break
		}
	}
	f.call(t, "terminal.get", map[string]any{"id": "att"}, &info)
	if len(info.Clients) != 0 || info.LastDetachAt == nil {
		t.Fatalf("after detach %+v", info)
	}
	if e := f.callErr("terminal.detach", map[string]any{"channel": a.Channel}); e == nil || e.Code != wire.CodeNotFound {
		t.Fatalf("detaching twice: %v", e)
	}
}

func TestTheHostClosingTheChannelDetaches(t *testing.T) {
	f := start(t)
	f.call(t, "terminal.create", map[string]any{"id": "peer", "argv": []string{"cat"}}, nil)
	var a attached
	f.call(t, "terminal.attach", map[string]any{"id": "peer", "client": map[string]any{}}, &a)
	frame, _ := wire.EncodeAttach(wire.Attach{})
	_ = f.client.Send(a.Channel, frame)
	untilEvent(t, f.client, a.Channel, "hello")
	if err := f.client.Send(a.Channel, nil); err != nil {
		t.Fatal(err)
	}
	// The daemon answers the close with its own, and nothing after it.
	for {
		fr := nextFrame(t, f.client, a.Channel)
		if len(fr.Payload) == 0 {
			break
		}
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		var info term.Info
		f.call(t, "terminal.get", map[string]any{"id": "peer"}, &info)
		if len(info.Clients) == 0 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("still attached: %+v", info.Clients)
		}
		time.Sleep(10 * time.Millisecond)
	}
	select {
	case fr := <-f.client.Frames:
		t.Fatalf("a frame after the close: channel %d, %d bytes", fr.Channel, len(fr.Payload))
	case <-time.After(100 * time.Millisecond):
	}
}

func TestAttachAndKeyboardValidation(t *testing.T) {
	f := start(t)
	f.call(t, "terminal.create", map[string]any{"id": "val", "argv": []string{"cat"}}, nil)
	for _, c := range []struct {
		method string
		params map[string]any
		code   int
	}{
		{"terminal.attach", map[string]any{"id": "nope", "client": map[string]any{}}, wire.CodeNotFound},
		{"terminal.attach", map[string]any{"id": "val", "client": map[string]any{"kind": "robot"}}, wire.CodeInvalidParams},
		{"terminal.attach", map[string]any{"id": "val", "client": map[string]any{"colour": "red"}}, wire.CodeInvalidParams},
		{"terminal.detach", map[string]any{"channel": 99}, wire.CodeNotFound},
		{"terminal.keyboard", map[string]any{"id": "val", "owner": "cat"}, wire.CodeInvalidParams},
		{"terminal.keyboard", map[string]any{"id": "val", "owner": "human", "ttl_ms": -1}, wire.CodeInvalidParams},
		{"terminal.keyboard", map[string]any{"id": "nope", "owner": "human"}, wire.CodeNotFound},
	} {
		if e := f.callErr(c.method, c.params); e == nil || e.Code != c.code {
			t.Errorf("%s %v: %v, want %d", c.method, c.params, e, c.code)
		}
	}
	var kb struct {
		Owner string    `json:"owner"`
		Until time.Time `json:"until"`
	}
	f.call(t, "terminal.keyboard", map[string]any{"id": "val", "owner": "human", "ttl_ms": 60000}, &kb)
	if kb.Owner != "human" || time.Until(kb.Until) < 50*time.Second {
		t.Fatalf("keyboard %+v", kb)
	}
	// A human holds the keyboard: an agent write that waits times out with keyboard_held.
	e := f.callErr("terminal.write", map[string]any{"id": "val", "text": "x", "origin": map[string]any{"kind": "agent", "actor": "t"},
		"timeout_ms": 100})
	if e == nil || e.Code != wire.CodeKeyboardHeld {
		t.Fatalf("write under a human grant: %v", e)
	}
	kb.Owner, kb.Until = "", time.Time{}
	f.call(t, "terminal.keyboard", map[string]any{"id": "val", "owner": "auto"}, &kb)
	if kb.Owner != "auto" || !kb.Until.IsZero() {
		t.Fatalf("keyboard %+v", kb)
	}
	var info map[string]any
	f.call(t, "daemon.info", nil, &info)
	limits := info["limits"].(map[string]any)
	for _, k := range []string{"window_bytes", "ack_bytes", "resync_backlog_bytes", "snapshot_scrollback", "max_clients"} {
		if limits[k] == nil {
			t.Errorf("daemon.info limits lack %s: %v", k, limits)
		}
	}
}

// A connection that disappears takes its attachments with it: no client, channel or goroutine is
// left behind.
func TestADroppedConnectionLeavesNothingBehind(t *testing.T) {
	f := start(t)
	f.call(t, "terminal.create", map[string]any{"id": "drop", "argv": []string{"cat"}}, nil)
	settle := func() int {
		runtime.GC()
		time.Sleep(50 * time.Millisecond)
		return runtime.NumGoroutine()
	}
	before := settle()
	other, err := clienttest.Dial(f.dir + "/run")
	if err != nil {
		t.Fatal(err)
	}
	for i := range 5 {
		var a attached
		if err := other.Call(t.Context(), "terminal.attach", map[string]any{"id": "drop", "client": map[string]any{"label": strconv.Itoa(i)}}, &a); err != nil {
			t.Fatal(err)
		}
		if i%2 == 0 {
			frame, _ := wire.EncodeAttach(wire.Attach{})
			_ = other.Send(a.Channel, frame)
		}
	}
	other.Close()
	deadline := time.Now().Add(10 * time.Second)
	for {
		var info term.Info
		f.call(t, "terminal.get", map[string]any{"id": "drop"}, &info)
		after := settle()
		if len(info.Clients) == 0 && after <= before {
			break
		}
		if time.Now().After(deadline) {
			buf := make([]byte, 1<<20)
			t.Fatalf("%d clients and %d goroutines left (%d before):\n%s", len(info.Clients), after, before,
				buf[:runtime.Stack(buf, true)])
		}
	}
}

// The acceptance run: two clients on bash, one of them stalled, while 200 MB go through the PTY. The
// stalled one must end up resynchronised and the other must hold every byte. It takes a while and
// is meant for a run under a memory cap: PTYD_ACCEPTANCE=1.
func TestAcceptanceTwoClientsOneStalled(t *testing.T) {
	if os.Getenv("PTYD_ACCEPTANCE") == "" {
		t.Skip("set PTYD_ACCEPTANCE=1 to run")
	}
	total := 200 << 20
	if v := os.Getenv("PTYD_ACCEPTANCE_BYTES"); v != "" {
		total, _ = strconv.Atoi(v)
	}
	f := startWith(t, basic.Factory)
	f.call(t, "terminal.create", map[string]any{"id": "bash", "argv": []string{"bash", "--noprofile", "--norc"},
		"env": map[string]string{"PS1": "$ "}}, nil)
	dial := func() *clienttest.Client {
		c, err := clienttest.Dial(f.dir + "/run")
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(c.Close)
		return c
	}
	if os.Getenv("PTYD_ACCEPTANCE_NO_CLIENTS") != "" {
		// The baseline: the same stream with nobody attached.
		began := time.Now()
		f.call(t, "terminal.write", map[string]any{"id": "bash", "text": fmt.Sprintf("yes | head -c %d; echo done-marker\r", total),
			"origin": map[string]any{"kind": "agent", "actor": "acceptance"}, "wait": "none"}, nil)
		f.waitOutputSince(t, "bash", "done-marker\n", int64(total))
		elapsed := time.Since(began)
		t.Logf("%d bytes through bash in %s (%.1f MB/s) with no client", total, elapsed, float64(total)/elapsed.Seconds()/1e6)
		return
	}
	stalledConn, fastConn := dial(), dial()
	attach := func(c *clienttest.Client, label string) uint32 {
		var a attached
		if err := c.Call(t.Context(), "terminal.attach", map[string]any{"id": "bash", "client": map[string]any{"label": label}}, &a); err != nil {
			t.Fatal(err)
		}
		frame, _ := wire.EncodeAttach(wire.Attach{})
		_ = c.Send(a.Channel, frame)
		return a.Channel
	}
	stalledCh := attach(stalledConn, "stalled")
	fastCh := attach(fastConn, "fast")

	// The fast client parses everything and acknowledges every 64 KiB, as a browser that keeps up.
	var fastAt atomic.Int64
	fastAt.Store(-1)
	type result struct {
		bytes, snapshots, frames int
		err                      error
	}
	done := make(chan result, 1)
	stop := make(chan struct{})
	go func() {
		var r result
		next, acked := int64(-1), int64(0)
		for {
			var fr wire.Frame
			select {
			case fr = <-fastConn.Frames:
			case <-stop:
				done <- r
				return
			}
			if fr.Channel != fastCh || len(fr.Payload) == 0 {
				continue
			}
			bf, err := wire.DecodeBrowser(fr.Payload)
			if err != nil {
				r.err = err
				continue
			}
			r.frames++
			switch bf.Type {
			case wire.TypeSnapshot:
				r.snapshots++
				next = int64(bf.Seq)
				_ = fastConn.Send(fastCh, wire.EncodeAck(bf.Seq))
				acked = next
			case wire.TypeOutput:
				if next >= 0 && int64(bf.Seq) != next && r.err == nil {
					r.err = fmt.Errorf("OUTPUT at %d, expected %d", bf.Seq, next)
				}
				next = int64(bf.Seq) + int64(len(bf.Data))
				r.bytes += len(bf.Data)
				if next-acked >= 64<<10 {
					_ = fastConn.Send(fastCh, wire.EncodeAck(uint64(next)))
					acked = next
				}
			}
			fastAt.Store(next)
		}
	}()

	var info term.Info
	began := time.Now()
	f.call(t, "terminal.write", map[string]any{"id": "bash", "text": fmt.Sprintf("yes | head -c %d; echo done-marker\r", total),
		"origin": map[string]any{"kind": "agent", "actor": "acceptance"}, "wait": "none"}, nil)
	// Stripped output has CR LF as LF; the command line's own echo is before `total`.
	f.waitOutputSince(t, "bash", "done-marker\n", int64(total))
	elapsed := time.Since(began)

	// The fast client catches up with the head, once the prompt after the marker has settled.
	deadline := time.Now().Add(time.Minute)
	var head int64
	for {
		f.call(t, "terminal.get", map[string]any{"id": "bash"}, &info)
		head = info.OutputSeq
		if fastAt.Load() >= head {
			time.Sleep(300 * time.Millisecond)
			f.call(t, "terminal.get", map[string]any{"id": "bash"}, &info)
			if info.OutputSeq == head && fastAt.Load() == head {
				break
			}
		}
		if time.Now().After(deadline) {
			t.Fatalf("the fast client is at %d of %d", fastAt.Load(), head)
		}
		time.Sleep(50 * time.Millisecond)
	}
	close(stop)
	fast := <-done

	// The stalled client: it holds its window and nothing more; when it acknowledges again, it is
	// resynchronised rather than sent the backlog.
	var stalledBytes, stalledSnapshots int
	var stalledSent int64
	drain := func() {
		for {
			select {
			case fr := <-stalledConn.Frames:
				if fr.Channel != stalledCh || len(fr.Payload) == 0 {
					continue
				}
				bf, _ := wire.DecodeBrowser(fr.Payload)
				switch bf.Type {
				case wire.TypeOutput:
					stalledBytes += len(bf.Data)
					stalledSent = int64(bf.Seq) + int64(len(bf.Data))
				case wire.TypeSnapshot:
					stalledSnapshots++
					stalledSent = int64(bf.Seq)
				}
			case <-time.After(300 * time.Millisecond):
				return
			}
		}
	}
	drain()
	windowed := stalledBytes
	_ = stalledConn.Send(stalledCh, wire.EncodeAck(uint64(stalledSent)))
	drain()

	t.Logf("%d bytes through bash in %s (%.1f MB/s); head %d", total, elapsed, float64(total)/elapsed.Seconds()/1e6, head)
	t.Logf("fast client: %d bytes in %d frames, %d snapshots, at %d, error %v", fast.bytes, fast.frames, fast.snapshots, fastAt.Load(), fast.err)
	t.Logf("stalled client: %d bytes before acknowledging, %d snapshots in all, %d bytes in all", windowed, stalledSnapshots, stalledBytes)
	if fast.err != nil || fast.snapshots != 1 || fastAt.Load() != head {
		t.Fatalf("the fast client lost bytes")
	}
	if windowed > 256<<10 || stalledSnapshots != 2 {
		t.Fatalf("the stalled client was not held to its window and resynchronised")
	}
}

// waitOutputSince polls raw output from since until it contains want.
func (f *fixture) waitOutputSince(t *testing.T, id, want string, since int64) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Minute)
	for {
		var out output
		var info term.Info
		f.call(t, "terminal.get", map[string]any{"id": id}, &info)
		from := max(since, info.OutputSeq-4096)
		f.call(t, "terminal.read_output", map[string]any{"id": id, "since_seq": from, "strip": true}, &out)
		if bytes.Contains([]byte(out.Data), []byte(want)) {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("output of %s never contained %q", id, want)
		}
		time.Sleep(100 * time.Millisecond)
	}
}

// The load estimate needs what a terminal costs inside the daemon too: its ring per terminal, and
// the daemon's own process, which holds every emulator.
func TestStatsCountTheDaemonsShare(t *testing.T) {
	f := start(t)
	f.call(t, "terminal.create", map[string]any{"id": "cost", "argv": []string{"sh", "-c", "yes | head -c 100000; exec cat"}}, nil)
	// Every byte read, not only the first: the ring grows as the output arrives.
	eventually(t, "the whole output read", func() bool {
		var info term.Info
		f.call(t, "terminal.get", map[string]any{"id": "cost"}, &info)
		return info.OutputSeq >= 100000
	})
	var s struct {
		Terminals []struct {
			ID          string `json:"id"`
			Processes   int    `json:"processes"`
			DaemonBytes int64  `json:"daemon_bytes"`
		} `json:"terminals"`
		Daemon struct {
			Pid      int   `json:"pid"`
			RSSBytes int64 `json:"rss_bytes"`
		} `json:"daemon"`
	}
	f.call(t, "terminal.stats", map[string]any{"ids": []string{"cost"}}, &s)
	if len(s.Terminals) != 1 || s.Terminals[0].DaemonBytes < 100000 || s.Terminals[0].Processes < 1 {
		t.Fatalf("stats %+v", s)
	}
	if s.Daemon.Pid != os.Getpid() || s.Daemon.RSSBytes <= 0 {
		t.Fatalf("daemon %+v", s.Daemon)
	}
}
