//go:build linux

package chrome

import (
	"os"
	"strconv"
	"strings"
)

// PrivateBytes is what a process costs on its own: its anonymous and shared-memory pages, from
// /proc/<pid>/status. The resident size counts Chromium's code once per process, and summed over a
// browser's dozen processes it read four to five times what the browser's cgroup held; a sandboxed
// renderer's smaps (and so its proportional size) cannot be read at all.
func PrivateBytes(pid int) int64 {
	data, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/status")
	if err != nil {
		return 0
	}
	var total int64
	for _, line := range strings.Split(string(data), "\n") {
		if !strings.HasPrefix(line, "RssAnon:") && !strings.HasPrefix(line, "RssShmem:") {
			continue
		}
		f := strings.Fields(line)
		if len(f) >= 2 {
			kb, _ := strconv.ParseInt(f[1], 10, 64)
			total += kb << 10
		}
	}
	return total
}

// RendererSandboxed says whether a renderer among pids runs under Chromium's seccomp filter:
// sandboxed is the answer, known is false when no renderer was found to ask.
func RendererSandboxed(pids []int) (sandboxed, known bool) {
	for _, pid := range pids {
		cmd, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/cmdline")
		// Chromium rewrites its helpers' command lines into one space-separated string.
		if err != nil || !strings.Contains(string(cmd), "--type=renderer") {
			continue
		}
		status, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/status")
		if err != nil {
			continue
		}
		for _, line := range strings.Split(string(status), "\n") {
			if strings.HasPrefix(line, "Seccomp:") {
				return strings.TrimSpace(strings.TrimPrefix(line, "Seccomp:")) == "2", true
			}
		}
	}
	return false, false
}
