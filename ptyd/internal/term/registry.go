package term

import (
	"errors"
	"sort"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
)

// Registry errors.
var (
	ErrNotFound = errors.New("no such terminal")
	ErrExists   = errors.New("a terminal with this id exists")
	ErrLimit    = errors.New("too many running terminals")
	ErrRunning  = errors.New("the terminal is still running")
)

// Registry is every terminal of the daemon.
type Registry struct {
	deps         Deps
	maxTerminals int

	mu    sync.Mutex
	terms map[string]*Terminal
}

// NewRegistry returns an empty registry.
func NewRegistry(deps Deps, maxTerminals int) *Registry {
	return &Registry{deps: deps, maxTerminals: maxTerminals, terms: map[string]*Terminal{}}
}

// Create starts a terminal. Ids are the host's: a clash is an error, not a replacement, because the
// host would otherwise lose track of a live terminal.
func (r *Registry) Create(spec Spec) (*Terminal, error) {
	r.mu.Lock()
	if _, ok := r.terms[spec.ID]; ok {
		r.mu.Unlock()
		return nil, ErrExists
	}
	running := 0
	for _, t := range r.terms {
		// A nil entry is an id reserved by a create in progress; it counts, since it is about to run.
		if t == nil || t.Running() {
			running++
		}
	}
	if running >= r.maxTerminals {
		r.mu.Unlock()
		return nil, ErrLimit
	}
	// Reserve the id while the program starts, so two creates with one id cannot both succeed.
	r.terms[spec.ID] = nil
	r.mu.Unlock()

	t, err := Start(spec, r.deps)

	r.mu.Lock()
	defer r.mu.Unlock()
	if err != nil {
		delete(r.terms, spec.ID)
		return nil, err
	}
	r.terms[spec.ID] = t
	return t, nil
}

// Get returns a terminal.
func (r *Registry) Get(id string) (*Terminal, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	t := r.terms[id]
	if t == nil {
		return nil, ErrNotFound
	}
	return t, nil
}

// List returns every terminal, oldest first.
func (r *Registry) List() []*Terminal {
	r.mu.Lock()
	out := make([]*Terminal, 0, len(r.terms))
	for _, t := range r.terms {
		if t != nil {
			out = append(out, t)
		}
	}
	r.mu.Unlock()
	sort.Slice(out, func(i, j int) bool {
		if !out[i].CreatedAt.Equal(out[j].CreatedAt) {
			return out[i].CreatedAt.Before(out[j].CreatedAt)
		}
		return out[i].ID < out[j].ID
	})
	return out
}

// Forget drops an exited terminal and its screen.
func (r *Registry) Forget(id string) error {
	r.mu.Lock()
	t := r.terms[id]
	if t == nil {
		r.mu.Unlock()
		return ErrNotFound
	}
	if t.Running() {
		r.mu.Unlock()
		return ErrRunning
	}
	delete(r.terms, id)
	r.mu.Unlock()
	t.forget()
	return nil
}

// Counts returns how many terminals run and how many have exited.
func (r *Registry) Counts() (running, exited int) {
	for _, t := range r.List() {
		if t.Running() {
			running++
		} else {
			exited++
		}
	}
	return running, exited
}

// Reap forgets terminals that exited more than keep ago.
func (r *Registry) Reap(now time.Time, keep time.Duration) {
	for _, t := range r.List() {
		if t.Running() {
			continue
		}
		t.mu.Lock()
		old := now.Sub(t.exitedAt) > keep
		t.mu.Unlock()
		if old {
			_ = r.Forget(t.ID)
		}
	}
}

// RunReaper reaps once a minute until stop is closed.
func (r *Registry) RunReaper(stop <-chan struct{}) {
	tick := time.NewTicker(time.Minute)
	defer tick.Stop()
	for {
		select {
		case <-stop:
			return
		case now := <-tick.C:
			r.Reap(now.UTC(), config.ExitedKeep)
		}
	}
}

// Shutdown ends every running terminal at once, each with the grace, and waits for all of them.
func (r *Registry) Shutdown(grace time.Duration) {
	var wg sync.WaitGroup
	for _, t := range r.List() {
		if !t.Running() {
			continue
		}
		wg.Add(1)
		go func() {
			defer wg.Done()
			t.Kill(grace)
		}()
	}
	wg.Wait()
	for _, t := range r.List() {
		t.forget()
	}
}
