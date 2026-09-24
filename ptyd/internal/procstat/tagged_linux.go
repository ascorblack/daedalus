//go:build linux

package procstat

import (
	"bytes"
	"os"
	"strconv"
)

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
		env, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/environ")
		if err != nil {
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
