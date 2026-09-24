//go:build !windows

package server

// RestrictDir is a no-op where the file mode already says who may read: the run and state
// directories are created 0700 and their files 0600.
func RestrictDir(string) error { return nil }

func restrictFile(string) error { return nil }
