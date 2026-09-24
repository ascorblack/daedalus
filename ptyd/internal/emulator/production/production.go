// Package production chooses the emulator the daemon runs with. It is its own package because the
// choice depends on the build: the screen emulator needs cgo, and a build without cgo (a
// cross-compile for a platform whose library is not built yet) still has to produce a daemon.
package production
