//go:build unix && !linux && !darwin

package ptyproc

// member is unused where the process table cannot be read: there, a terminal is ended through its
// process group alone, and a process that left the group survives.
type member struct{}

func (p *Proc) members(bool) []member { return nil }

func (member) kill() {}
