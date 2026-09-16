package main

import (
	"context"
	"sync"
)

// Surface is where the operator sees Daedalus, and it is decided once per start by what the machine
// can actually do, never by a setting:
//
//  1. the launcher's own window, a web view the operating system already ships (windowed builds);
//  2. a Chromium-family browser in application mode, which is a window with no browser around it;
//  3. the default browser, which every machine has.
//
// Nothing here is a hard failure. A machine that cannot do the first does the second without saying
// anything about it, and one that cannot do either opens a tab.
type Surface struct {
	paths  Paths
	window *Window

	mu      sync.Mutex
	current string
}

// OpenSurface makes the window when there is one to make, and points it at the launcher's own page:
// the status, the buttons and — on a first start — the questions. The app comes later, when the
// stack answers. This has to be called from the goroutine that runs main.
func OpenSurface(paths Paths, launcher string) *Surface {
	surface := &Surface{paths: paths, current: launcher}
	if !windowBuild || !windowAvailable() {
		return surface
	}
	window, err := openWindow(paths, "Daedalus", launcher)
	if err != nil {
		return surface
	}
	surface.window = window
	return surface
}

// Windowed reports whether this start got a window of its own, which is what the status page says
// out loud so that "closing this leaves the stack running" is read about the right thing.
func (s *Surface) Windowed() bool { return s != nil && s.window != nil }

// Show puts a URL in front of the operator. In the window it is a navigation; without one it is an
// application-mode browser window, or a tab.
func (s *Surface) Show(ctx context.Context, url string) {
	if s == nil || url == "" {
		return
	}
	s.mu.Lock()
	s.current = url
	s.mu.Unlock()
	if s.window != nil {
		s.window.Show(url)
		return
	}
	if err := OpenAppWindow(s.paths, url); err == nil {
		return
	}
	_ = OpenBrowser(ctx, url)
}

// Focus is what a second launch of the launcher asks this one to do: come to the front, and go to
// the link the second launch was given if it was given one. Without a window there is no window to
// raise, and opening the URL again is what raises the browser instead.
func (s *Surface) Focus(ctx context.Context, url string) {
	if s == nil {
		return
	}
	if url == "" {
		s.mu.Lock()
		url = s.current
		s.mu.Unlock()
	}
	if s.window != nil {
		s.window.Show(url)
		s.window.Focus()
		return
	}
	s.Show(ctx, url)
}

// Run holds the launcher open: the window's event loop, or — with no window — until the context
// ends, which is the Ctrl+C the launcher has always waited for. Either way the containers are
// running detached and stay up.
func (s *Surface) Run(ctx context.Context) {
	if s.window != nil {
		s.window.Run(ctx)
		return
	}
	<-ctx.Done()
}
