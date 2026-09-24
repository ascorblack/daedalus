package term

import (
	"context"
	"regexp"
	"strings"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/emulator"
)

// waitPoll is how often a wait re-evaluates. A screen regex is only re-run when output arrived since
// the last run, so a quiet terminal costs a clock read per tick; a busy one is matched at most this
// often, not once per chunk.
const waitPoll = 50 * time.Millisecond

// waitOutputKeep bounds the output a regex over output is matched against: the most recent stripped
// text since the wait began.
const waitOutputKeep = 1 << 20

// WaitSpec is what `terminal.wait_for` waits for; the first condition met ends the wait.
type WaitSpec struct {
	Regex    *regexp.Regexp // nil for none
	Output   bool           // match Regex against the output since SinceSeq instead of the screen
	SinceSeq int64          // where output matching starts; negative for the output head at the call
	Idle     time.Duration  // no output for this long; 0 for no idle condition
	// CommandDone waits for a command to end (its D mark) after SinceSeq, or after the output head at
	// the call. Giving the offset a write's receipt reported closes the race with a command that ends
	// before the wait begins.
	CommandDone bool
	Timeout     time.Duration
}

// WaitResult says which condition ended a wait.
type WaitResult struct {
	Matched string // regex | idle | command_done | exited | timeout
	Seq     int64  // the output head when it ended (for command_done, the offset after the D mark)
	Match   string // the text the regex matched
	Command *CommandRecord
}

// WaitFor blocks until one of the conditions holds, the terminal exits, or the timeout passes.
func (t *Terminal) WaitFor(ctx context.Context, w WaitSpec) (WaitResult, error) {
	start := t.deps.Clock.Now()
	deadline := time.NewTimer(w.Timeout)
	defer deadline.Stop()
	tick := time.NewTicker(waitPoll)
	defer tick.Stop()

	pos := w.SinceSeq
	if pos < 0 {
		pos = t.ring.Head()
	}
	doneAfter := pos
	var out strings.Builder
	lastScreen := int64(-1)
	check := func() (WaitResult, bool) {
		head := t.ring.Head()
		if w.CommandDone {
			if r, ok := t.CommandDoneAfter(doneAfter); ok {
				return WaitResult{Matched: "command_done", Seq: *r.EndSeq, Command: &r}, true
			}
		}
		if w.Regex != nil && w.Output && pos < head {
			for pos < head {
				data, from, to, _ := t.ReadOutput(pos, 256<<10, true)
				if to <= from {
					break
				}
				out.Write(data)
				pos = to
			}
			if out.Len() > waitOutputKeep {
				kept := out.String()[out.Len()-waitOutputKeep:]
				out.Reset()
				out.WriteString(kept)
			}
			text := out.String()
			if loc := w.Regex.FindStringIndex(text); loc != nil {
				return WaitResult{Matched: "regex", Seq: pos, Match: text[loc[0]:loc[1]]}, true
			}
		}
		if w.Regex != nil && !w.Output && head != lastScreen {
			lastScreen = head
			if text, seq, ok := t.screenText(); ok {
				if loc := w.Regex.FindStringIndex(text); loc != nil {
					return WaitResult{Matched: "regex", Seq: seq, Match: text[loc[0]:loc[1]]}, true
				}
			}
		}
		if w.Idle > 0 {
			t.mu.Lock()
			quietSince := t.lastOutput
			t.mu.Unlock()
			if quietSince.Before(start) {
				quietSince = start
			}
			if t.deps.Clock.Now().Sub(quietSince) >= w.Idle {
				return WaitResult{Matched: "idle", Seq: head}, true
			}
		}
		return WaitResult{}, false
	}
	for {
		if r, ok := check(); ok {
			return r, nil
		}
		select {
		case <-ctx.Done():
			return WaitResult{}, ctx.Err()
		case <-deadline.C:
			return WaitResult{Matched: "timeout", Seq: t.ring.Head()}, nil
		case <-t.done:
			// The exit is published only after every byte was fed, so a last look sees the end.
			if r, ok := check(); ok {
				return r, nil
			}
			return WaitResult{Matched: "exited", Seq: t.ring.Head()}, nil
		case <-tick.C:
		}
	}
}

// screenText is the visible screen as text, and the output offset it shows.
func (t *Terminal) screenText() (string, int64, bool) {
	var text string
	var seq int64
	err := t.WithEmulator(func(e emulator.Emulator) {
		c := e.Cursor()
		_, rows := e.Size()
		top := c.AbsRow - int64(c.Y)
		text = strings.Join(e.Text(top, top+int64(rows)), "\n")
		seq = t.fed.Load()
	})
	return text, seq, err == nil
}
