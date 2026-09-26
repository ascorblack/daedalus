package cdp

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"io"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeBrowser answers on the other end of a pipe: every command gets its id back with the method
// as its result, and a command named Fake.emit makes it send the events its params list first.
type fakeBrowser struct {
	in  *io.PipeReader // what the connection writes
	out *io.PipeWriter // what the connection reads

	mu   sync.Mutex
	seen []string
}

func newPair(t *testing.T, handler func(Event)) (*Conn, *fakeBrowser) {
	t.Helper()
	toR, toW := io.Pipe()
	fromR, fromW := io.Pipe()
	f := &fakeBrowser{in: toR, out: fromW}
	c := New(fromR, toW, handler)
	go f.serve()
	t.Cleanup(func() { fromW.Close(); toR.Close() })
	return c, f
}

func (f *fakeBrowser) serve() {
	r := bufio.NewReader(f.in)
	for {
		b, err := r.ReadBytes(0)
		if err != nil {
			return
		}
		var m message
		if json.Unmarshal(b[:len(b)-1], &m) != nil {
			continue
		}
		f.mu.Lock()
		f.seen = append(f.seen, m.Method)
		f.mu.Unlock()
		switch m.Method {
		case "Fake.emit":
			var events []string
			_ = json.Unmarshal(m.Params, &events)
			for _, e := range events {
				f.write(message{Method: e, SessionID: m.SessionID, Params: json.RawMessage(`{"n":1}`)})
			}
		case "Fake.fail":
			f.write(message{ID: m.ID, Error: &Error{Code: -32000, Message: "no"}})
			continue
		case "Fake.big":
			big, _ := json.Marshal(map[string]string{"data": strings.Repeat("x", 3<<20)})
			f.write(message{ID: m.ID, Result: big})
			continue
		case "Fake.never":
			continue
		}
		res, _ := json.Marshal(map[string]string{"method": m.Method, "session": m.SessionID})
		f.write(message{ID: m.ID, Result: res, SessionID: m.SessionID})
	}
}

func (f *fakeBrowser) write(m message) {
	b, _ := json.Marshal(m)
	_, _ = f.out.Write(append(b, 0))
}

func TestCallsAndEventsInOrder(t *testing.T) {
	var mu sync.Mutex
	var got []string
	var c *Conn
	c, _ = newPair(t, func(e Event) {
		mu.Lock()
		got = append(got, e.Session+":"+e.Method)
		mu.Unlock()
		// A handler may call back into the connection: its reply is read while it waits.
		if e.Method == "A.one" {
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			if err := c.Call(ctx, "", "Inside.call", nil, nil); err != nil {
				t.Errorf("a call from a handler: %v", err)
			}
		}
	})
	ctx := context.Background()
	var r struct{ Method, Session string }
	if err := c.Call(ctx, "S1", "Page.enable", nil, &r); err != nil || r.Method != "Page.enable" || r.Session != "S1" {
		t.Fatalf("call: %+v %v", r, err)
	}
	if err := c.Call(ctx, "S1", "Fake.emit", []string{"A.one", "A.two", "A.three"}, nil); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		mu.Lock()
		n := len(got)
		mu.Unlock()
		if n == 3 || time.Now().After(deadline) {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if strings.Join(got, ",") != "S1:A.one,S1:A.two,S1:A.three" {
		t.Fatalf("events: %v", got)
	}
	var ce *Error
	if err := c.Call(ctx, "", "Fake.fail", nil, nil); !errors.As(err, &ce) || ce.Code != -32000 {
		t.Fatalf("an error reply: %v", err)
	}
	var big struct{ Data string }
	if err := c.Call(ctx, "", "Fake.big", nil, &big); err != nil || len(big.Data) != 3<<20 {
		t.Fatalf("a message larger than the read buffer: %d %v", len(big.Data), err)
	}
}

func TestSendKeepsOrderWithoutWaiting(t *testing.T) {
	c, f := newPair(t, nil)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	var replies []<-chan error
	for _, m := range []string{"Page.enable", "Emulation.x", "Runtime.runIfWaitingForDebugger"} {
		replies = append(replies, c.Send(ctx, "S", m, nil))
	}
	for _, r := range replies {
		if err := <-r; err != nil {
			t.Fatal(err)
		}
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if strings.Join(f.seen, ",") != "Page.enable,Emulation.x,Runtime.runIfWaitingForDebugger" {
		t.Fatalf("order on the pipe: %v", f.seen)
	}
}

func TestCallsEndWithThePipe(t *testing.T) {
	c, f := newPair(t, nil)
	done := make(chan error, 1)
	go func() { done <- c.Call(context.Background(), "", "Fake.never", nil, nil) }()
	time.Sleep(50 * time.Millisecond)
	f.out.Close()
	select {
	case err := <-done:
		if !errors.Is(err, ErrClosed) {
			t.Fatalf("a waiting call: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("a waiting call outlived the pipe")
	}
	<-c.Done()
	if err := c.Call(context.Background(), "", "X", nil, nil); !errors.Is(err, ErrClosed) {
		t.Fatalf("a call after the end: %v", err)
	}
}

func TestCallTimesOut(t *testing.T) {
	c, _ := newPair(t, nil)
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	if err := c.Call(ctx, "", "Fake.never", nil, nil); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("timeout: %v", err)
	}
}
