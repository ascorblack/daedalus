//go:build !nowebview && (windows || (darwin && !cgo))

package main

// Everywhere but a real macOS build there is no menu bar to install.
//
// On Windows the shortcuts this exists for work already: WebView2 hosts the same edit commands
// Edge does and handles Ctrl+C, Ctrl+V, Ctrl+X, Ctrl+A and Ctrl+Z inside the page itself, with no
// accelerator table of ours in between. There is nothing to add and nothing to fix.
//
// The second half of the constraint is the macOS build with cgo switched off, which is not a build
// anyone ships — a web view is cgo — but is exactly what type-checks window.go and this package
// against the darwin API without a Mac. It compiles; it has no menu, because it has no window
// either.
func installMenu(string) {}
