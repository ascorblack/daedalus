//go:build windows

package main

// isSetuidRoot is never true on Windows, which has no setuid; Chromium sandboxes itself there
// without a helper.
func isSetuidRoot(string) bool { return false }
