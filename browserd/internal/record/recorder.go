package record

import (
	"context"
	"crypto/sha256"
	"log/slog"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// ChangeInterval is how often a recording group's page is looked at for a change between actions.
const ChangeInterval = 5 * time.Second

// Shooter captures a tab's viewport as a JPEG with its secret fields masked (the page model's
// screenshot), so a recording never holds what the agent may not read.
type Shooter func(ctx context.Context, t *browser.Tab) (jpeg []byte, w, h int, err error)

// State is a group's recording switch: frames on or off, and whether a person's own driving is
// recorded too.
type State struct {
	GroupID string `json:"group_id"`
	Frames  bool   `json:"frames"`
	Human   bool   `json:"human"`
}

type groupState struct {
	State
	last [32]byte
	busy bool
}

// Recorder takes the keyframes of the groups whose recording is on.
type Recorder struct {
	Store  *Store
	Shoot  Shooter
	Groups func(id string) (*browser.Group, error)
	Limits func() (maxBytes int64, retention time.Duration)
	Log    *slog.Logger

	mu     sync.Mutex
	groups map[string]*groupState
}

// Set switches a group's recording. The frames already taken stay until they expire or are deleted.
func (r *Recorder) Set(g *browser.Group, frames, human bool) State {
	r.mu.Lock()
	if r.groups == nil {
		r.groups = map[string]*groupState{}
	}
	st := r.groups[g.ID]
	if st == nil {
		st = &groupState{State: State{GroupID: g.ID}}
		r.groups[g.ID] = st
	}
	was := st.Frames
	st.Frames, st.Human = frames, human
	out := st.State
	r.mu.Unlock()
	if frames && !was {
		if t := g.ActiveTab(); t != nil {
			go r.capture(t, "start", "")
		}
	}
	return out
}

// State is a group's switch as it is.
func (r *Recorder) State(groupID string) State {
	r.mu.Lock()
	defer r.mu.Unlock()
	if st := r.groups[groupID]; st != nil {
		return st.State
	}
	return State{GroupID: groupID}
}

// Forget drops a closed group's switch; its frames stay on disk.
func (r *Recorder) Forget(groupID string) {
	r.mu.Lock()
	delete(r.groups, groupID)
	r.mu.Unlock()
}

// AfterAction takes the keyframe of what an action left, when the group records.
func (r *Recorder) AfterAction(t *browser.Tab, actionID string) {
	if !r.State(t.Group.ID).Frames {
		return
	}
	go r.capture(t, "action", actionID)
}

// capture takes one frame of t. A frame for the change interval is kept only when the picture
// differs from the group's last one; an action's is always kept, so every row of the action log has
// its picture. Nothing is taken while a person drives unless they chose to be recorded.
func (r *Recorder) capture(t *browser.Tab, kind, actionID string) {
	g := t.Group
	r.mu.Lock()
	st := r.groups[g.ID]
	if st == nil || !st.Frames || (st.busy && kind == "change") {
		r.mu.Unlock()
		return
	}
	if g.Control().Owner == browser.OwnerHuman && !st.Human {
		r.mu.Unlock()
		return
	}
	st.busy = true
	r.mu.Unlock()
	defer func() {
		r.mu.Lock()
		st.busy = false
		r.mu.Unlock()
	}()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	jpeg, w, h, err := r.Shoot(ctx, t)
	if err != nil {
		if r.Log != nil {
			r.Log.Debug("keyframe not taken", "group", g.ID, "tab", t.ID, "error", err.Error())
		}
		return
	}
	sum := sha256.Sum256(jpeg)
	r.mu.Lock()
	same := sum == st.last
	st.last = sum
	r.mu.Unlock()
	if same && kind == "change" {
		return
	}
	maxBytes, _ := r.Limits()
	if _, err := r.Store.Put(g.ID, Frame{At: time.Now().UnixMilli(), Tab: t.ID, URL: t.URL(), Kind: kind, ActionID: actionID, W: w, H: h}, jpeg, maxBytes); err != nil && r.Log != nil {
		r.Log.Warn("keyframe not written", "group", g.ID, "error", err.Error())
	}
}

// Run looks at every recording group's active tab each ChangeInterval, and sweeps the store every
// ten minutes, until stop is closed.
func (r *Recorder) Run(stop <-chan struct{}) {
	tick := time.NewTicker(ChangeInterval)
	defer tick.Stop()
	sweep := time.NewTicker(10 * time.Minute)
	defer sweep.Stop()
	r.sweep()
	for {
		select {
		case <-stop:
			return
		case <-sweep.C:
			r.sweep()
		case <-tick.C:
			r.mu.Lock()
			var ids []string
			for id, st := range r.groups {
				if st.Frames {
					ids = append(ids, id)
				}
			}
			r.mu.Unlock()
			for _, id := range ids {
				g, err := r.Groups(id)
				if err != nil {
					r.Forget(id)
					continue
				}
				if t := g.ActiveTab(); t != nil {
					go r.capture(t, "change", "")
				}
			}
		}
	}
}

func (r *Recorder) sweep() {
	maxBytes, retention := r.Limits()
	if n := r.Store.Sweep(time.Now(), retention, maxBytes); n > 0 && r.Log != nil {
		r.Log.Info("recorded keyframes expired", "frames", n)
	}
}

// ErrNoFrame is record.read's answer for a frame that is not kept.
func ErrNoFrame(group string, no int64) error {
	return wire.Errorf(wire.CodeNotFound, "group %q has no keyframe %d (it may have expired)", group, no)
}
