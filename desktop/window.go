//go:build !nowebview && (darwin || windows)

// The native window. It is one file and one dependency — github.com/webview/webview_go, which is
// the operating system's own web view (WKWebView, WebView2, WebKitGTK) and not a browser shipped
// with the launcher. The pages it shows are the two the installation already serves: the
// launcher's own status page first, the app once the stack answers.
//
// A `nowebview` build compiles window_off.go instead and the launcher behaves as it did before
// there was a window: it opens a browser. That build tag is what keeps the cross-compiled
// dependency-free binaries possible, since a web view means cgo and cgo means no cross-compiling.
//
// Linux is not in the constraint at all, and the reason is not a preference. webview_go links
// GTK and WebKitGTK at load time, so a Linux binary built with it cannot start on a machine that
// does not have those libraries — not fall back, not warn: not start. One binary that runs on every
// Linux is worth more than a window of our own there, so the Linux builds take the fallback chain
// (an application-mode browser, then the default browser) and the window is a macOS and Windows
// thing. A Linux operator who wants the window has the source and the libraries and can build it.
package main

import (
	"context"
	"fmt"
	"os/exec"
	"runtime"
	"strings"
	"sync"
	"time"

	webview "github.com/webview/webview_go"
)

// windowBuild says that this build was compiled with the web view in it. It is false in the
// nowebview build, and the README's table of what each archive contains follows it.
const windowBuild = true

// geometryInterval is how often the page is asked where it is and how big it has become. The
// answer is only written out when the window closes, so this is not a write per tick.
const geometryInterval = 10 * time.Second

// Window is the launcher's own window and the state that outlives a navigation: where it is on the
// screen, so the next start opens it in the same place.
type Window struct {
	view  webview.WebView
	paths Paths

	mu   sync.Mutex
	geom Geometry
}

// windowAvailable reports whether this machine can show a web view at all. macOS always can — it
// is part of the system. Windows can once WebView2 is installed, which is the case out of the box
// on Windows 11 and wherever Edge has been updated; the runtime is asked for rather than assumed,
// because creating a web view without it takes the process down with it instead of failing.
// Linux is asked nothing: a binary that needs WebKitGTK to start is not built for Linux at all
// (see the README), so a Linux build reaching this point is one someone compiled themselves and
// therefore has the library.
func windowAvailable() bool {
	switch runtime.GOOS {
	case "darwin", "linux":
		return true
	case "windows":
		return webView2Installed()
	default:
		return false
	}
}

// webView2Installed asks the registry for the Evergreen runtime's version. Both hives are read:
// the runtime is installed per machine by the Edge installer and per user by the bootstrapper.
func webView2Installed() bool {
	const key = `Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}`
	for _, hive := range []string{`HKLM\` + key, `HKCU\` + key} {
		out, err := exec.Command("reg", "query", hive, "/v", "pv").CombinedOutput()
		if err == nil && strings.Contains(string(out), "pv") {
			return true
		}
	}
	return false
}

// openWindow creates the window and points it at the first page. It has to be called from the
// goroutine that runs main: a web view belongs to the thread that made it, and webview_go locks
// main to its thread for exactly that reason.
func openWindow(p Paths, title, url string) (*Window, error) {
	view := webview.New(false)
	if view == nil {
		return nil, fmt.Errorf("this machine has no web view the launcher can use")
	}
	w := &Window{view: view, paths: p, geom: ReadGeometry(p)}
	view.SetTitle(title)
	view.SetSize(w.geom.Width, w.geom.Height, webview.HintNone)
	// The page tells the launcher where the window is, since the web view exposes no way to ask.
	if err := view.Bind("__daedalusGeometry", w.record); err != nil {
		return nil, err
	}
	// What the app itself may raise. The web views do not all carry the Web Notifications API, and
	// the two that do would ask for a permission the operator has already given by installing this,
	// so the page is given a function instead of an API to ask for.
	if err := view.Bind("__daedalusNotify", func(title, body, link string) { _ = Notify(Notification{Title: title, Body: body, Link: link}) }); err != nil {
		return nil, err
	}
	view.Init(`window.daedalus = Object.assign(window.daedalus || {}, {
  notify: (title, body, link) => window.__daedalusNotify(String(title || ""), String(body || ""), String(link || "")),
  window: true,
});`)
	view.Navigate(url)
	// Position is restored once, after the first page is there to run it: moving a window is the
	// page's to do, and only some platforms allow it. A refusal is silent and costs the operator
	// nothing but a window the desktop placed itself.
	if w.geom.X > 0 || w.geom.Y > 0 {
		view.Eval(fmt.Sprintf("try { window.moveTo(%d, %d); } catch (e) {}", w.geom.X, w.geom.Y))
	}
	return w, nil
}

// record is what the page calls with the numbers only it can read.
func (w *Window) record(width, height, x, y int) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.geom = w.geom.With(width, height, x, y)
}

// Show points the window at a URL. It is safe from any goroutine: the call is posted to the thread
// the window lives on.
func (w *Window) Show(url string) {
	w.view.Dispatch(func() { w.view.Navigate(url) })
}

// Focus is what a second launch asks the first for. Raising a native window is not something the
// web view offers, so what is asked is the page's own focus — which is enough on the platforms
// that honour it, and on the others the window is at least showing the right thing when the
// operator finds it.
func (w *Window) Focus() {
	w.view.Dispatch(func() { w.view.Eval("try { window.focus(); } catch (e) {}") })
}

// Run shows the window and returns when it is closed, or when the context ends — Ctrl+C in the
// terminal the launcher was started from. Closing the window stops the launcher and nothing else:
// the containers are started detached and keep running, which is what the status page says.
func (w *Window) Run(ctx context.Context) {
	done := make(chan struct{})
	go func() {
		ticker := time.NewTicker(geometryInterval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				w.view.Terminate()
				return
			case <-done:
				return
			case <-ticker.C:
				w.view.Dispatch(func() {
					w.view.Eval("try { window.__daedalusGeometry(window.outerWidth, window.outerHeight, window.screenX, window.screenY); } catch (e) {}")
				})
			}
		}
	}()
	w.view.Run()
	close(done)
	w.mu.Lock()
	geom := w.geom
	w.mu.Unlock()
	WriteGeometry(w.paths, geom)
	w.view.Destroy()
}
