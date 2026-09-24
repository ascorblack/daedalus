package sandbox

import (
	"bytes"
	"context"
	"os/exec"
	"runtime"
	"strings"
	"sync"
	"time"
)

// OK is the status of a sandbox that works.
const OK = "ok"

// RetryAfter is how long a failed probe is believed. A working sandbox is believed for good; a
// machine given namespaces later is noticed without restarting the daemon, which would end every
// terminal it runs.
const RetryAfter = 5 * time.Minute

// probeTimeout bounds one probe: bubblewrap either creates its namespaces at once or not at all.
const probeTimeout = 20 * time.Second

// Runner runs the probe command and returns its combined output and whether it succeeded. Tests
// replace it.
type Runner func(ctx context.Context, argv []string) (string, bool)

// Prober answers whether bubblewrap can create its namespaces here, and caches the answer.
type Prober struct {
	bwrap string // resolved, or "" when it is not installed
	goos  string
	run   Runner
	now   func() time.Time

	mu      sync.Mutex
	state   string
	at      time.Time
	probing chan struct{} // closed when the probe under way ends; nil when none is
}

// NewProber returns a prober for the bubblewrap at path ("" when it was not found).
func NewProber(path string) *Prober {
	return &Prober{bwrap: path, goos: runtime.GOOS, run: runCommand, now: time.Now}
}

// NewTestProber is NewProber with the operating system, the command runner and the clock replaced.
func NewTestProber(path, goos string, run Runner, now func() time.Time) *Prober {
	return &Prober{bwrap: path, goos: goos, run: run, now: now}
}

// Bwrap is the bubblewrap executable.
func (p *Prober) Bwrap() string { return p.bwrap }

// Status probes when the cached answer is missing or stale, waits for the probe, and returns
// "ok" or why the sandbox is not available.
func (p *Prober) Status(ctx context.Context) string {
	for {
		p.mu.Lock()
		if p.fresh() {
			s := p.state
			p.mu.Unlock()
			return s
		}
		wait := p.probing
		if wait == nil {
			wait = make(chan struct{})
			p.probing = wait
			p.mu.Unlock()
			p.probe(wait)
			continue
		}
		p.mu.Unlock()
		select {
		case <-wait:
		case <-ctx.Done():
			return "the sandbox check did not finish: " + ctx.Err().Error()
		}
	}
}

// Peek returns the cached answer without waiting, and starts a probe in the background when it is
// missing or stale. Before the first probe ends it says so.
func (p *Prober) Peek() string {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.fresh() {
		return p.state
	}
	if p.probing == nil {
		wait := make(chan struct{})
		p.probing = wait
		go p.probe(wait)
	}
	if p.state == "" {
		return "not checked yet"
	}
	return p.state
}

// fresh is called with mu held.
func (p *Prober) fresh() bool {
	return p.state == OK || (p.state != "" && p.now().Sub(p.at) < RetryAfter)
}

func (p *Prober) probe(done chan struct{}) {
	state := p.check()
	p.mu.Lock()
	p.state, p.at, p.probing = state, p.now(), nil
	p.mu.Unlock()
	close(done)
}

// platformName is how the operator knows the system: the reason is shown in the app as it is.
func platformName(goos string) string {
	switch goos {
	case "windows":
		return "Windows"
	case "darwin":
		return "macOS"
	}
	return goos
}

func (p *Prober) check() string {
	if p.goos != "linux" {
		// bubblewrap is a Linux facility built on user namespaces; elsewhere there is nothing to probe,
		// and probing would report missing software rather than the platform it is.
		return "not available on " + platformName(p.goos)
	}
	if p.bwrap == "" {
		return "bwrap is not installed"
	}
	ctx, cancel := context.WithTimeout(context.Background(), probeTimeout)
	defer cancel()
	// The flags of the agent's own Exec probe, so the two answer the same question the same way.
	out, ok := p.run(ctx, []string{p.bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--unshare-pid", "true"})
	if ok {
		return OK
	}
	out = strings.TrimSpace(out)
	if len(out) > 160 {
		out = out[:160]
	}
	if out == "" {
		out = "no message"
	}
	return "bwrap cannot create namespaces here: " + out + " (a container needs cap_add SYS_ADMIN with unconfined seccomp and AppArmor; a machine needs unprivileged user namespaces allowed)"
}

func runCommand(ctx context.Context, argv []string) (string, bool) {
	var out bytes.Buffer
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.Stdout, cmd.Stderr = &out, &out
	err := cmd.Run()
	return out.String(), err == nil
}
