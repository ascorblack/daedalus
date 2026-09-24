package procstat

import (
	"os"
	"os/exec"
	"testing"
	"time"
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
