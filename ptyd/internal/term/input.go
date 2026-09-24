package term

import (
	"context"
	"errors"
	"io"
	"sync"
	"time"
)

// chunkBytes is the most one write to the PTY carries. A large agent write is delivered in chunks so
// that a human keystroke arriving meanwhile goes in between them instead of waiting behind a
// megabyte.
const chunkBytes = 4 << 10

// Keyboard owners.
const (
	OwnerAuto  = "auto"  // agents wait for the human to be idle
	OwnerHuman = "human" // agents wait until the grant ends
	OwnerAgent = "agent" // agents go without waiting, until a human types
)

// KeyboardState is who may type, and until when (zero for no expiry).
type KeyboardState struct {
	Owner string    `json:"owner"`
	Until time.Time `json:"until,omitzero"`
}

// Errors of an agent write.
var (
	ErrExited       = errors.New("the terminal has exited")
	ErrWriteTimeout = errors.New("timed out waiting for the keyboard")
	ErrKeyboardHeld = errors.New("a human holds the keyboard")
)

// Receipt says that an agent write reached the PTY. It never says that the program read it, let
// alone acted on it.
type Receipt struct {
	Bytes       int       `json:"bytes"`
	SeqBefore   int64     `json:"seq_before"` // the output offset when the first byte was written
	QueuedMs    int64     `json:"queued_ms"`
	DeliveredAt time.Time `json:"delivered_at"`
}

type agentWrite struct {
	data     []byte
	off      int
	wait     bool // wait for the keyboard
	queued   time.Time
	deadline time.Time
	ctx      context.Context
	receipt  Receipt
	done     chan error
}

// input is one terminal's writer: a single goroutine owns the PTY's write side and takes, in order
// of priority, answers to terminal queries, human input, and agent writes that arbitration lets
// through.
type input struct {
	w     io.Writer
	clock Clock
	idle  time.Duration
	head  func() int64 // the output offset, for receipts

	mu        sync.Mutex
	replies   [][]byte
	replyLen  int // bytes queued in replies
	human     [][]byte
	agent     []*agentWrite
	keyboard  KeyboardState
	lastHuman time.Time
	closed    bool
	wake      chan struct{}

	// onDelivered is called after each completed agent write, outside the lock.
	onDelivered func(a *agentWrite)
	// onKeyboard is called when the keyboard state changes, outside the lock.
	onKeyboard func(KeyboardState)
}

func newInput(w io.Writer, clock Clock, idle time.Duration, head func() int64) *input {
	return &input{w: w, clock: clock, idle: idle, head: head, keyboard: KeyboardState{Owner: OwnerAuto}, wake: make(chan struct{}, 1)}
}

func (in *input) poke() {
	select {
	case in.wake <- struct{}{}:
	default:
	}
}

// maxQueuedReplies bounds the answers waiting to be written. A program that asks and never reads
// (`cat` of a file full of cursor queries is enough) stops taking input once the kernel's buffer is
// full; without a bound every further query would queue its answer in the daemon for good. Past the
// bound answers are dropped, which is what that program would see from a real terminal whose input
// it does not read.
const maxQueuedReplies = 64 << 10

// Reply queues an answer to a terminal query ahead of everything else.
func (in *input) Reply(p []byte) {
	in.mu.Lock()
	if !in.closed && in.replyLen+len(p) <= maxQueuedReplies {
		in.replies = append(in.replies, append([]byte(nil), p...))
		in.replyLen += len(p)
	}
	in.mu.Unlock()
	in.poke()
}

// Human queues a human's keystrokes. They go straight through, and they take the keyboard: agents
// now wait for the idle time again, and a grant to agents ends.
func (in *input) Human(p []byte) {
	in.mu.Lock()
	if in.closed {
		in.mu.Unlock()
		return
	}
	in.human = append(in.human, append([]byte(nil), p...))
	in.lastHuman = in.clock.Now()
	var changed *KeyboardState
	if in.keyboard.Owner == OwnerAgent {
		in.keyboard = KeyboardState{Owner: OwnerAuto}
		k := in.keyboard
		changed = &k
	}
	in.mu.Unlock()
	in.poke()
	if changed != nil && in.onKeyboard != nil {
		in.onKeyboard(*changed)
	}
}

// LastHuman is the time of the last human keystroke, zero if none.
func (in *input) LastHuman() time.Time {
	in.mu.Lock()
	defer in.mu.Unlock()
	return in.lastHuman
}

// SetKeyboard changes the owner. ttl of zero means until changed.
func (in *input) SetKeyboard(owner string, ttl time.Duration) KeyboardState {
	in.mu.Lock()
	in.keyboard = KeyboardState{Owner: owner}
	if ttl > 0 && owner != OwnerAuto {
		in.keyboard.Until = in.clock.Now().Add(ttl)
	}
	k := in.keyboard
	in.mu.Unlock()
	in.poke()
	if in.onKeyboard != nil {
		in.onKeyboard(k)
	}
	return k
}

// Keyboard returns the current state, with an expired grant shown as auto.
func (in *input) Keyboard() KeyboardState {
	in.mu.Lock()
	defer in.mu.Unlock()
	return in.effective(in.clock.Now())
}

