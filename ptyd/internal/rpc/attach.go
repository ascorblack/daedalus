package rpc

import (
	"context"
	"encoding/json"
	"errors"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/internal/server"
	"github.com/ascorblack/daedalus/ptyd/internal/term"
	"github.com/ascorblack/daedalus/ptyd/internal/wire"
)

// registerAttach installs the attachment methods.
func (d *Daemon) registerAttach(srv *server.Server) {
	srv.Handle("terminal.attach", d.attach)
	srv.Handle("terminal.detach", d.detach)
	srv.Handle("terminal.keyboard", d.keyboard)
}

// maxClientLabel bounds the free-text fields of a client, which are echoed to every other client.
const maxClientLabel = 256

// attachmentsKey holds a connection's attachments by channel, for `terminal.detach`.
type attachmentsKey struct{}

type connAttachments struct {
	mu      sync.Mutex
	clients map[uint32]*term.Client
}

func attachmentsOf(c *server.Conn) *connAttachments {
	// Methods of one connection run concurrently; the first to arrive creates the table.
	attachMu.Lock()
	defer attachMu.Unlock()
	if a, ok := c.Value(attachmentsKey{}).(*connAttachments); ok {
		return a
	}
	a := &connAttachments{clients: map[uint32]*term.Client{}}
	c.SetValue(attachmentsKey{}, a)
	return a
}

var attachMu sync.Mutex

// channelSink is an attachment channel as the terminal sees it.
type channelSink struct {
	c  *server.Conn
	id uint32
}

func (s channelSink) Send(frame []byte) error { return s.c.SendChannel(s.id, frame) }
func (s channelSink) Close()                  { s.c.CloseChannel(s.id) }

// relay routes the channel's frames to the client, once there is one.
type relay struct {
	mu     sync.Mutex
	client *term.Client
	closed bool
}

func (r *relay) Frame(payload []byte) {
	r.mu.Lock()
	c := r.client
	r.mu.Unlock()
	if c != nil {
		c.Frame(payload)
	}
}

func (r *relay) Closed() {
	r.mu.Lock()
	c := r.client
	r.closed = true
	r.mu.Unlock()
	if c != nil {
		c.Closed()
	}
}

func (r *relay) set(c *term.Client) {
	r.mu.Lock()
	r.client = c
	closed := r.closed
	r.mu.Unlock()
	if closed {
		c.Closed()
	}
}

func (d *Daemon) attach(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		ID     string `json:"id"`
		Client struct {
			Kind     string `json:"kind"`
			Label    string `json:"label"`
			Via      string `json:"via"`
			ReadOnly bool   `json:"read_only"`
		} `json:"client"`
	}
	t, err := d.terminal(params, &p, &p.ID)
	if err != nil {
		return nil, err
	}
	switch p.Client.Kind {
	case "", term.KindHuman, term.KindViewer:
	default:
		return nil, wire.Errorf(wire.CodeInvalidParams, "client.kind must be human or viewer")
	}
	if len(p.Client.Label) > maxClientLabel || len(p.Client.Via) > maxClientLabel {
		return nil, wire.Errorf(wire.CodeInvalidParams, "client.label and client.via are at most %d bytes", maxClientLabel)
	}
	r := &relay{}
	id := c.OpenChannel(r)
	client, err := t.Attach(c.Context(), term.ClientOptions{Kind: p.Client.Kind, Label: p.Client.Label,
		Via: p.Client.Via, ReadOnly: p.Client.ReadOnly}, channelSink{c: c, id: id})
	if err != nil {
		c.DiscardChannel(id)
		if errors.Is(err, term.ErrTooManyClients) {
			return nil, wire.Errorf(wire.CodeLimit, "%v (at most %d)", err, config.MaxClients)
		}
		return nil, termError(err)
	}
	r.set(client)
	table := attachmentsOf(c)
	table.mu.Lock()
	table.clients[id] = client
	table.mu.Unlock()
	go func() {
		<-client.Done()
		table.mu.Lock()
		delete(table.clients, id)
		table.mu.Unlock()
	}()
	d.Log.Info("client attached", "terminal", t.ID, "client", client.ID, "kind", client.Kind, "via", client.Via,
		"channel", id)
	return map[string]any{"channel": id, "client_id": client.ID}, nil
}

func (d *Daemon) detach(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		Channel uint32 `json:"channel"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	table := attachmentsOf(c)
	table.mu.Lock()
	client := table.clients[p.Channel]
	table.mu.Unlock()
	if client == nil {
		return nil, wire.Errorf(wire.CodeNotFound, "no attachment on channel %d", p.Channel)
	}
	client.Detach()
	return map[string]any{}, nil
}

func (d *Daemon) keyboard(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		ID    string `json:"id"`
		Owner string `json:"owner"`
		TTLMs *int64 `json:"ttl_ms"`
	}
	t, err := d.terminal(params, &p, &p.ID)
	if err != nil {
		return nil, err
	}
	switch p.Owner {
	case term.OwnerAuto, term.OwnerHuman, term.OwnerAgent:
	default:
		return nil, wire.Errorf(wire.CodeInvalidParams, "owner must be auto, human or agent")
	}
	var ttl time.Duration
	if p.TTLMs != nil {
		ttl = time.Duration(*p.TTLMs) * time.Millisecond
		if ttl < 0 || ttl > config.MaxKeyboardTTL {
			return nil, wire.Errorf(wire.CodeInvalidParams, "ttl_ms must be 0..%d", config.MaxKeyboardTTL.Milliseconds())
		}
	}
	if !t.Running() {
		return nil, termError(term.ErrExited)
	}
	return t.SetKeyboard(p.Owner, ttl), nil
}
