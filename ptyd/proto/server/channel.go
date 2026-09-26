package server

import (
	"errors"
	"sync"
)

// ErrChannelClosed is returned by SendChannel once either side has closed the channel.
var ErrChannelClosed = errors.New("the channel is closed")

// channel is one open channel. Its lock is held across every send and across the close, which makes
// the empty closing frame the last frame of the channel on the wire: a producer that raced the close
// learns it from SendChannel instead of putting a frame behind it.
type channel struct {
	h      ChannelHandler
	mu     sync.Mutex
	closed bool
}

// OpenChannel allocates a channel id and routes its frames to h. Ids are never reused within a
// connection, so a late frame for a closed channel can never reach a new one.
func (c *Conn) OpenChannel(h ChannelHandler) uint32 {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.nextChannel++
	c.channels[c.nextChannel] = &channel{h: h}
	return c.nextChannel
}

// SendChannel queues one frame on an open channel. It fails with ErrChannelClosed once the channel
// is closed from either side, and with the connection's error once the connection is gone.
func (c *Conn) SendChannel(id uint32, payload []byte) error {
	if len(payload) == 0 {
		return errors.New("an empty payload closes a channel; use CloseChannel")
	}
	c.mu.Lock()
	ch := c.channels[id]
	c.mu.Unlock()
	if ch == nil {
		return ErrChannelClosed
	}
	ch.mu.Lock()
	defer ch.mu.Unlock()
	if ch.closed {
		return ErrChannelClosed
	}
	return c.Send(id, payload)
}

// CloseChannel closes a channel from this side: it is forgotten, and the other side is told with an
// empty frame. Its answering empty frame finds no channel and is dropped. The handler is not called:
// whoever closes a channel has already finished with it.
func (c *Conn) CloseChannel(id uint32) {
	c.mu.Lock()
	ch := c.channels[id]
	delete(c.channels, id)
	c.mu.Unlock()
	if ch == nil {
		return
	}
	ch.mu.Lock()
	if !ch.closed {
		ch.closed = true
		_ = c.Send(id, nil)
	}
	ch.mu.Unlock()
}

// Channels is the number of open channels, for tests that check nothing is left behind.
func (c *Conn) Channels() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.channels)
}

// peerClosed handles an empty frame from the other side: the channel is forgotten, answered with
// our own empty frame, and its handler told. A channel this side already closed is not in the table,
// so the answer to our own close is never answered again.
func (c *Conn) peerClosed(id uint32) {
	c.mu.Lock()
	ch := c.channels[id]
	delete(c.channels, id)
	c.mu.Unlock()
	if ch == nil {
		return
	}
	ch.mu.Lock()
	ch.closed = true
	_ = c.Send(id, nil)
	ch.mu.Unlock()
	ch.h.Closed()
}

// closeAll ends every channel when the connection ends; nothing can be sent on them any more.
func (c *Conn) closeAll() {
	c.mu.Lock()
	chans := c.channels
	c.channels = map[uint32]*channel{}
	c.mu.Unlock()
	for _, ch := range chans {
		ch.mu.Lock()
		ch.closed = true
		ch.mu.Unlock()
		ch.h.Closed()
	}
}

// DiscardChannel forgets a channel the other side was never told about (a method that opened one
// and then failed), without the closing frame the other side would not recognise.
func (c *Conn) DiscardChannel(id uint32) {
	c.mu.Lock()
	ch := c.channels[id]
	delete(c.channels, id)
	c.mu.Unlock()
	if ch != nil {
		ch.mu.Lock()
		ch.closed = true
		ch.mu.Unlock()
	}
}
