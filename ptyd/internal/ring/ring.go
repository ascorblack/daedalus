// Package ring keeps the tail of a terminal's output, addressed by absolute byte offset.
package ring

import "sync"

// Ring holds the last Cap bytes written to it. Every byte has an absolute sequence number, its
// offset in the whole stream since the terminal started, so a reader can say "everything after
// byte N" across any number of wraps and learn exactly what it missed.
//
// The buffer starts small and doubles as output arrives, up to the capacity, so an idle shell costs
// kilobytes rather than the full capacity. It is safe for concurrent use.
type Ring struct {
	mu    sync.RWMutex
	buf   []byte // circular storage; len(buf) is the current allocation
	first int    // index in buf of the oldest byte held
	n     int    // bytes held
	cap   int    // the most buf may grow to
	head  int64  // offset one past the last byte written
}

const initialBytes = 16 << 10

// New returns a ring that keeps at most capacity bytes.
func New(capacity int) *Ring {
	return &Ring{cap: max(1, capacity)}
}

// Write appends p to the stream. It never fails and never blocks on readers for longer than the
// copy.
func (r *Ring) Write(p []byte) {
	if len(p) == 0 {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.head += int64(len(p))
	if len(p) >= r.cap {
		// Only the last Cap bytes can survive.
		p = p[len(p)-r.cap:]
		r.resize(r.cap, 0)
		copy(r.buf, p)
		r.first, r.n = 0, len(p)
		return
	}
	if need := r.n + len(p); need > len(r.buf) && len(r.buf) < r.cap {
		r.resize(min(r.cap, max(need, 2*len(r.buf), initialBytes)), r.n)
	}
	size := len(r.buf)
	// Drop the oldest bytes that the new ones displace.
	if over := r.n + len(p) - size; over > 0 {
		r.first = (r.first + over) % size
		r.n -= over
	}
	end := (r.first + r.n) % size
	c := copy(r.buf[end:], p)
	copy(r.buf, p[c:])
	r.n += len(p)
}

// resize reallocates buf to size bytes, keeping the newest keep bytes in order at its start.
func (r *Ring) resize(size, keep int) {
	nb := make([]byte, size)
	keep = min(keep, r.n, size)
	r.copyOut(nb[:keep], r.n-keep)
	r.buf, r.first, r.n = nb, 0, keep
}

// copyOut fills dst with the held bytes starting skip bytes after the oldest one.
func (r *Ring) copyOut(dst []byte, skip int) {
	if len(dst) == 0 {
		return
	}
	i := (r.first + skip) % len(r.buf)
	c := copy(dst, r.buf[i:])
	copy(dst[c:], r.buf)
}

// Head is the offset one past the last byte written.
func (r *Ring) Head() int64 {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.head
}

// Start is the offset of the oldest byte still held.
func (r *Ring) Start() int64 {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.head - int64(r.n)
}

// ReadFrom copies up to max bytes starting at offset seq. When seq is older than the oldest byte
// held, the read starts at the oldest byte instead and gap is true: the caller lost something and
// must be told. A seq at or beyond the head returns nothing, from the head.
func (r *Ring) ReadFrom(seq int64, max int) (data []byte, from int64, gap bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	start := r.head - int64(r.n)
	if seq < start {
		seq, gap = start, true
	}
	if seq >= r.head || max <= 0 {
		return nil, min(seq, r.head), gap
	}
	data = make([]byte, min(int64(max), r.head-seq))
	r.copyOut(data, int(seq-start))
	return data, seq, gap
}

// Shrink keeps only the newest keep bytes, releases the rest of the allocation, and lowers the
// capacity to keep. Offsets are unchanged: the start simply moves forward.
func (r *Ring) Shrink(keep int) {
	r.mu.Lock()
	defer r.mu.Unlock()
	keep = max(1, keep)
	if keep >= r.cap {
		return
	}
	r.cap = keep
	r.resize(min(keep, max(r.n, 1)), r.n)
}
