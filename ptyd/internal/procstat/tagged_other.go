//go:build !linux

package procstat

func TaggedAny(map[int]Proc, map[string]bool, map[int]bool) map[string][]Proc { return nil }
