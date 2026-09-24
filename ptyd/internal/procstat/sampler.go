package procstat

import (
	"os"
	"runtime"
	"sync"
	"time"
)

// Term names a terminal whose process tree is to be measured.
type Term struct {
	ID  string
	Pid int
	// Tag is the "NAME=value" every process the terminal started inherits, which finds a process
	// that left both the tree and the session (a daemon that forked twice and called setsid).
	Tag string
}

// TermStats is what one terminal's processes cost.
type TermStats struct {
	ID         string  `json:"id"`
	Pid        int     `json:"pid"`
	Processes  int     `json:"processes"`
	RSSBytes   int64   `json:"rss_bytes"`
	CPUPercent float64 `json:"cpu_percent"` // of one CPU, as top shows it, since the previous sample
	// DaemonBytes is what the terminal costs inside the daemon that no process of its own shows:
	// the output ring and the queues of its clients. The daemon fills it in.
	DaemonBytes int64 `json:"daemon_bytes"`
}

// DaemonStats is the daemon's own process: its memory holds every terminal's emulator and ring.
type DaemonStats struct {
	Pid        int     `json:"pid"`
	RSSBytes   int64   `json:"rss_bytes"`
	CPUPercent float64 `json:"cpu_percent"`
}

// Sample is one measurement of every terminal and of the machine.
type Sample struct {
	At        time.Time   `json:"at"`
	Supported bool        `json:"supported"`
	Terminals []TermStats `json:"terminals"`
	Daemon    DaemonStats `json:"daemon"`
	Machine   Machine     `json:"machine"`
}

type procKey struct {
	pid   int
	start uint64
}

// Sampler turns successive readings of the process table into CPU percentages. The CPU time of a
// process is a running total, so a percentage needs the previous reading of the same process: the
// same pid with the same start time, so that a reused pid does not inherit someone else's total.
type Sampler struct {
	mu        sync.Mutex
	prev      map[procKey]uint64
	prevAt    time.Time
	prevBusy  uint64
	prevTotal uint64
}

// NewSampler returns a sampler with no history; its first sample reports 0 % CPU.
func NewSampler() *Sampler { return &Sampler{} }

// Sample measures the given terminals and the daemon. RSS is summed over each terminal's process
// tree, its session and the processes carrying its tag, which counts shared pages more than once: it
// is an estimate of what the terminal costs, not an accounting of the machine.
func (s *Sampler) Sample(terms []Term, now time.Time) Sample {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := Sample{At: now, Supported: Supported, Terminals: make([]TermStats, 0, len(terms))}
	out.Machine.CPUs = runtime.NumCPU()
	if !Supported {
		for _, t := range terms {
			out.Terminals = append(out.Terminals, TermStats{ID: t.ID, Pid: t.Pid})
		}
		return out
	}
	readMachine(&out.Machine)
	busy, total := cpuTimes()
	if s.prevTotal > 0 && total > s.prevTotal {
		out.Machine.CPUPercent = round1(100 * float64(busy-s.prevBusy) / float64(total-s.prevTotal))
	}
	elapsed := now.Sub(s.prevAt).Seconds()
	table := Table()
	next := make(map[procKey]uint64, 64)
	// ticks is the CPU time p spent since the previous sample, and remembers it for the next.
	ticks := func(p Proc) uint64 {
		k := procKey{p.Pid, p.Start}
		next[k] = p.CPUTicks
		if before, ok := s.prev[k]; ok && p.CPUTicks >= before {
			return p.CPUTicks - before
		} else if s.prev != nil {
			// Started since the previous sample: all of its time was spent in the interval.
			return p.CPUTicks
		}
		return 0
	}
	percent := func(t uint64) float64 {
		if s.prev == nil || elapsed <= 0 {
			return 0
		}
		return round1(100 * float64(t) / clockTicks / elapsed)
	}
	type acc struct {
		seen  map[int]bool
		ticks uint64
	}
	accs := make([]acc, len(terms))
	counted := map[int]bool{}
	add := func(i int, p Proc) {
		a := &accs[i]
		if a.seen[p.Pid] {
			return
		}
		a.seen[p.Pid] = true
		counted[p.Pid] = true
		ts := &out.Terminals[i]
		ts.Processes++
		ts.RSSBytes += p.RSSBytes
		a.ticks += ticks(p)
	}
	tags := map[string]bool{}
	for i, t := range terms {
		out.Terminals = append(out.Terminals, TermStats{ID: t.ID, Pid: t.Pid})
		accs[i].seen = map[int]bool{}
		for _, group := range [][]Proc{Tree(table, t.Pid), Session(table, t.Pid)} {
			for _, p := range group {
				add(i, p)
			}
		}
		if t.Tag != "" {
			tags[t.Tag] = true
		}
	}
	tagged := TaggedAny(table, tags, counted)
	for i, t := range terms {
		for _, p := range tagged[t.Tag] {
			add(i, p)
		}
		out.Terminals[i].CPUPercent = percent(accs[i].ticks)
	}
	if self, ok := table[os.Getpid()]; ok {
		out.Daemon = DaemonStats{Pid: self.Pid, RSSBytes: self.RSSBytes, CPUPercent: percent(ticks(self))}
	}
	s.prev, s.prevAt, s.prevBusy, s.prevTotal = next, now, busy, total
	return out
}

func round1(v float64) float64 { return float64(int64(v*10+0.5)) / 10 }
