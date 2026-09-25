//go:build linux || darwin

package procstat

import (
	"bytes"
	"os"
)

// Supported is true where the process table can be read.
const Supported = true

// Tree returns root and every process descending from it, found through parent links.
func Tree(table map[int]Proc, root int) []Proc {
	children := make(map[int][]int, len(table))
	for pid, p := range table {
		children[p.PPid] = append(children[p.PPid], pid)
	}
	var out []Proc
	seen := map[int]bool{}
	stack := []int{root}
	for len(stack) > 0 {
		pid := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		if seen[pid] {
			continue
		}
		seen[pid] = true
		if p, ok := table[pid]; ok {
			out = append(out, p)
		}
		stack = append(stack, children[pid]...)
	}
	return out
}

// Session returns every process whose session id is sid.
func Session(table map[int]Proc, sid int) []Proc {
	var out []Proc
	for _, p := range table {
		if p.Sid == sid {
			out = append(out, p)
		}
	}
	return out
}

// Tagged returns the processes whose environment holds tag ("NAME=value") exactly. Only processes
// of the same user can be read, which are the only ones a terminal could have started.
func Tagged(table map[int]Proc, tag string) []Proc {
	want := []byte(tag)
	var out []Proc
	for pid, p := range table {
		env, ok := environ(pid)
		if !ok {
			continue
		}
		for _, kv := range bytes.Split(env, []byte{0}) {
			if bytes.Equal(kv, want) {
				out = append(out, p)
				break
			}
		}
	}
	return out
}

// TaggedAny finds, in one pass over the process table, the processes whose environment holds any of
// tags ("NAME=value") exactly, grouped by tag. Processes in skip are not read: they are already
// accounted for, and reading every environment is the expensive part of a sample.
func TaggedAny(table map[int]Proc, tags map[string]bool, skip map[int]bool) map[string][]Proc {
	out := map[string][]Proc{}
	if len(tags) == 0 {
		return out
	}
	self := os.Getpid()
	for pid, p := range table {
		if skip[pid] || pid == self || pid <= 1 {
			continue
		}
		env, ok := environ(pid)
		if !ok {
			continue
		}
		for _, kv := range bytes.Split(env, []byte{0}) {
			if tags[string(kv)] {
				out[string(kv)] = append(out[string(kv)], p)
				break
			}
		}
	}
	return out
}
