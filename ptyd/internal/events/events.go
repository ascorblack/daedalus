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
	first int   // index of the oldest event in buf
	n     int   // events held
	seq   int64 // the last sequence number handed out
	wake  chan struct{}
	now   func() time.Time
}

// NewLog returns a log that keeps size events.
func NewLog(size int) *Log {
	return &Log{buf: make([]Event, max(1, size)), wake: make(chan struct{}), now: time.Now}
}

// Publish appends an event and wakes every waiting subscriber.
func (l *Log) Publish(typ, terminalID string, data any) Event {
	l.mu.Lock()
	l.seq++
	e := Event{Seq: l.seq, At: l.now().UTC(), Type: typ, TerminalID: terminalID, Data: data}
	if l.n == len(l.buf) {
		l.buf[l.first] = e
		l.first = (l.first + 1) % len(l.buf)
	} else {
		l.buf[(l.first+l.n)%len(l.buf)] = e
		l.n++
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
