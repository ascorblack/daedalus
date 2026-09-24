package events

import (
	"sync"
	"time"
)

// Policy is how often one kind of event may be published per terminal.
type Policy struct {
	Window time.Duration
	// Coalesce publishes the latest value at the end of the window instead of dropping it. A title
	// that changes forty times a second (a progress counter in the title) ends up correct, just not
	// forty times; a bell that rings forty times a second rings once.
	Coalesce bool
}

// Policies are the per-terminal limits of the chatty event kinds.
var Policies = map[string]Policy{
	"terminal.title":    {Window: 250 * time.Millisecond, Coalesce: true},
	"terminal.progress": {Window: 500 * time.Millisecond, Coalesce: true},
	"terminal.bell":     {Window: 2 * time.Second},
	"terminal.notify":   {Window: 5 * time.Second},
}

type debounceKey struct{ terminal, typ string }

type debounceState struct {
	last    time.Time
	pending any
	has     bool
	timer   *time.Timer
}

// Debouncer publishes into a Log through the Policies.
type Debouncer struct {
	log      *Log
	policies map[string]Policy
	now      func() time.Time
	after    func(time.Duration, func()) *time.Timer

	mu    sync.Mutex
	state map[debounceKey]*debounceState
}

// NewDebouncer returns a debouncer over log with the given policies.
func NewDebouncer(log *Log, policies map[string]Policy) *Debouncer {
	return &Debouncer{log: log, policies: policies, now: time.Now, after: time.AfterFunc, state: map[debounceKey]*debounceState{}}
}

// Publish publishes now, later, or not at all, as the event's policy says. Kinds without a policy
// are published at once.
func (d *Debouncer) Publish(typ, terminalID string, data any) {
	p, ok := d.policies[typ]
	if !ok {
		d.log.Publish(typ, terminalID, data)
		return
	}
	d.mu.Lock()
	k := debounceKey{terminalID, typ}
	st := d.state[k]
	if st == nil {
		st = &debounceState{}
		d.state[k] = st
	}
	now := d.now()
	if st.last.IsZero() || now.Sub(st.last) >= p.Window {
		st.last = now
		st.has = false
		d.mu.Unlock()
		d.log.Publish(typ, terminalID, data)
		return
	}
	if p.Coalesce {
		st.pending, st.has = data, true
		if st.timer == nil {
			st.timer = d.after(p.Window-now.Sub(st.last), func() { d.fire(k) })
		}
	}
	d.mu.Unlock()
}

func (d *Debouncer) fire(k debounceKey) {
	d.mu.Lock()
	st := d.state[k]
	if st == nil {
		d.mu.Unlock()
		return
	}
	st.timer = nil
	if !st.has {
		d.mu.Unlock()
		return
	}
	data := st.pending
	st.pending, st.has = nil, false
	st.last = d.now()
	d.mu.Unlock()
	d.log.Publish(k.typ, k.terminal, data)
}

// Flush publishes whatever a terminal has pending and forgets its state. It is called when the
// terminal exits, so its last title is published before its exit.
func (d *Debouncer) Flush(terminalID string) {
	type out struct {
		typ  string
		data any
	}
	var pending []out
	d.mu.Lock()
	for k, st := range d.state {
		if k.terminal != terminalID {
			continue
		}
		if st.timer != nil {
			st.timer.Stop()
		}
		if st.has {
			pending = append(pending, out{k.typ, st.pending})
		}
		delete(d.state, k)
	}
	d.mu.Unlock()
	for _, p := range pending {
		d.log.Publish(p.typ, terminalID, p.data)
	}
}
