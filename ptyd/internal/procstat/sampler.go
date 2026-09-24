package procstat

import (
	"runtime"
	"sync"
	"time"
)

// Term names a terminal whose process tree is to be measured.
type Term struct {
	ID  string
	Pid int
}

// TermStats is what one terminal's processes cost.
type TermStats struct {
	ID         string  `json:"id"`
	Pid        int     `json:"pid"`
	Processes  int     `json:"processes"`
	RSSBytes   int64   `json:"rss_bytes"`
	CPUPercent float64 `json:"cpu_percent"` // of one CPU, as top shows it, since the previous sample
}

// Sample is one measurement of every terminal and of the machine.
type Sample struct {
	At        time.Time   `json:"at"`
	Supported bool        `json:"supported"`
	Terminals []TermStats `json:"terminals"`
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

// Sample measures the given terminals. RSS is summed over each terminal's process tree and session,
// which counts shared pages more than once: it is an estimate of what the terminal costs, not an
// accounting of the machine.
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
	for _, t := range terms {
		ts := TermStats{ID: t.ID, Pid: t.Pid}
		seen := map[int]bool{}
		var ticks uint64
		for _, group := range [][]Proc{Tree(table, t.Pid), Session(table, t.Pid)} {
			for _, p := range group {
				if seen[p.Pid] {
					continue
				}
				seen[p.Pid] = true
				ts.Processes++
				ts.RSSBytes += p.RSSBytes
				k := procKey{p.Pid, p.Start}
				next[k] = p.CPUTicks
				if before, ok := s.prev[k]; ok && p.CPUTicks >= before {
					ticks += p.CPUTicks - before
				} else if s.prev != nil {
					// Started since the previous sample: all of its time was spent in the interval.
					ticks += p.CPUTicks
				}
			}
		}
		if s.prev != nil && elapsed > 0 {
			ts.CPUPercent = round1(100 * float64(ticks) / clockTicks / elapsed)
		}
		out.Terminals = append(out.Terminals, ts)
	}
	s.prev, s.prevAt, s.prevBusy, s.prevTotal = next, now, busy, total
	return out
}

func round1(v float64) float64 { return float64(int64(v*10+0.5)) / 10 }
