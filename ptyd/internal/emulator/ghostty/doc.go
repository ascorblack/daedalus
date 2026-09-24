// Package ghostty is the daemon's screen emulator: libghostty-vt, Ghostty's terminal core, linked
// statically through its Go bindings, with the daemon's own snapshot layer on top of its formatter.
//
// It needs cgo and the library built by ptyd/libghostty/build.sh. A build without cgo has no screen
// emulator (see the production package), which is why this file carries no build constraint: the
// package exists, empty, in such a build.
//
// Why Ghostty: of the emulators measured it was the only one that survived every hostile stream
// thrown at it unclamped (at most 92 MB, never a hang), its snapshots replayed into xterm.js came
// closest to the live screen, and it parses at 75–186 MB/s in about 15 MB per terminal.
package ghostty
