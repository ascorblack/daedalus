// Package clienttest is a minimal client of the daemon's socket, for tests: it does the handshake,
// makes calls, and collects notifications.
package clienttest

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"strconv"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/ptyd/proto/server"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// Notification is one notification from the daemon.
type Notification struct {
	Method string
	Params json.RawMessage
}

// Client is one connection.
type Client struct {
	nc   net.Conn
	wmu  sync.Mutex
	mu   sync.Mutex
	next int
	wait map[string]chan wire.Response
	done chan struct{}

	// Hello is the params of the hello notification.
	Hello json.RawMessage
	// Notes receives every notification after hello, in order. It is buffered; a test that ignores
	// it for too long stalls the reader.
	Notes chan Notification
	// Frames receives frames of channels other than 0.
	Frames chan wire.Frame
}

// Dial connects to the daemon serving runDir and waits for its hello.
func Dial(runDir string) (*Client, error) {
	network, address, token, err := server.ReadEndpoint(runDir)
	if err != nil {
		return nil, err
	}
	return DialWith(network, address, token)
}

// DialWith connects with an explicit address and token (a wrong token, in the tests that need one).
func DialWith(network, address, token string) (*Client, error) {
	nc, err := net.DialTimeout(network, address, 5*time.Second)
	if err != nil {
		return nil, err
	}
	c := &Client{nc: nc, wait: map[string]chan wire.Response{}, done: make(chan struct{}),
		Notes: make(chan Notification, 4096), Frames: make(chan wire.Frame, 1024)}
	if err := wire.WriteFrame(nc, 0, []byte(token)); err != nil {
		nc.Close()
		return nil, err
	}
	_ = nc.SetReadDeadline(time.Now().Add(5 * time.Second))
	f, err := wire.ReadFrame(nc)
	if err != nil {
		nc.Close()
		return nil, fmt.Errorf("no hello: %w", err)
	}
	var n wire.Request
	if err := json.Unmarshal(f.Payload, &n); err != nil || n.Method != "hello" {
		nc.Close()
		return nil, fmt.Errorf("first frame is not hello: %s", f.Payload)
	}
	c.Hello = n.Params
	_ = nc.SetReadDeadline(time.Time{})
	go c.read()
	return c, nil
}

func (c *Client) read() {
	defer close(c.done)
	for {
		f, err := wire.ReadFrame(c.nc)
		if err != nil {
			c.mu.Lock()
			for _, ch := range c.wait {
				close(ch)
			}
			c.wait = map[string]chan wire.Response{}
			c.mu.Unlock()
			close(c.Notes)
			return
		}
		if f.Channel != 0 {
			c.Frames <- f
			continue
		}
		var probe struct {
			ID     json.RawMessage `json:"id"`
			Method string          `json:"method"`
			Params json.RawMessage `json:"params"`
		}
		_ = json.Unmarshal(f.Payload, &probe)
		if probe.Method != "" {
			c.Notes <- Notification{Method: probe.Method, Params: probe.Params}
			continue
		}
		var resp wire.Response
		_ = json.Unmarshal(f.Payload, &resp)
		c.mu.Lock()
		ch := c.wait[string(resp.ID)]
		delete(c.wait, string(resp.ID))
		c.mu.Unlock()
		if ch != nil {
			ch <- resp
		}
	}
}

// Call makes one call and decodes its result into result (which may be nil).
func (c *Client) Call(ctx context.Context, method string, params, result any) error {
	c.mu.Lock()
	c.next++
	id := strconv.Itoa(c.next)
	ch := make(chan wire.Response, 1)
	c.wait[id] = ch
	c.mu.Unlock()
	p, err := json.Marshal(params)
	if err != nil {
		return err
	}
	req, _ := json.Marshal(wire.Request{JSONRPC: "2.0", ID: json.RawMessage(id), Method: method, Params: p})
	c.wmu.Lock()
	err = wire.WriteFrame(c.nc, 0, req)
	c.wmu.Unlock()
	if err != nil {
		return err
	}
	select {
	case resp, ok := <-ch:
		if !ok {
			return fmt.Errorf("%s: connection closed", method)
		}
		if resp.Error != nil {
			return resp.Error
		}
		if result != nil {
			return json.Unmarshal(resp.Result, result)
		}
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// Send writes a raw frame.
func (c *Client) Send(channel uint32, payload []byte) error {
	c.wmu.Lock()
	defer c.wmu.Unlock()
	return wire.WriteFrame(c.nc, channel, payload)
}

// Close closes the connection and waits for the reader.
func (c *Client) Close() {
	c.nc.Close()
	<-c.done
}

// Closed is closed when the connection has ended.
func (c *Client) Closed() <-chan struct{} { return c.done }

// Event is the params of an "event" notification.
type Event struct {
	Seq        int64           `json:"seq"`
	Type       string          `json:"type"`
	TerminalID string          `json:"terminal_id"`
	Data       json.RawMessage `json:"data"`
}

// WaitEvent reads notifications until an event of type typ for terminalID arrives (any terminal
// when terminalID is empty), or the timeout passes.
func (c *Client) WaitEvent(typ, terminalID string, timeout time.Duration) (Event, error) {
	deadline := time.After(timeout)
	for {
		select {
		case n, ok := <-c.Notes:
			if !ok {
				return Event{}, fmt.Errorf("connection closed waiting for %s", typ)
			}
			if n.Method != "event" {
				continue
			}
			var e Event
			if err := json.Unmarshal(n.Params, &e); err != nil {
				return Event{}, err
			}
			if e.Type == typ && (terminalID == "" || e.TerminalID == terminalID) {
				return e, nil
			}
		case <-deadline:
			return Event{}, fmt.Errorf("no %s event within %s", typ, timeout)
		}
	}
}
