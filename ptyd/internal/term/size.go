package term

import (
	"fmt"
	"slices"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/internal/emulator"
	"github.com/ascorblack/daedalus/ptyd/internal/wire"
)

// The size owner, as Info reports it: the host, a client ("human:<client id>"), or nobody ("") once
// the owning client left and no other client has a size to offer.
const ownerHost = "host"

// Size ownership. The most recently active client owns the PTY's size: every client says what size
// it would like (RESIZE), and the one that last resized or typed gets it; the others render at that
// size. Three rules keep one screen from wrecking another's, which is what went wrong with a pane
// that measured itself while hidden:
//   - a size below 20x4 or above 500x300 is refused with an `invalid_size` error and changes nothing;
//   - a read-only client never owns the size, it only watches;
//   - a client that reattaches after a resize gets a fresh screen, never bytes drawn for another size
//     (replayable).
//
// Lock order: t.sizeMu, then t.mu, then t.att.mu; none of them is held while waiting for the
// emulator except t.sizeMu.

// setSize applies a size for owner (nil: the host) and tells the clients when the grid or the
// owner changed.
func (t *Terminal) setSize(s Size, owner *Client) error {
	t.sizeMu.Lock()
	defer t.sizeMu.Unlock()
	if !t.Running() {
		return ErrExited
	}
	name := ownerHost
	if owner != nil {
		t.att.mu.Lock()
		present := slices.Contains(t.att.clients, owner)
		t.att.mu.Unlock()
		if !present {
			return nil // it left while its claim was on the way
		}
		name = "human:" + owner.ID
	}
	t.mu.Lock()
	grid := s.Cols != t.cols || s.Rows != t.rows
	pixels := s.PxW != t.pxW || s.PxH != t.pxH
	previous := t.sizeOwner
	t.mu.Unlock()
	if grid || pixels {
		if err := t.proc.Resize(s.Cols, s.Rows, s.PxW, s.PxH); err != nil {
			return err
		}
	}
	if grid {
		_ = t.WithEmulator(func(e emulator.Emulator) { e.Resize(s.Cols, s.Rows) })
	}
	t.mu.Lock()
	t.cols, t.rows, t.pxW, t.pxH = s.Cols, s.Rows, s.PxW, s.PxH
	if grid {
		// Everything from here on may be drawn for the new size; a client whose stream ends before
		// this point cannot be given the bytes after it.
		t.lastResizeSeq = t.ring.Head()
	}
	t.sizeOwner = name
	t.mu.Unlock()
	t.att.mu.Lock()
	t.att.owner = owner
	t.att.mu.Unlock()
	if grid || previous != name {
		t.broadcastSize()
	}
	return nil
}

// ownerLeft hands the size to next, the most recently active client left with a size of its own,
// or to nobody: the PTY keeps its size until someone claims it.
func (t *Terminal) ownerLeft(next *Client) {
	if next != nil {
		t.att.mu.Lock()
		d := *next.desired
		t.att.mu.Unlock()
		if err := t.setSize(d, next); err == nil {
			return
		}
	}
	t.sizeMu.Lock()
	t.mu.Lock()
	changed := t.sizeOwner != ""
	if t.sizeOwner != ownerHost {
		t.sizeOwner = ""
	}
	t.mu.Unlock()
	t.sizeMu.Unlock()
	if changed {
		t.broadcastSize()
	}
}

// successorLocked picks the next size owner: the most recently active client that may own the size
// and has said what size it wants. t.att.mu is held.
func (a *attachments) successorLocked() *Client {
	var best *Client
	for _, c := range a.clients {
		if c.desired == nil || c.readOnly.Load() {
			continue
		}
		if best == nil || c.lastActive.After(best.lastActive) {
			best = c
		}
	}
	return best
}

// ownerForLocked is the size owner as c sees it. t.mu is held.
func (t *Terminal) ownerForLocked(c *Client) string {
	if t.sizeOwner == ownerHost {
		return "host"
	}
	t.att.mu.Lock()
	own := t.att.owner == c
	t.att.mu.Unlock()
	if own {
		return "you"
	}
	return "other"
}

// broadcastSize tells every client the size and whose it is, read when the event is sent so a
// client that is behind gets the latest.
func (t *Terminal) broadcastSize() {
	t.broadcast("size", func(c *Client) any {
		return func() any {
			t.mu.Lock()
			defer t.mu.Unlock()
			return map[string]any{"type": "size", "cols": t.cols, "rows": t.rows, "owner": t.ownerForLocked(c)}
		}
	})
}

// checkClientSize refuses a size outside the daemon's range.
func checkClientSize(s Size) error {
	if s.Cols < config.MinCols || s.Rows < config.MinRows || s.Cols > config.MaxCols || s.Rows > config.MaxRows {
		return fmt.Errorf("%dx%d is outside %dx%d … %dx%d", s.Cols, s.Rows, config.MinCols, config.MinRows,
			config.MaxCols, config.MaxRows)
	}
	return nil
}

// resize handles a client's RESIZE: its wish is recorded and it becomes the owner.
func (c *Client) resize(s Size) error {
	t := c.t
	if c.readOnly.Load() || !t.Running() {
		return nil
	}
	if err := checkClientSize(s); err != nil {
		return c.errorEvent("invalid_size", err.Error())
	}
	t.att.mu.Lock()
	c.desired = &s
	c.lastActive = t.deps.Clock.Now()
	t.att.mu.Unlock()
	if err := t.setSize(s, c); err != nil && err != ErrExited {
		return c.errorEvent("resize_failed", err.Error())
	}
	return nil
}

// input handles a client's keystrokes. A client that types while someone else owns the size takes
// it back first, so the program redraws for the screen the person is looking at before it reads the
// keystroke.
func (c *Client) input(p []byte) {
	t := c.t
	if c.readOnly.Load() || !t.Running() || len(p) == 0 {
		return
	}
	if t.replyEcho(c, p) {
		return
	}
	t.att.mu.Lock()
	c.lastActive = t.deps.Clock.Now()
	var claim *Size
	if c.desired != nil && t.att.owner != c {
		d := *c.desired
		claim = &d
	}
	t.att.mu.Unlock()
	if claim != nil {
		_ = t.setSize(*claim, c)
	}
	t.HumanInput(p)
}

// OwnerFacts are what the size owner told the daemon about its screen: the answerer uses them for
// the queries only a screen can answer (pixel sizes, colours).
type OwnerFacts struct {
	Cols, Rows int
	PxW, PxH   int
	// Theme is the owner's colours; with the host or nobody as the owner, those of the most recently
	// active client that sent any. Nil when no client did.
	Theme *wire.Theme
}

// OwnerFacts returns the size owner's facts. It is safe to call from the emulator goroutine.
func (t *Terminal) OwnerFacts() OwnerFacts {
	t.mu.Lock()
	f := OwnerFacts{Cols: t.cols, Rows: t.rows, PxW: t.pxW, PxH: t.pxH}
	t.mu.Unlock()
	a := &t.att
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.owner != nil && a.owner.theme != nil {
		th := *a.owner.theme
		f.Theme = &th
		return f
	}
	var best *Client
	for _, c := range a.clients {
		if c.theme != nil && (best == nil || c.lastActive.After(best.lastActive)) {
			best = c
		}
	}
	if best != nil {
		th := *best.theme
		f.Theme = &th
	}
	return f
}
