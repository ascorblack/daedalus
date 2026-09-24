//go:build !linux

package procstat

// Supported is false: outside Linux there is no /proc to read. The daemon then ends a terminal by
// its process group alone and reports no process statistics.
const Supported = false

const clockTicks = 100

func Table() map[int]Proc                { return nil }
func Read(int) (Proc, bool)              { return Proc{}, false }
func Tree(map[int]Proc, int) []Proc      { return nil }
func Session(map[int]Proc, int) []Proc   { return nil }
func Tagged(map[int]Proc, string) []Proc { return nil }
func cpuTimes() (uint64, uint64)         { return 0, 0 }
func readMachine(*Machine)               {}
