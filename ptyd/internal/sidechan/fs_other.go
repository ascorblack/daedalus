//go:build !unix

package sidechan

import "os"

func openNoFollow(p string, dir bool) (*os.File, error) { return os.Open(p) }

func openedPath(f *os.File) (string, bool) { return "", false }

func fileID(st os.FileInfo) string { return "" }
