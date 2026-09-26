package browser

import (
	"os"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/chrome"
	"github.com/ascorblack/daedalus/browserd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/proto/procstat"
)

// BrowserStats is one browser's cost.
type BrowserStats struct {
	ID         string  `json:"id"`
	Pid        int     `json:"pid"`
	Processes  int     `json:"processes"`
	RSSBytes   int64   `json:"rss_bytes"`
	CPUPercent float64 `json:"cpu_percent"`
	Tabs       int     `json:"tabs"`
}

// Stats is browser.stats's result.
type Stats struct {
	At        time.Time            `json:"at"`
	Supported bool                 `json:"supported"`
	Browsers  []BrowserStats       `json:"browsers"`
	Daemon    procstat.DaemonStats `json:"daemon"`
	Machine   procstat.Machine     `json:"machine"`
}

// Sample measures every browser. Memory is private memory (chrome.PrivateBytes) where it can be
// read, and the resident size elsewhere; the process tree is the browser's session, which holds every
// helper Chromium starts.
func (m *Manager) Sample(s *procstat.Sampler, now time.Time) Stats {
	bs := m.Browsers()
	terms := make([]procstat.Term, len(bs))
	for i, b := range bs {
		terms[i] = procstat.Term{ID: b.ID, Pid: b.Pid()}
	}
	sample := s.Sample(terms, now)
	out := Stats{At: sample.At, Supported: sample.Supported, Daemon: sample.Daemon, Machine: sample.Machine,
		Browsers: make([]BrowserStats, 0, len(bs))}
	var table map[int]procstat.Proc
	if procstat.Supported {
		table = procstat.Table()
	}
	for i, b := range bs {
		ts := sample.Terminals[i]
		st := BrowserStats{ID: b.ID, Pid: ts.Pid, Processes: ts.Processes, RSSBytes: ts.RSSBytes, CPUPercent: ts.CPUPercent}
		if table != nil {
			var private int64
			ok := true
			for _, p := range procstat.Tree(table, b.Pid()) {
				v := chrome.PrivateBytes(p.Pid)
				if v < 0 {
					ok = false
					break
				}
				private += v
			}
			if ok {
				st.RSSBytes = private
			}
		}
		for _, g := range b.groupList() {
			st.Tabs += len(g.Tabs())
		}
		out.Browsers = append(out.Browsers, st)
	}
	if self, ok := table[os.Getpid()]; ok {
		if v := chrome.PrivateBytes(self.Pid); v >= 0 {
			out.Daemon.RSSBytes = v
		}
	}
	return out
}

// RunStats publishes browser.stats every interval while a browser runs, ends a browser past the hard
// memory limit, and asks the first renderer whether it runs sandboxed.
func (m *Manager) RunStats(stop <-chan struct{}) {
	s := procstat.NewSampler()
	tick := time.NewTicker(config.StatsInterval)
	defer tick.Stop()
	for {
		select {
		case <-stop:
			return
		case <-tick.C:
		}
		bs := m.Browsers()
		if len(bs) == 0 {
			continue
		}
		st := m.Sample(s, time.Now())
		m.publish("browser.stats", st)
		for _, b := range bs {
			m.checkSandbox(b)
		}
		for _, x := range st.Browsers {
			if m.lim.MemoryHardBytes > 0 && x.RSSBytes > m.lim.MemoryHardBytes {
				for _, b := range bs {
					if b.ID == x.ID {
						m.log.Warn("browser over its memory limit", "browser", b.ID, "bytes", x.RSSBytes)
						b.setClosing("memory")
						go b.proc.Kill()
					}
				}
			}
		}
	}
}

// checkSandbox records whether a renderer of b runs under the seccomp filter, once there is one.
func (m *Manager) checkSandbox(b *Browser) {
	m.mu.Lock()
	known := m.sandbox != "unknown"
	m.mu.Unlock()
	if known || !procstat.Supported {
		return
	}
	var pids []int
	for _, p := range procstat.Tree(procstat.Table(), b.Pid()) {
		pids = append(pids, p.Pid)
	}
	sandboxed, ok := chrome.RendererSandboxed(pids)
	if !ok {
		return
	}
	m.mu.Lock()
	if sandboxed {
		m.sandbox = "ok"
	} else {
		m.sandbox = "a renderer runs without its seccomp filter"
	}
	m.mu.Unlock()
}
