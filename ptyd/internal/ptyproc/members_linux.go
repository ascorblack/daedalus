//go:build linux

package ptyproc

import (
	"os"
	"syscall"

	"github.com/ascorblack/daedalus/ptyd/internal/procstat"
)

// member is a process found to belong to a terminal, remembered with its start time so that a pid
// reused in the meantime is never signalled.
type member struct {
	pid   int
	start uint64
}

// members finds the terminal's processes three ways, because each misses something: the process
// tree loses a child whose parent exited (it is reparented), the session loses one that called
// setsid, and the tag loses one that cleared its environment. Together they leave only a process
// that did all three.
//
// withTree is false once the program may have been reaped: a pid that has been freed can be reused,
// and a tree rooted at it would be someone else's. A session id cannot be reused while any process
// still carries it, so the session stays safe to follow.
func (p *Proc) members(withTree bool) []member {
	table := procstat.Table()
	groups := [][]procstat.Proc{procstat.Session(table, p.Pid)}
	if withTree {
		groups = append(groups, procstat.Tree(table, p.Pid))
	}
	if p.tag != "" {
		groups = append(groups, procstat.Tagged(table, p.tag))
	}
	self := os.Getpid()
	seen := map[int]bool{}
	var out []member
	for _, g := range groups {
		for _, pr := range g {
			if pr.Pid == self || pr.Pid <= 1 || seen[pr.Pid] {
				continue
			}
			seen[pr.Pid] = true
			out = append(out, member{pr.Pid, pr.Start})
		}
	}
	return out
}

func (m member) kill() {
	if now, ok := procstat.Read(m.pid); ok && now.Start == m.start {
		_ = syscall.Kill(m.pid, syscall.SIGKILL)
	}
}
