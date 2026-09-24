package ring

import (
	"bytes"
	"math/rand"
	"testing"
)

// model is the obviously correct ring: the whole stream, and a capacity.
type model struct {
	all []byte
	cap int
}

func (m *model) start() int64 { return int64(max(0, len(m.all)-m.cap)) }

func (m *model) read(seq int64, n int) ([]byte, int64, bool) {
	gap := false
	if seq < m.start() {
		seq, gap = m.start(), true
	}
	head := int64(len(m.all))
	if seq >= head || n <= 0 {
		return nil, min(seq, head), gap
	}
	end := min(head, seq+int64(n))
	return m.all[seq:end], seq, gap
}

func check(t *testing.T, r *Ring, m *model, seq int64, n int) {
	t.Helper()
	got, from, gap := r.ReadFrom(seq, n)
	want, wfrom, wgap := m.read(seq, n)
	if !bytes.Equal(got, want) || from != wfrom || gap != wgap {
		t.Fatalf("ReadFrom(%d, %d) = %d bytes from %d gap %v; want %d bytes from %d gap %v",
			seq, n, len(got), from, gap, len(want), wfrom, wgap)
	}
}

func TestEveryBoundary(t *testing.T) {
	const capacity = 64
	r, m := New(capacity), &model{cap: capacity}
	b := byte(0)
	for step := 0; step < 300; step++ {
		p := make([]byte, step%23)
		for i := range p {
			p[i] = b
			b++
		}
		r.Write(p)
		m.all = append(m.all, p...)
		if r.Head() != int64(len(m.all)) || r.Start() != m.start() {
			t.Fatalf("head %d start %d, want %d %d", r.Head(), r.Start(), len(m.all), m.start())
		}
		// Read at every offset from before the start to past the head, with several lengths.
		for seq := m.start() - 2; seq <= int64(len(m.all))+2; seq++ {
			for _, n := range []int{0, 1, 7, capacity, capacity * 2} {
				check(t, r, m, seq, n)
			}
		}
	}
}

func TestWriteLargerThanCapacity(t *testing.T) {
	r, m := New(10), &model{cap: 10}
	for _, n := range []int{3, 25, 10, 1, 9} {
		p := bytes.Repeat([]byte{byte('a' + n)}, n)
		for i := range p {
			p[i] += byte(i % 3)
		}
		r.Write(p)
		m.all = append(m.all, p...)
		check(t, r, m, 0, 100)
		check(t, r, m, m.start(), 100)
	}
}

func TestRandomAgainstModel(t *testing.T) {
	rng := rand.New(rand.NewSource(1))
	for round := 0; round < 50; round++ {
		capacity := 1 + rng.Intn(5000)
		r, m := New(capacity), &model{cap: capacity}
		for step := 0; step < 200; step++ {
			p := make([]byte, rng.Intn(capacity*2+1))
			rng.Read(p)
			r.Write(p)
			m.all = append(m.all, p...)
			if rng.Intn(20) == 0 {
				keep := 1 + rng.Intn(capacity)
				r.Shrink(keep)
				m.cap = min(m.cap, keep)
				capacity = m.cap
			}
			seq := m.start() + int64(rng.Intn(capacity+10)) - 5
			check(t, r, m, seq, rng.Intn(capacity+10))
		}
	}
}

func TestGrowsOnlyAsNeeded(t *testing.T) {
	r := New(8 << 20)
	r.Write([]byte("hello"))
	if len(r.buf) > initialBytes {
		t.Fatalf("five bytes allocated %d", len(r.buf))
	}
}

func TestShrinkKeepsOffsets(t *testing.T) {
	r := New(100)
	for i := 0; i < 250; i++ {
		r.Write([]byte{byte(i)})
	}
	r.Shrink(10)
	if r.Head() != 250 || r.Start() != 240 {
		t.Fatalf("head %d start %d", r.Head(), r.Start())
	}
	data, from, gap := r.ReadFrom(0, 100)
	if from != 240 || !gap || len(data) != 10 || data[0] != 240 {
		t.Fatalf("after shrink: %v from %d gap %v", data, from, gap)
	}
	r.Write([]byte{1, 2})
	if r.Start() != 242 {
		t.Fatalf("the shrunk capacity was not kept: start %d", r.Start())
	}
}
