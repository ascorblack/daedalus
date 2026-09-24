package events

import (
	"sync"
	"testing"
	"time"
)

func seqs(evs []Event) []int64 {
	var out []int64
	for _, e := range evs {
		out = append(out, e.Seq)
	}
	return out
}

func TestOneOrderAcrossTerminals(t *testing.T) {
	l := NewLog(10)
	l.Publish("terminal.created", "a", nil)
	l.Publish("hook", "b", nil)
	l.Publish("terminal.exited", "a", nil)
	evs, lost := l.After(0, 100)
	if lost || len(evs) != 3 || evs[0].Seq != 1 || evs[2].Seq != 3 || evs[1].TerminalID != "b" {
		t.Fatalf("%+v lost=%v", evs, lost)
	}
}

func TestSubscribeAfterGaps(t *testing.T) {
	l := NewLog(5)
	// Nothing published: a fresh subscriber starts at the beginning, nothing lost.
	if c, resync := l.Start(0); c != 0 || resync {
		t.Fatalf("empty log: %d %v", c, resync)
	}
	for i := 0; i < 12; i++ {
		l.Publish("x", "", i)
	}
	// Held: 8..12.
	if l.Oldest() != 8 || l.Last() != 12 {
		t.Fatalf("oldest %d last %d", l.Oldest(), l.Last())
	}
	cases := []struct {
		after  int64
		cursor int64
		resync bool
	}{
		{12, 12, false}, // up to date
		{9, 9, false},   // behind, but everything missed is still held
		{7, 7, false},   // exactly the event before the oldest: nothing lost
		{6, 7, true},    // event 7 is gone
		{0, 7, true},    // a new subscriber after the log wrapped
		{99, 7, true},   // a cursor from another life of the daemon
	}
	for _, c := range cases {
		cursor, resync := l.Start(c.after)
		if cursor != c.cursor || resync != c.resync {
			t.Errorf("Start(%d) = %d %v, want %d %v", c.after, cursor, resync, c.cursor, c.resync)
		}
	}
	evs, lost := l.After(3, 2)
	if !lost || len(evs) != 2 || evs[0].Seq != 8 {
		t.Fatalf("After past the start: %v lost=%v", seqs(evs), lost)
	}
}

func TestWaitWakesOnPublish(t *testing.T) {
	l := NewLog(4)
	w := l.Wait()
	select {
	case <-w:
		t.Fatal("woke with nothing published")
	default:
	}
	l.Publish("x", "", nil)
	select {
	case <-w:
	case <-time.After(time.Second):
		t.Fatal("not woken")
	}
}

// manualTimers lets a test fire the debouncer's timers by hand.
type manualTimers struct {
	mu  sync.Mutex
	fns []func()
}

func (m *manualTimers) after(d time.Duration, fn func()) *time.Timer {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.fns = append(m.fns, fn)
	return time.NewTimer(time.Hour) // stopped by the debouncer or left to expire unused
}

func (m *manualTimers) fire() {
	m.mu.Lock()
	fns := m.fns
	m.fns = nil
	m.mu.Unlock()
	for _, f := range fns {
		f()
	}
}

func TestDebounce(t *testing.T) {
	l := NewLog(100)
	d := NewDebouncer(l, Policies)
	now := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	d.now = func() time.Time { return now }
	timers := &manualTimers{}
	d.after = timers.after

	// Titles: the first goes at once, the burst after it collapses into its last value.
	d.Publish("terminal.title", "a", "t1")
	d.Publish("terminal.title", "a", "t2")
	d.Publish("terminal.title", "a", "t3")
	// Another terminal is not held back by the first.
	d.Publish("terminal.title", "b", "other")
	now = now.Add(250 * time.Millisecond)
	timers.fire()
	evs, _ := l.After(0, 100)
	var got []any
	for _, e := range evs {
		got = append(got, e.Data)
	}
	if len(got) != 3 || got[0] != "t1" || got[1] != "other" || got[2] != "t3" {
		t.Fatalf("titles %v", got)
	}

	// Bells: one per two seconds, the rest dropped.
	base := l.Last()
	for i := 0; i < 5; i++ {
		d.Publish("terminal.bell", "a", nil)
		now = now.Add(100 * time.Millisecond)
	}
	now = now.Add(2 * time.Second)
	d.Publish("terminal.bell", "a", nil)
	timers.fire()
	if n := l.Last() - base; n != 2 {
		t.Fatalf("%d bells published, want 2", n)
	}

	// Kinds without a policy go straight through.
	base = l.Last()
	d.Publish("terminal.exited", "a", nil)
	d.Publish("terminal.exited", "a", nil)
	if l.Last()-base != 2 {
		t.Fatal("an unthrottled kind was throttled")
	}
}

func TestFlushPublishesPending(t *testing.T) {
	l := NewLog(100)
	d := NewDebouncer(l, Policies)
	now := time.Now()
	d.now = func() time.Time { return now }
	timers := &manualTimers{}
	d.after = timers.after
	d.Publish("terminal.title", "a", "first")
	d.Publish("terminal.title", "a", "last")
	d.Flush("a")
	evs, _ := l.After(0, 10)
	if len(evs) != 2 || evs[1].Data != "last" {
		t.Fatalf("after flush: %+v", evs)
	}
	timers.fire() // the stale timer finds nothing to do
	if l.Last() != 2 {
		t.Fatal("a flushed title was published twice")
	}
}
