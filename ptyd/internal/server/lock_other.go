//go:build !unix

package server

// lockDir is a no-op where flock does not exist; the socket check is the only guard there.
func lockDir(string) (func(), error) { return func() {}, nil }
