//go:build linux

// On Linux everything is read from /proc.
package procstat

import (
	"bytes"
	"os"
	"strconv"
	"strings"
)

// clockTicks is USER_HZ, the unit of the CPU times in /proc. It is 100 on every Linux the daemon
// runs on; reading it properly needs sysconf, which needs cgo.
const clockTicks = 100

// Table reads every process. Processes that exit while it reads are skipped.
func Table() map[int]Proc {
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return nil
	}
	page := int64(os.Getpagesize())
	out := make(map[int]Proc, len(entries))
	for _, e := range entries {
		pid, err := strconv.Atoi(e.Name())
		if err != nil {
			continue
		}
		if p, ok := readStat(pid, page); ok {
			out[pid] = p
		}
	}
	return out
}

// Read returns one process, if it exists.
func Read(pid int) (Proc, bool) {
	return readStat(pid, int64(os.Getpagesize()))
}

func readStat(pid int, page int64) (Proc, bool) {
	data, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/stat")
	if err != nil {
		return Proc{}, false
	}
	// The command name is in parentheses and may itself contain spaces and parentheses; the fields
	// start after the last closing one.
	i := bytes.LastIndexByte(data, ')')
	if i < 0 {
		return Proc{}, false
	}
	f := strings.Fields(string(data[i+1:]))
	// f[0] is field 3 (state); field n is f[n-3].
	if len(f) < 22 {
		return Proc{}, false
	}
	num := func(n int) uint64 { v, _ := strconv.ParseUint(f[n-3], 10, 64); return v }
	inum := func(n int) int { v, _ := strconv.Atoi(f[n-3]); return v }
	return Proc{
		Pid:      pid,
		PPid:     inum(4),
		Pgrp:     inum(5),
		Sid:      inum(6),
		CPUTicks: num(14) + num(15),
		Start:    num(22),
		RSSBytes: int64(num(24)) * page,
	}, true
}

// environ is the environment a process was started with, as NUL-separated "NAME=value" entries.
// Only processes of the same user can be read, which are the only ones a terminal could have
// started.
func environ(pid int) ([]byte, bool) {
	data, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/environ")
	return data, err == nil
}

// cpuTimes reads the busy and total ticks of all CPUs.
func cpuTimes() (busy, total uint64) {
	data, err := os.ReadFile("/proc/stat")
	if err != nil {
		return 0, 0
	}
	line, _, _ := strings.Cut(string(data), "\n")
	f := strings.Fields(line)
	if len(f) < 5 || f[0] != "cpu" {
		return 0, 0
	}
	for i, s := range f[1:] {
		v, _ := strconv.ParseUint(s, 10, 64)
		// guest and guest_nice (fields 9 and 10) are already counted in user and nice.
		if i >= 8 {
			break
		}
		total += v
		if i != 3 && i != 4 { // idle and iowait
			busy += v
		}
	}
	return busy, total
}

func readMachine(m *Machine) {
	if data, err := os.ReadFile("/proc/meminfo"); err == nil {
		for _, line := range strings.Split(string(data), "\n") {
			f := strings.Fields(line)
			if len(f) < 2 {
				continue
			}
			v, _ := strconv.ParseInt(f[1], 10, 64)
			switch f[0] {
			case "MemTotal:":
				m.MemTotalBytes = v << 10
			case "MemAvailable:":
				m.MemAvailableBytes = v << 10
			}
		}
	}
	if data, err := os.ReadFile("/proc/loadavg"); err == nil {
		f := strings.Fields(string(data))
		if len(f) >= 3 {
			m.Load1, _ = strconv.ParseFloat(f[0], 64)
			m.Load5, _ = strconv.ParseFloat(f[1], 64)
			m.Load15, _ = strconv.ParseFloat(f[2], 64)
		}
	}
	// cgroup v2, as a container sees its own limits at the root of its cgroup namespace.
	if data, err := os.ReadFile("/sys/fs/cgroup/memory.max"); err == nil {
		if v, err := strconv.ParseInt(strings.TrimSpace(string(data)), 10, 64); err == nil && v < m.MemTotalBytes {
			m.CgroupLimitBytes = v
			if cur, err := os.ReadFile("/sys/fs/cgroup/memory.current"); err == nil {
				m.CgroupUsedBytes, _ = strconv.ParseInt(strings.TrimSpace(string(cur)), 10, 64)
			}
		}
	}
	if data, err := os.ReadFile("/sys/fs/cgroup/cpu.max"); err == nil {
		f := strings.Fields(string(data))
		if len(f) == 2 && f[0] != "max" {
			quota, _ := strconv.ParseFloat(f[0], 64)
			period, _ := strconv.ParseFloat(f[1], 64)
			if period > 0 {
				m.CgroupCPUs = quota / period
			}
		}
	}
}
