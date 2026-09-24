//go:build unix && !linux

package ptyproc

// ProgramGroup is the program's own process group. Nothing is wrapped where there is no
// bubblewrap.
func (p *Proc) ProgramGroup() int { return p.Pid }
