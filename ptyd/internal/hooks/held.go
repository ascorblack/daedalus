package hooks

import (
	"sync"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
)

// Reply is the host's answer to a held post.
type Reply struct {
	Status      int
	ContentType string
	Body        []byte
	Gone        bool // the launch ended while the post waited
}

// held is one post waiting for its reply. Its channel takes exactly one value: the reply, or the
// launch's end; whichever comes second finds it full and is dropped.
type held struct {
	id     string
	launch *Launch
	ch     chan Reply
}

func (h *held) deliver(r Reply) {
	select {
	case h.ch <- r:
	default:
	}
}

// hold registers a post that waits for a reply. It fails when the launch or the daemon already has
// as many posts waiting as it may.
func (r *Registry) hold(l *Launch) (*held, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if l.closed {
		return nil, ErrNoLaunch
	}
	if len(l.held) >= config.MaxHeldPerLaunch || r.nheld >= config.MaxHeld {
		return nil, ErrTooMany
	}
	h := &held{id: "r" + randomHex(8), launch: l, ch: make(chan Reply, 1)}
	l.held[h.id] = h
	r.replies[h.id] = h
	r.nheld++
	return h, nil
}

// release forgets a held post that stopped waiting (its time ran out, or its client went away).
func (r *Registry) release(h *held) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.replies[h.id] == h {
		delete(r.replies, h.id)
		delete(h.launch.held, h.id)
		r.nheld--
	}
}

// Reply answers a held post. launchID, when given, must be the post's launch: a reply meant for one
// worker can never answer another's question. It fails with ErrNoReply when nothing waits under
// that id any more — answered, timed out, or its CLI gone — which the host must hear, since its
// answer then reached nobody.
func (r *Registry) Reply(launchID, replyID string, reply Reply) error {
	r.mu.Lock()
	h := r.replies[replyID]
	if h == nil || (launchID != "" && h.launch.ID != launchID) {
		r.mu.Unlock()
		return ErrNoReply
	}
	delete(r.replies, replyID)
	delete(h.launch.held, replyID)
	r.nheld--
	r.mu.Unlock()
	h.deliver(reply)
	return nil
}

// Held is the number of posts waiting for a reply.
func (r *Registry) Held() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.nheld
}

// bucket is a token bucket: rate tokens a second, at most burst at once.
type bucket struct {
	mu     sync.Mutex
	rate   float64
	burst  float64
	tokens float64
	last   time.Time
	now    func() time.Time
}

func newBucket(rate, burst int) *bucket {
	return &bucket{rate: float64(rate), burst: float64(burst), tokens: float64(burst), now: time.Now}
}

func (b *bucket) allow() bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	now := b.now()
	if !b.last.IsZero() {
		b.tokens = min(b.burst, b.tokens+now.Sub(b.last).Seconds()*b.rate)
	}
	b.last = now
	if b.tokens < 1 {
		return false
	}
	b.tokens--
	return true
}
