//go:build !unix

package sidechan

import "os"

func openNoFollow(p string, dir bool) (*os.File, error) { return os.Open(p) }

func openedPath(f *os.File) (string, bool) { return "", false }

func fileID(st os.FileInfo) string { return "" }

// writable is unknown here, and left out of the answer rather than guessed.
func writable(p string) *bool { return nil }
