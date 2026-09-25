//go:build darwin

package procstat

import (
	"bytes"
	"encoding/binary"
	"os"
	"syscall"
	"unsafe"

	"golang.org/x/sys/unix"
)

// On macOS there is no /proc. The process table comes from the kern.proc sysctl, a process's
// memory and CPU time from proc_info (the call behind libproc's proc_pidinfo, made directly so that
// a build without cgo reads the same), its environment from kern.procargs2, and the machine from
// the hw and vm sysctls. None of it needs a privilege for the user's own processes, which are the
// only ones a terminal could have started.

// clockTicks is the unit CPUTicks are kept in, the same as on Linux: the kernel's own unit is
// converted to it (see cpuTicks), so the sampler's arithmetic is the same on both.
const clockTicks = 100

// Table reads every process. Processes that exit while it reads are skipped.
func Table() map[int]Proc {
	kps, err := unix.SysctlKinfoProcSlice("kern.proc.all")
	if err != nil {
		return nil
	}
	out := make(map[int]Proc, len(kps))
	for i := range kps {
		if p, ok := fromKinfo(&kps[i]); ok {
			out[p.Pid] = p
		}
	}
	return out
}

// Read returns one process, if it exists.
func Read(pid int) (Proc, bool) {
	kp, err := unix.SysctlKinfoProc("kern.proc.pid", pid)
	if err != nil || int(kp.Proc.P_pid) != pid {
		return Proc{}, false
	}
	return fromKinfo(kp)
}

func fromKinfo(kp *unix.KinfoProc) (Proc, bool) {
	pid := int(kp.Proc.P_pid)
	if pid <= 0 {
		return Proc{}, false // the kernel itself
	}
	// kinfo_proc has the session only as a kernel address; the id is asked for separately.
	sid, err := unix.Getsid(pid)
	if err != nil {
		return Proc{}, false
	}
	st := kp.Proc.P_starttime
	p := Proc{
		Pid:  pid,
		PPid: int(kp.Eproc.Ppid),
		Pgrp: int(kp.Eproc.Pgid),
		Sid:  sid,
		// Microseconds since the epoch: only ever compared, with the pid, to tell a process from a
		// later one that was given the same pid.
		Start: uint64(st.Sec)*1_000_000 + uint64(st.Usec),
	}
	if ti, ok := taskInfo(pid); ok {
		p.RSSBytes = int64(ti.residentSize)
		p.CPUTicks = cpuTicks(ti.totalUser + ti.totalSystem)
	}
	return p, true
}

// procTaskInfo is struct proc_taskinfo from <sys/proc_info.h>, the part of it read here and the
// rest as padding to its full size, which the call checks.
type procTaskInfo struct {
	virtualSize  uint64
	residentSize uint64
	totalUser    uint64 // in Mach absolute time units, not nanoseconds (see cpuTicks)
	totalSystem  uint64
	_            [2]uint64
	_            [12]int32
}

const (
	procInfoCallPidinfo = 2 // PROC_INFO_CALL_PIDINFO
	procPidTaskInfo     = 4 // PROC_PIDTASKINFO
)

func taskInfo(pid int) (procTaskInfo, bool) {
	var ti procTaskInfo
	n, _, errno := syscall.Syscall6(unix.SYS_PROC_INFO, procInfoCallPidinfo, uintptr(pid), procPidTaskInfo, 0,
		uintptr(unsafe.Pointer(&ti)), unsafe.Sizeof(ti))
	if errno != 0 || n != unsafe.Sizeof(ti) {
		return procTaskInfo{}, false
	}
	return ti, true
}

// cpuTicks converts a CPU time in Mach absolute time units to clockTicks. The unit is a nanosecond
// on Intel and a tick of the 24 MHz timebase on Apple silicon; hw.tbfrequency is that timebase's
// rate, so the conversion holds on both. Without it the unit is taken to be the nanosecond.
func cpuTicks(abs uint64) uint64 {
	if timebaseHz == 0 {
		return abs / (1_000_000_000 / clockTicks)
	}
	return uint64(float64(abs) * clockTicks / float64(timebaseHz))
}

var timebaseHz = sysctlNumber("hw.tbfrequency")

// sysctlNumber reads an integer sysctl of either width, or 0.
func sysctlNumber(name string) uint64 {
	b, err := unix.SysctlRaw(name)
	if err != nil {
		return 0
	}
	switch len(b) {
	case 4:
		return uint64(binary.NativeEndian.Uint32(b))
	case 8:
		return binary.NativeEndian.Uint64(b)
	}
	return 0
}

// environ is the environment a process was started with, as NUL-separated "NAME=value" entries.
// kern.procargs2 holds the argument count, the executable's path, the arguments and then the
// environment, each NUL-terminated (with extra NULs after the path), and the environment ends at an
// empty string.
func environ(pid int) ([]byte, bool) {
	b, err := unix.SysctlRaw("kern.procargs2", pid)
	if err != nil || len(b) < 4 {
		return nil, false
	}
	argc := int(binary.NativeEndian.Uint32(b))
	rest := b[4:]
	i := bytes.IndexByte(rest, 0) // the executable's path
	if i < 0 {
		return nil, false
	}
	rest = rest[i:]
	for len(rest) > 0 && rest[0] == 0 {
		rest = rest[1:]
	}
	for ; argc > 0; argc-- {
		i := bytes.IndexByte(rest, 0)
		if i < 0 {
			return nil, false
		}
		rest = rest[i+1:]
	}
	// What follows the empty string is the kernel's own (apple_* strings), not the environment.
	env := rest
	for n := 0; n < len(rest); {
		i := bytes.IndexByte(rest[n:], 0)
		if i <= 0 {
			env = rest[:n]
			break
		}
		n += i + 1
	}
	return env, true
}

// cpuTimes would be the machine's busy and total CPU time. macOS keeps those only behind the Mach
// host interface, which is out of reach without cgo, so the machine's CPU percentage stays 0 there;
// each terminal's own CPU percentage is measured.
func cpuTimes() (uint64, uint64) { return 0, 0 }

func readMachine(m *Machine) {
	// Available is what the kernel could hand out without swapping: the free pages, the speculative
	// ones (read ahead, not yet used) and the file cache it can drop. Without the free count there
	// is no estimate, and the total is left out too: a total with nothing available would read as a
	// machine out of memory.
	free := sysctlNumber("vm.page_free_count")
	if total := sysctlNumber("hw.memsize"); total > 0 && free > 0 {
		pages := free + sysctlNumber("vm.page_speculative_count") + sysctlNumber("vm.page_pageable_external_count")
		m.MemTotalBytes = int64(total)
		m.MemAvailableBytes = min(int64(pages)*int64(os.Getpagesize()), m.MemTotalBytes)
	}
	// struct loadavg: three fixed-point averages and the scale they are in.
	if b, err := unix.SysctlRaw("vm.loadavg"); err == nil && len(b) >= 24 {
		scale := float64(binary.NativeEndian.Uint64(b[16:24]))
		if scale > 0 {
			m.Load1 = float64(binary.NativeEndian.Uint32(b[0:4])) / scale
			m.Load5 = float64(binary.NativeEndian.Uint32(b[4:8])) / scale
			m.Load15 = float64(binary.NativeEndian.Uint32(b[8:12])) / scale
		}
	}
}
