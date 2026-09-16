package main

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
)

// OpenBrowser hands a URL to whatever the platform uses to open one. A failure is not worth an
// error: the address is printed in the terminal as well, and a headless machine has no browser to
// open it with in the first place.
func OpenBrowser(ctx context.Context, url string) error {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "darwin":
		cmd = exec.CommandContext(ctx, "open", url)
	case "windows":
		cmd = exec.CommandContext(ctx, "rundll32", "url.dll,FileProtocolHandler", url)
	default:
		cmd = exec.CommandContext(ctx, "xdg-open", url)
	}
	return cmd.Start()
}

// OpenAppWindow is the middle step of the three the launcher has for showing the app: a
// Chromium-family browser in application mode, which is a window with the page in it and no tabs,
// address bar or bookmarks — the closest thing to our own window that a machine without a web view
// already has. It gets a profile of its own inside the data folder, so it is a separate window
// from the operator's browsing and keeps its own session.
//
// The context is deliberately not passed to the command: the browser outlives the launcher, and a
// Ctrl+C in the launcher's terminal must not take the operator's window down with it.
func OpenAppWindow(p Paths, url string) error {
	browser := findChromium(runtime.GOOS, homeDir(), exec.LookPath, runnableFile)
	if browser == "" {
		return os.ErrNotExist
	}
	profile := filepath.Join(p.Data, "browser-profile")
	cmd := exec.Command(browser, "--app="+url, "--user-data-dir="+profile)
	if err := cmd.Start(); err != nil {
		return err
	}
	// Nothing waits for this window, but something has to reap it or a Linux launcher that outlives
	// it collects a zombie.
	go func() { _ = cmd.Wait() }()
	return nil
}

// chromiumNames are the commands a Chromium-family browser is called on PATH, in the order they are
// tried: Chrome, then Edge, then Brave, then Chromium itself.
func chromiumNames(goos string) []string {
	if goos == "windows" {
		return []string{"chrome.exe", "msedge.exe", "brave.exe", "chromium.exe"}
	}
	return []string{"google-chrome", "google-chrome-stable", "microsoft-edge", "microsoft-edge-stable", "brave-browser", "chromium", "chromium-browser"}
}

// chromiumCandidates are the places those browsers are installed when PATH does not name them —
// which on macOS and Windows is the usual case, since neither installer puts the browser on PATH,
// and on macOS a program started from Finder has barely any PATH to look at. The order is the same
// one chromiumNames has: what the operator most likely uses first.
func chromiumCandidates(goos, home string) []string {
	var out []string
	switch goos {
	case "darwin":
		apps := []string{
			"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
			"/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
			"/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
			"/Applications/Chromium.app/Contents/MacOS/Chromium",
		}
		out = append(out, apps...)
		if home != "" {
			for _, app := range apps {
				out = append(out, home+app)
			}
		}
	case "windows":
		relative := []string{
			`\Google\Chrome\Application\chrome.exe`,
			`\Microsoft\Edge\Application\msedge.exe`,
			`\BraveSoftware\Brave-Browser\Application\brave.exe`,
			`\Chromium\Application\chrome.exe`,
		}
		roots := []string{`C:\Program Files`, `C:\Program Files (x86)`}
		if local := os.Getenv("LOCALAPPDATA"); local != "" {
			roots = append(roots, local)
		}
		for _, root := range roots {
			for _, rest := range relative {
				out = append(out, root+rest)
			}
		}
	default:
		dirs := []string{"/usr/bin", "/usr/local/bin", "/snap/bin", "/var/lib/flatpak/exports/bin"}
		for _, dir := range dirs {
			for _, name := range chromiumNames(goos) {
				out = append(out, dir+"/"+name)
			}
		}
		out = append(out, "/opt/google/chrome/chrome", "/opt/microsoft/msedge/msedge", "/opt/brave.com/brave/brave")
	}
	return out
}

// findChromium resolves the browser to run in application mode: PATH first, because an operator who
// put one there means it, then the known install locations. An empty answer means there is none,
// and the launcher falls through to the default browser.
func findChromium(goos, home string, lookPath func(string) (string, error), runnable func(string) bool) string {
	for _, name := range chromiumNames(goos) {
		if path, err := lookPath(name); err == nil {
			return path
		}
	}
	for _, candidate := range chromiumCandidates(goos, home) {
		if runnable(candidate) {
			return candidate
		}
	}
	return ""
}

func homeDir() string {
	home, _ := os.UserHomeDir()
	return home
}