func (in *input) effective(now time.Time) KeyboardState {
	k := in.keyboard
	if !k.Until.IsZero() && !now.Before(k.Until) {
		return KeyboardState{Owner: OwnerAuto}
	}
	return k
}

// agentAllowed says whether an agent may type now, and if not, when to look again (zero: only when
// something changes).
func (in *input) agentAllowed(now time.Time) (bool, time.Time) {
	k := in.effective(now)
	switch k.Owner {
	case OwnerAgent:
		return true, time.Time{}
	case OwnerHuman:
		return false, k.Until
	}
	if in.lastHuman.IsZero() {
		return true, time.Time{}
	}
	at := in.lastHuman.Add(in.idle)
	if !now.Before(at) {
		return true, time.Time{}
	}
	return false, at
}

// Agent queues an agent write and waits for it to be delivered, for the deadline, or for ctx.
func (in *input) Agent(ctx context.Context, data []byte, wait bool, timeout time.Duration) (Receipt, error) {
	now := in.clock.Now()
	a := &agentWrite{data: data, wait: wait, queued: now, deadline: now.Add(timeout), ctx: ctx, done: make(chan error, 1)}
	in.mu.Lock()
	if in.closed {
		in.mu.Unlock()
		return Receipt{}, ErrExited
	}
	in.agent = append(in.agent, a)
	in.mu.Unlock()
	in.poke()
	// The writer goroutine settles every queued write, including on close, so this cannot hang.
	err := <-a.done
	return a.receipt, err
}

// Close fails every queued agent write and stops the writer.
func (in *input) Close() {
	in.mu.Lock()
	in.closed = true
	in.mu.Unlock()
	in.poke()
}

// run is the writer goroutine.
func (in *input) run() {
	var timer Timer
	stop := func() {
		if timer != nil {
			timer.Stop()
			timer = nil
		}
	}
	defer stop()
	for {
		in.mu.Lock()
		if in.closed {
			pending := in.agent
			in.agent, in.human, in.replies, in.replyLen = nil, nil, nil, 0
			in.mu.Unlock()
			for _, a := range pending {
				a.done <- ErrExited
			}
			return
		}
		if len(in.replies) > 0 {
			p := in.replies[0]
			in.replies = in.replies[1:]
			in.replyLen -= len(p)
			in.mu.Unlock()
			in.write(p)
			continue
		}
		if len(in.human) > 0 {
			p := in.human[0]
			if len(p) > chunkBytes {
				in.human[0] = p[chunkBytes:]
				p = p[:chunkBytes]
			} else {
				in.human = in.human[1:]
			}
			in.mu.Unlock()
			in.write(p)
			continue
		}
		if len(in.agent) == 0 {
			in.mu.Unlock()
			stop()
			<-in.wake
			continue
		}
		a := in.agent[0]
		now := in.clock.Now()
		if a.off == 0 {
			// Not started: it may still give up, and it waits for the keyboard. Once started, a write
			// is finished without waiting again, so a keystroke cannot split an agent's text.
			if err := a.ctx.Err(); err != nil {
				in.agent = in.agent[1:]
				in.mu.Unlock()
				a.done <- err
				continue
			}
			ok, retry := true, time.Time{}
			if a.wait {
				ok, retry = in.agentAllowed(now)
			}
			if !ok {
				if !now.Before(a.deadline) {
					in.agent = in.agent[1:]
					err := ErrWriteTimeout
					if in.effective(now).Owner == OwnerHuman {
						err = ErrKeyboardHeld
					}
					in.mu.Unlock()
					a.done <- err
					continue
				}
				wakeAt := a.deadline
				if !retry.IsZero() && retry.Before(wakeAt) {
					wakeAt = retry
				}
				in.mu.Unlock()
				stop()
				timer = in.clock.NewTimer(wakeAt.Sub(now))
				select {
				case <-in.wake:
				case <-timer.C():
				case <-a.ctx.Done():
				}
				continue
			}
			a.receipt.SeqBefore = in.head()
			a.receipt.QueuedMs = now.Sub(a.queued).Milliseconds()
		}
		end := min(len(a.data), a.off+chunkBytes)
		p := a.data[a.off:end]
		a.off = end
		finished := a.off == len(a.data)
		if finished {
			in.agent = in.agent[1:]
		}
		in.mu.Unlock()
		err := in.write(p)
		if err != nil {
			if !finished {
				in.mu.Lock()
				if len(in.agent) > 0 && in.agent[0] == a {
					in.agent = in.agent[1:]
				}
				in.mu.Unlock()
			}
			a.done <- ErrExited
			continue
		}
		if finished {
			a.receipt.Bytes = len(a.data)
			a.receipt.DeliveredAt = in.clock.Now().UTC()
			if in.onDelivered != nil {
				in.onDelivered(a)
			}
			a.done <- nil
		}
	}
}

func (in *input) write(p []byte) error {
	for len(p) > 0 {
		n, err := in.w.Write(p)
		if err != nil {
			return err
		}
		p = p[n:]
	}
	return nil
}
