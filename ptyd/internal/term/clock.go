package term

import (
	"sync"
	"time"
)

// Clock is the time source of input arbitration, replaceable so that a test of "an agent write waits
// ten seconds after a keystroke" does not take ten seconds.
type Clock interface {
	Now() time.Time
	NewTimer(d time.Duration) Timer
}

// Timer is the part of time.Timer that arbitration uses.
type Timer interface {
	C() <-chan time.Time
	Stop() bool
}

// RealClock is the wall clock.
type RealClock struct{}

func (RealClock) Now() time.Time { return time.Now() }

func (RealClock) NewTimer(d time.Duration) Timer { return realTimer{time.NewTimer(d)} }

type realTimer struct{ t *time.Timer }

func (r realTimer) C() <-chan time.Time { return r.t.C }
func (r realTimer) Stop() bool          { return r.t.Stop() }

// FakeClock is a clock that moves only when told to.
type FakeClock struct {
	mu     sync.Mutex
	now    time.Time
	timers []*fakeTimer
}

// NewFakeClock returns a fake clock at a fixed instant.
func NewFakeClock() *FakeClock {
	return &FakeClock{now: time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)}
}

func (f *FakeClock) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
}

func (f *FakeClock) NewTimer(d time.Duration) Timer {
	f.mu.Lock()
	defer f.mu.Unlock()
	t := &fakeTimer{at: f.now.Add(d), c: make(chan time.Time, 1), clock: f}
	if d <= 0 {
		t.c <- f.now
		t.fired = true
	} else {
		f.timers = append(f.timers, t)
	}
	return t
}

// Advance moves the clock forward and fires the timers that fall due.
func (f *FakeClock) Advance(d time.Duration) {
	f.mu.Lock()
	f.now = f.now.Add(d)
	keep := f.timers[:0]
	for _, t := range f.timers {
		if !t.at.After(f.now) {
			t.fired = true
			t.c <- f.now
		} else {
			keep = append(keep, t)
		}
	}
	f.timers = keep
	f.mu.Unlock()
}

// Timers is how many timers are waiting, so a test can tell that a goroutine has gone to sleep.
func (f *FakeClock) Timers() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.timers)
}

type fakeTimer struct {
	at    time.Time
	c     chan time.Time
	fired bool
	clock *FakeClock
}

func (t *fakeTimer) C() <-chan time.Time { return t.c }

func (t *fakeTimer) Stop() bool {
	f := t.clock
	f.mu.Lock()
	defer f.mu.Unlock()
	for i, x := range f.timers {
		if x == t {
			f.timers = append(f.timers[:i], f.timers[i+1:]...)
			return true
		}
	}
	return false
}
