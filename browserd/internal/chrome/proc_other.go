//go:build !linux

package chrome

// PrivateBytes is not measured outside Linux; the caller falls back to the resident size.
func PrivateBytes(pid int) int64 { return -1 }

// RendererSandboxed cannot be asked outside Linux; Chromium's own start-up refusal is the signal.
func RendererSandboxed(pids []int) (sandboxed, known bool) { return false, false }
