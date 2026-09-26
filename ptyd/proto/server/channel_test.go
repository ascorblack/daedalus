//go:build unix

package server_test

import (
	"context"
	"encoding/json"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/ptyd/proto/server"
	"github.com/ascorblack/daedalus/ptyd/proto/server/clienttest"
)

// flood is a channel handler whose producer sends as fast as it can until the channel closes.
type flood struct {
	closed chan struct{}
	once   sync.Once
}

func (f *flood) Frame([]byte) {}
func (f *flood) Closed()      { f.once.Do(func() { close(f.closed) }) }

// A channel closed while a producer is sending on it ends with the closing frame: nothing follows it
// on the wire, and the producer learns of the close from its next send.
func TestNothingFollowsAChannelsClose(t *testing.T) {
	dir := runDir(t)
	ep, err := server.Prepare(dir, "unix", testDaemon)
	if err != nil {
		t.Fatal(err)
	}
	srv := server.New(ep.Token, quiet(), func() any { return map[string]any{} })
	producers := make(chan error, 4)
	var conn *server.Conn
	var connMu sync.Mutex
	srv.Handle("open", func(ctx context.Context, c *server.Conn, p json.RawMessage) (any, error) {
		f := &flood{closed: make(chan struct{})}
		id := c.OpenChannel(f)
		connMu.Lock()
		conn = c
		connMu.Unlock()
		return server.Then{Result: id, After: func() {
			go func() {
				for {
					if err := c.SendChannel(id, []byte("data")); err != nil {
						producers <- err
						return
					}
				}
			}()
		}}, nil
	})
	srv.Handle("close", func(ctx context.Context, c *server.Conn, p json.RawMessage) (any, error) {
		var id uint32
		_ = json.Unmarshal(p, &id)
		c.CloseChannel(id)
		return nil, nil
	})
	go srv.Serve(ep.Listener)
	t.Cleanup(func() {
		ep.Listener.Close()
		srv.Close()
		ep.Release()
	})
	client, err := clienttest.Dial(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()

	// untilClosed reads the channel's frames up to its closing one and checks none follows.
	untilClosed := func(id uint32) {
		t.Helper()
		deadline := time.After(10 * time.Second)
		for {
			select {
			case f := <-client.Frames:
				if f.Channel == id && len(f.Payload) == 0 {
					select {
					case f := <-client.Frames:
						t.Fatalf("a frame on channel %d after its close", f.Channel)
					case <-time.After(200 * time.Millisecond):
					}
					return
				}
			case <-deadline:
				t.Fatalf("channel %d never closed", id)
			}
		}
	}

	var id uint32
	ctx := context.Background()
	// The other side closes.
	if err := client.Call(ctx, "open", nil, &id); err != nil {
		t.Fatal(err)
	}
	time.Sleep(20 * time.Millisecond)
	if err := client.Send(id, nil); err != nil {
		t.Fatal(err)
	}
	untilClosed(id)
	if err := <-producers; !errors.Is(err, server.ErrChannelClosed) {
		t.Fatalf("the producer's send after the close: %v", err)
	}
	// This side closes.
	var second uint32
	if err := client.Call(ctx, "open", nil, &second); err != nil {
		t.Fatal(err)
	}
	if second <= id {
		t.Fatalf("channel ids %d then %d: reused or not increasing", id, second)
	}
	time.Sleep(20 * time.Millisecond)
	go func() { _ = client.Call(ctx, "close", second, nil) }()
	untilClosed(second)
	if err := <-producers; !errors.Is(err, server.ErrChannelClosed) {
		t.Fatalf("the producer's send after the close: %v", err)
	}
	// Our answer to the daemon's close finds no channel and is dropped, not answered again.
	_ = client.Send(second, nil)
	select {
	case f := <-client.Frames:
		if f.Channel == second {
			t.Fatalf("the answer to a close was answered: %d bytes", len(f.Payload))
		}
	case <-time.After(200 * time.Millisecond):
	}
	connMu.Lock()
	n := conn.Channels()
	connMu.Unlock()
	if n != 0 {
		t.Fatalf("%d channels still open", n)
	}
}
