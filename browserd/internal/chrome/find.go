// Package chrome finds a Chromium, starts it on a profile with its debugging pipe, and watches it.
package chrome

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strings"
	"time"
)

// Found is a browser to run, and whether it came with the installation.
type Found struct {
	Path string
	Kind string // "bundled" (Playwright's pinned build) or "system"
}

// Find resolves which Chromium to run: the configured path, then $BROWSERD_CHROMIUM, then
// Playwright's pinned chromium under $PLAYWRIGHT_BROWSERS_PATH (or its default cache), then a system
// Chrome, Chromium or Edge. The pinned build comes before the system's because the screencast is an
// experimental part of the protocol, and the pinned build is the one the tests ran against.
func Find(configured string) (Found, bool) {
	for _, p := range []string{configured, os.Getenv("BROWSERD_CHROMIUM")} {
		if p != "" {
			return Found{Path: p, Kind: kindOf(p)}, executable(p)
		}
	}
	for _, dir := range playwrightDirs() {
		if p := newestPlaywright(dir); p != "" {
			return Found{Path: p, Kind: "bundled"}, true
		}
	}
	for _, p := range systemCandidates() {
		if filepath.IsAbs(p) {
			if executable(p) {
				return Found{Path: p, Kind: "system"}, true
			}
			continue
		}
		if full, err := exec.LookPath(p); err == nil {
			return Found{Path: full, Kind: "system"}, true
		}
	}
	return Found{}, false
}

// kindOf names a configured path: Playwright's builds live in directories it names chromium-<rev>.
func kindOf(path string) string {
	if strings.Contains(filepath.ToSlash(path), "/chromium-") || strings.Contains(filepath.ToSlash(path), "/chromium_headless_shell-") {
		return "bundled"
	}
	return "system"
}

func playwrightDirs() []string {
	var dirs []string
	if d := os.Getenv("PLAYWRIGHT_BROWSERS_PATH"); d != "" && d != "0" {
		dirs = append(dirs, d)
	}
	home, _ := os.UserHomeDir()
	switch runtime.GOOS {
	case "linux":
		if home != "" {
			dirs = append(dirs, filepath.Join(home, ".cache", "ms-playwright"))
		}
	case "darwin":
		if home != "" {
			dirs = append(dirs, filepath.Join(home, "Library", "Caches", "ms-playwright"))
		}
	case "windows":
		if d := os.Getenv("LOCALAPPDATA"); d != "" {
			dirs = append(dirs, filepath.Join(d, "ms-playwright"))
		}
	}
	return dirs
}

// newestPlaywright is the executable of the highest-numbered full chromium build in dir.
func newestPlaywright(dir string) string {
	entries, _ := filepath.Glob(filepath.Join(dir, "chromium-*"))
	sort.Slice(entries, func(i, j int) bool { return revision(entries[i]) > revision(entries[j]) })
	for _, e := range entries {
		for _, rel := range playwrightExecutables() {
			p := filepath.Join(e, rel)
			if executable(p) {
				return p
			}
		}
	}
	return ""
}

func revision(dir string) int {
	n := 0
	for _, r := range strings.TrimPrefix(filepath.Base(dir), "chromium-") {
		if r < '0' || r > '9' {
			return -1
		}
		n = n*10 + int(r-'0')
	}
	return n
}

func playwrightExecutables() []string {
	switch runtime.GOOS {
	case "darwin":
		return []string{
			"chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
			"chrome-mac/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
			"chrome-mac/Chromium.app/Contents/MacOS/Chromium",
		}
	case "windows":
		return []string{`chrome-win64\chrome.exe`, `chrome-win\chrome.exe`}
	}
	return []string{"chrome-linux64/chrome", "chrome-linux/chrome"}
}

func systemCandidates() []string {
	switch runtime.GOOS {
	case "darwin":
		return []string{
			"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
			"/Applications/Chromium.app/Contents/MacOS/Chromium",
			"/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
		}
	case "windows":
		var out []string
		for _, env := range []string{"ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"} {
			if d := os.Getenv(env); d != "" {
				out = append(out, filepath.Join(d, `Google\Chrome\Application\chrome.exe`),
					filepath.Join(d, `Microsoft\Edge\Application\msedge.exe`))
			}
		}
		return out
	}
	return []string{"google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"}
}

func executable(path string) bool {
	st, err := os.Stat(path)
	if err != nil || st.IsDir() {
		return false
	}
	return runtime.GOOS == "windows" || st.Mode()&0o111 != 0
}

var versionPattern = regexp.MustCompile(`\b(\d+\.\d+\.\d+\.\d+)\b`)

// ProbeVersion asks a Chromium for its version without starting a browser (`--version`), so the
// daemon can say which one it would run before anything has: a doctor that reads "version unknown"
// until the agent first browses tells the operator nothing. It answers in the product form a running
// browser reports ("Chrome/151.0.7922.34"), or "" when the build does not print one: Windows builds
// are window programs and print nothing.
func ProbeVersion(path string, timeout time.Duration) string {
	if path == "" || runtime.GOOS == "windows" {
		return ""
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	out, err := exec.CommandContext(ctx, path, "--version").Output()
	if err != nil {
		return ""
	}
	if m := versionPattern.FindSubmatch(out); m != nil {
		return "Chrome/" + string(m[1])
	}
	return ""
}
