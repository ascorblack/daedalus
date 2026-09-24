package term

import (
	"bytes"
	"slices"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
)

// The daemon answers every terminal query itself, and the app swallows them. An app bundle from
// before a query joined the swallowed set (the service worker can keep one for days) still answers,
// and the program reads that second answer as typing: a cursor report `ESC[1;2R` is byte for byte
// Shift+F3. So an INPUT frame that repeats exactly an answer the daemon gave to a query this client
// was shown, within two seconds of showing it, is dropped. The same bytes typed later pass.

// maxAnswers bounds the recent answers kept per terminal, and the pending ones per client.
const maxAnswers = 32

// recentAnswer is a reply the daemon wrote for a query that ends at output offset seq.
type recentAnswer struct {
	seq   int64
	reply []byte
	at    time.Time
}

// pendingReply is an answer a client may echo: the client was shown its query at `at`.
type pendingReply struct {
	seq   int64
	reply []byte
	at    time.Time
}

// answered records a reply to a query ending at seq. It runs on the emulator goroutine, which may be
// behind the clients: one that was already sent the query is marked here, the others when it is.
func (t *Terminal) answered(seq int64, reply []byte) {
	now := t.deps.Clock.Now()
	a := &t.att
	a.mu.Lock()
	defer a.mu.Unlock()
	a.answers = slices.DeleteFunc(a.answers, func(x recentAnswer) bool { return now.Sub(x.at) > config.ReplyEchoWindow })
	if len(a.answers) >= maxAnswers {
		a.answers = a.answers[1:]
	}
	r := append([]byte(nil), reply...)
	a.answers = append(a.answers, recentAnswer{seq: seq, reply: r, at: now})
	for _, c := range a.clients {
		if c.attached && c.sent.Load() >= seq {
			c.addPendingLocked(seq, r, now)
		}
	}
}

// forwarded runs after OUTPUT [from, to) went to c: the answers to queries in it may now be echoed.
func (t *Terminal) forwarded(c *Client, from, to int64) {
	a := &t.att
	a.mu.Lock()
	defer a.mu.Unlock()
	if len(a.answers) == 0 {
		return
	}
	now := t.deps.Clock.Now()
	for _, x := range a.answers {
		// A query's offset is the byte just after it: the client has seen it once that byte is sent.
		if x.seq > from && x.seq <= to {
			c.addPendingLocked(x.seq, x.reply, now)
		}
	}
}

// addPendingLocked notes an answer c may echo. t.att.mu is held.
func (c *Client) addPendingLocked(seq int64, reply []byte, now time.Time) {
	c.pending = slices.DeleteFunc(c.pending, func(p pendingReply) bool { return now.Sub(p.at) > config.ReplyEchoWindow })
	for _, p := range c.pending {
		if p.seq == seq {
			return
		}
	}
	if len(c.pending) >= maxAnswers {
		c.pending = c.pending[1:]
	}
	c.pending = append(c.pending, pendingReply{seq: seq, reply: reply, at: now})
}

// replyEcho reports whether p is c's own answer to a query the daemon already answered, and uses
// the pending answer up: each answer is dropped once.
func (t *Terminal) replyEcho(c *Client, p []byte) bool {
	a := &t.att
	a.mu.Lock()
	defer a.mu.Unlock()
	if len(c.pending) == 0 {
		return false
	}
	now := t.deps.Clock.Now()
	for i, x := range c.pending {
		if now.Sub(x.at) <= config.ReplyEchoWindow && bytes.Equal(x.reply, p) {
			c.pending = slices.Delete(c.pending, i, i+1)
			return true
		}
	}
	return false
}
