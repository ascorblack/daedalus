// Package procstat reads the process table and the machine's memory and CPU: from /proc on Linux,
// from the kernel's sysctl tables and proc_info on macOS. The daemon uses it to find every process
// a terminal started (to end them, and to count what they cost) and to report how much of the
// machine is left.
package procstat

// Proc is one line of the process table.
type Proc struct {
	Pid, PPid, Pgrp, Sid int
	CPUTicks             uint64 // user + system time of the process itself
	RSSBytes             int64
	Start                uint64 // start time in ticks since boot: with Pid, a process's identity
}

// Machine is the memory and CPU of the environment the daemon runs in.
type Machine struct {
	MemTotalBytes     int64   `json:"mem_total_bytes"`
	MemAvailableBytes int64   `json:"mem_available_bytes"`
	CgroupLimitBytes  int64   `json:"cgroup_limit_bytes,omitempty"` // a container's memory limit, when it has one
	CgroupUsedBytes   int64   `json:"cgroup_used_bytes,omitempty"`
	CPUs              int     `json:"cpus"`
	CgroupCPUs        float64 `json:"cgroup_cpus,omitempty"` // a container's CPU quota, in CPUs
	CPUPercent        float64 `json:"cpu_percent"`           // of the whole machine, since the previous sample
	Load1             float64 `json:"load1"`
	Load5             float64 `json:"load5"`
	Load15            float64 `json:"load15"`
}
