//go:build linux

package ptyproc

import (
	"os"
	"strconv"
	"strings"

	"github.com/ascorblack/daedalus/ptyd/internal/procstat"
)

// ProgramGroup is the process group the terminal's own program leads: the group an idle shell
// holds the foreground with, so that any other foreground group means a command is running.
//
// Unwrapped, that is the program's pid. Wrapped in bubblewrap, the PTY's process is bubblewrap,
// and the program runs two processes below it (below the init of its process namespace); an
// interactive shell has by then made a group of its own. 0 until the program is found: in the
// moment after the start it may not have been executed yet.
func (p *Proc) ProgramGroup() int {
	if !p.wrapped {
		return p.Pid
	}
	pid := int(p.program.Load())
	if pid == 0 {
		if pid = findWrapped(p.Pid); pid == 0 {
			return 0
		}
		p.program.Store(int64(pid))
	}
	pr, ok := procstat.Read(pid)
	if !ok {
		return 0
	}
	return pr.Pgrp
}

// findWrapped follows bubblewrap's processes down to the first one that is something else. Each of
// them has one child; a process still called bwrap with none has not executed the program yet.
func findWrapped(root int) int {
	pid := root
	for depth := 0; depth < 4; depth++ {
		if comm(pid) != "bwrap" {
			if pid == root {
				return 0
			}
			return pid
		}
		child := onlyChild(pid)
		if child == 0 {
			return 0
		}
		pid = child
	}
	return 0
}

func comm(pid int) string {
	data, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/comm")
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(data))
}

// onlyChild reads the kernel's list of a process's children, and falls back to the process table
// on a kernel built without it.
func onlyChild(pid int) int {
	data, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/task/" + strconv.Itoa(pid) + "/children")
	if err == nil {
		fields := strings.Fields(string(data))
		if len(fields) == 0 {
			return 0
		}
		child, _ := strconv.Atoi(fields[0])
		return child
	}
	for _, pr := range procstat.Table() {
		if pr.PPid == pid {
			return pr.Pid
		}
	}
	return 0
}
