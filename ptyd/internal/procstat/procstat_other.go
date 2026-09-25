//go:build !linux && !darwin

package procstat

// Supported is false: no process table is read here. On Windows a terminal's processes are held
// in a job object instead, which ends them all without being asked who they are, and no process
// statistics are reported.
const Supported = false

const clockTicks = 100

func Table() map[int]Proc                { return nil }
func Read(int) (Proc, bool)              { return Proc{}, false }
func Tree(map[int]Proc, int) []Proc      { return nil }
func Session(map[int]Proc, int) []Proc   { return nil }
func Tagged(map[int]Proc, string) []Proc { return nil }
func cpuTimes() (uint64, uint64)         { return 0, 0 }
func readMachine(*Machine)               {}

func TaggedAny(map[int]Proc, map[string]bool, map[int]bool) map[string][]Proc { return nil }
