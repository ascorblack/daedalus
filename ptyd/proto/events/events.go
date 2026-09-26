// Package events is the daemon's single ordered log of what happened: terminals created and exited,
// titles, bells, shell marks, statistics, and (later) hook posts. Every event takes its sequence
// number from one counter, so a subscriber sees one total order across terminals and kinds — which
// is what lets the host put a hook's "turn finished" and a terminal's output in the right order.
package events

import (
	"sync"
	"time"
)

// Event is one entry of the log.
type Event struct {
	Seq        int64     `json:"seq"`
	At         time.Time `json:"at"`
	Type       string    `json:"type"`
	TerminalID string    `json:"terminal_id,omitempty"`
	Data       any       `json:"data,omitempty"`
}

// Log keeps the last Size events for subscribers that reconnect, and wakes live ones.
type Log struct {
	mu    sync.Mutex
	buf   []Event
	sizes []int // the size each held event was published with, parallel to buf
	first int   // index of the oldest event in buf
	n     int   // events held
	seq   int64 // the last sequence number handed out
	wake  chan struct{}
	now   func() time.Time

	bytes    int // the sizes of the events held
	maxBytes int // 0: bounded by count alone
}

// nominalSize is what an event published without a size counts for: the small events of terminals
// are a few hundred bytes each.
const nominalSize = 256

// NewLog returns a log that keeps size events.
func NewLog(size int) *Log {
	size = max(1, size)
	return &Log{buf: make([]Event, size), sizes: make([]int, size), wake: make(chan struct{}), now: time.Now}
}

// SetMaxBytes bounds the log by the sizes its events were published with, as well as by count. An
// event that pushes the log past it drops the oldest ones; a subscriber that had not read them is
// told to resync, as when the count runs over.
func (l *Log) SetMaxBytes(n int) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.maxBytes = n
}

// Publish appends an event and wakes every waiting subscriber.
func (l *Log) Publish(typ, terminalID string, data any) Event {
	return l.PublishSized(typ, terminalID, data, nominalSize)
}

// PublishSized is Publish for an event whose data is large (a hook's body), with its encoded size,
// which counts against the log's byte bound.
func (l *Log) PublishSized(typ, terminalID string, data any, size int) Event {
	l.mu.Lock()
	l.seq++
	e := Event{Seq: l.seq, At: l.now().UTC(), Type: typ, TerminalID: terminalID, Data: data}
	if l.n == len(l.buf) {
		l.bytes -= l.sizes[l.first]
		l.first = (l.first + 1) % len(l.buf)
		l.n--
	}
	i := (l.first + l.n) % len(l.buf)
	l.buf[i], l.sizes[i] = e, size
	l.n++
	l.bytes += size
	for l.maxBytes > 0 && l.bytes > l.maxBytes && l.n > 1 {
		l.bytes -= l.sizes[l.first]
		l.buf[l.first] = Event{} // let the dropped data go
		l.first = (l.first + 1) % len(l.buf)
		l.n--
	}
	wake := l.wake
	l.wake = make(chan struct{})
	l.mu.Unlock()
	close(wake)
	return e
}

// Last is the sequence number of the newest event, 0 before the first.
func (l *Log) Last() int64 {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.seq
}

// Oldest is the sequence number of the oldest event held; with no events it is Last()+1.
func (l *Log) Oldest() int64 {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.oldest()
}

func (l *Log) oldest() int64 { return l.seq - int64(l.n) + 1 }

// Wait returns a channel that is closed at the next Publish.
func (l *Log) Wait() <-chan struct{} {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.wake
}

// Start resolves where a subscription that has seen everything up to after begins. resync is true
// when events it has not seen are gone: they fell off the log, or after comes from a different
// life of the daemon (it is beyond the newest event). The subscription then starts at the oldest
// event held.
func (l *Log) Start(after int64) (cursor int64, resync bool) {
	l.mu.Lock()
	defer l.mu.Unlock()
	oldest := l.oldest()
	switch {
	case after > l.seq:
		return oldest - 1, true
	case after+1 < oldest:
		return oldest - 1, true
	default:
		return after, false
	}
}

// After returns up to max events with a sequence number above cursor. lost is true when events
// between cursor and the first one returned have already fallen off the log.
func (l *Log) After(cursor int64, max int) (evs []Event, lost bool) {
	l.mu.Lock()
	defer l.mu.Unlock()
	oldest := l.oldest()
	if cursor+1 < oldest {
		lost = true
		cursor = oldest - 1
	}
	for s := cursor + 1; s <= l.seq && len(evs) < max; s++ {
		evs = append(evs, l.buf[(l.first+int(s-oldest))%len(l.buf)])
	}
	return evs, lost
}
