//go:build unix

package procstat

import (
	"os"
	"os/exec"
	"strconv"
	"syscall"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/procstat/proctest"
)

func TestTreeAndSampler(t *testing.T) {
	if !Supported {
		t.Skip("no process table here")
	}
	cmd := exec.Command("sh", "-c", "sleep 30 & sleep 30")
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = cmd.Process.Kill(); _ = cmd.Wait() }()
	deadline := time.Now().Add(5 * time.Second)
	for {
		if len(Tree(Table(), cmd.Process.Pid)) == 3 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("tree of sh has %d processes, want 3", len(Tree(Table(), cmd.Process.Pid)))
		}
		time.Sleep(10 * time.Millisecond)
	}
	self, ok := Read(os.Getpid())
	if !ok || self.RSSBytes <= 0 || self.Start == 0 {
		t.Fatalf("own process %+v", self)
	}
	s := NewSampler()
	first := s.Sample([]Term{{ID: "x", Pid: cmd.Process.Pid}}, time.Now())
	if len(first.Terminals) != 1 || first.Terminals[0].Processes != 3 || first.Terminals[0].RSSBytes <= 0 {
		t.Fatalf("sample %+v", first)
	}
	if first.Machine.MemTotalBytes <= 0 || first.Machine.MemAvailableBytes <= 0 || first.Machine.CPUs <= 0 {
		t.Fatalf("machine %+v", first.Machine)
	}
	second := s.Sample([]Term{{ID: "x", Pid: cmd.Process.Pid}}, time.Now().Add(time.Second))
	if second.Terminals[0].CPUPercent < 0 || second.Terminals[0].CPUPercent > 100 {
		t.Fatalf("an idle tree at %.1f %%", second.Terminals[0].CPUPercent)
	}
}

// A process that left both the tree and the session (setsid, then its parent exited) still counts
// against its terminal through the tag it inherited; and the daemon reports its own cost.
func TestEscapedProcessesAndTheDaemonAreCounted(t *testing.T) {
	if !Supported {
		t.Skip("no process table here")
	}
	proctest.Setsid(t)
	tag := "PROCSTAT_TEST_TAG=" + strconv.FormatInt(time.Now().UnixNano(), 10)
	cmd := exec.Command("sh", "-c", "setsid sh -c 'sleep 30 & exit' & sleep 30")
	cmd.Env = append(os.Environ(), tag)
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	var escaped []Proc
	defer func() {
		// Everything the test started carries the tag, the orphans included.
		for _, p := range TaggedAny(Table(), map[string]bool{tag: true}, nil)[tag] {
			_ = syscall.Kill(p.Pid, syscall.SIGKILL)
		}
		_ = cmd.Wait()
	}()
	deadline := time.Now().Add(5 * time.Second)
	for {
		table := Table()
		inTree := map[int]bool{}
		for _, p := range Tree(table, cmd.Process.Pid) {
			inTree[p.Pid] = true
		}
		escaped = nil
		for _, p := range TaggedAny(table, map[string]bool{tag: true}, inTree)[tag] {
			if p.Sid != cmd.Process.Pid && p.Sid != table[cmd.Process.Pid].Sid {
				escaped = append(escaped, p)
			}
		}
		if len(escaped) == 1 && len(inTree) == 2 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("tree %d processes, escaped %+v", len(inTree), escaped)
		}
		time.Sleep(20 * time.Millisecond)
	}
	s := NewSampler()
	untagged := s.Sample([]Term{{ID: "x", Pid: cmd.Process.Pid}}, time.Now())
	tagged := s.Sample([]Term{{ID: "x", Pid: cmd.Process.Pid, Tag: tag}}, time.Now().Add(time.Second))
	if untagged.Terminals[0].Processes != tagged.Terminals[0].Processes-1 {
		t.Fatalf("the escaped process was not counted: %d without the tag, %d with it",
			untagged.Terminals[0].Processes, tagged.Terminals[0].Processes)
	}
	if tagged.Daemon.Pid != os.Getpid() || tagged.Daemon.RSSBytes <= 0 {
		t.Fatalf("daemon %+v", tagged.Daemon)
	}
}
