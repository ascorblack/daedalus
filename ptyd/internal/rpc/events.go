package rpc

import (
	"context"
	"encoding/json"
	"errors"
	"os"

	"github.com/ascorblack/daedalus/ptyd/internal/events"
	"github.com/ascorblack/daedalus/ptyd/internal/server"
	"github.com/ascorblack/daedalus/ptyd/internal/wire"
)

func pidSelf() int { return os.Getpid() }

// subscriptionKey holds a connection's subscription; there is at most one per connection, and a
// new subscribe replaces it.
type subscriptionKey struct{}

// eventBatch is how many events are read from the log at a time.
const eventBatch = 256

func (d *Daemon) subscribe(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		AfterSeq int64 `json:"after_seq"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	d.stopSubscription(c)
	cursor, resync := d.Events.Start(p.AfterSeq)
	subCtx, cancel := context.WithCancel(c.Context())
	c.SetValue(subscriptionKey{}, cancel)
	result := map[string]any{"instance": d.Instance, "from_seq": cursor + 1, "resync": resync}
	return server.Then{Result: result, After: func() { go d.pump(subCtx, c, cursor) }}, nil
}

func (d *Daemon) unsubscribe(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	d.stopSubscription(c)
	return map[string]any{}, nil
}

func (d *Daemon) stopSubscription(c *server.Conn) {
	if cancel, ok := c.Value(subscriptionKey{}).(context.CancelFunc); ok {
		cancel()
		c.SetValue(subscriptionKey{}, nil)
	}
}

// pump delivers events after cursor to the connection, in order, as notifications. It reads the log
// by offset, so a slow client only falls behind; if it falls off the end of the log it is told with
// an `events.resync` notification and continues from the oldest event held.
func (d *Daemon) pump(ctx context.Context, c *server.Conn, cursor int64) {
	for {
		// Take the wake-up channel before reading, so an event published in between is not missed.
		wake := d.Events.Wait()
		evs, lost := d.Events.After(cursor, eventBatch)
		if lost {
			if err := c.Notify("events.resync", map[string]any{"from_seq": d.Events.Oldest()}); err != nil {
				return
			}
		}
		for _, e := range evs {
			if ctx.Err() != nil {
				return
			}
			err := c.Notify("event", e)
			if errors.Is(err, wire.ErrFrameTooLarge) {
				// An event bigger than a frame must not end the subscription: every later event
				// would be lost with it. It goes out as its envelope, marked, and the rest follow.
				err = c.Notify("event", events.Event{Seq: e.Seq, At: e.At, Type: e.Type, TerminalID: e.TerminalID,
					Data: map[string]any{"too_large": true}})
			}
			if err != nil {
				return
			}
			cursor = e.Seq
		}
		if len(evs) > 0 {
			continue
		}
		select {
		case <-wake:
		case <-ctx.Done():
			return
		}
	}
}
