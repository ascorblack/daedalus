//go:build nowebview || (!darwin && !windows)

// The launcher without a window. Everything the windowed build does through a web view this build
// does through a browser, which is what the launcher did before there was a window at all.
//
// This is every Linux build and every build made with the `nowebview` tag. On Linux it is not a
// lesser build but the only sound one: a binary linked against WebKitGTK cannot start on a machine
// that does not have it, and "one binary that runs everywhere" is worth more there than a window of
// our own. See the README, and the comment at the top of window.go.
package main

import (
	"context"
	"errors"
)

const windowBuild = false

// Window exists so that the rest of the launcher does not have to know which build it is in.
type Window struct{}

func windowAvailable() bool { return false }

func openWindow(Paths, string, string) (*Window, error) {
	return nil, errors.New("this build has no window")
}

func (w *Window) Show(string) {}

func (w *Window) Focus() {}

func (w *Window) Run(ctx context.Context) { <-ctx.Done() }
