package main

import (
	"errors"
	"os"
	"strings"
	"testing"
)

func TestGeometryKeepsWhatIsUsable(t *testing.T) {
	paths := setupTempInstall(t)
	if got := ReadGeometry(paths); got != DefaultGeometry() {
		t.Fatalf("a first start opened %+v, want the default", got)
	}
	WriteGeometry(paths, Geometry{Width: 1400, Height: 900, X: 40, Y: 20})
	if got := ReadGeometry(paths); got.Width != 1400 || got.Height != 900 || got.X != 40 || got.Y != 20 {
		t.Fatalf("the window came back as %+v", got)
	}
	// A window reported as a sliver, or off the side of the world, keeps what it had.
	kept := Geometry{Width: 1400, Height: 900}.With(2, 2, -900, -900)
	if kept.Width != 1400 || kept.Height != 900 || kept.X != 0 || kept.Y != 0 {
		t.Fatalf("nonsense was believed: %+v", kept)
	}
	if err := os.WriteFile(geometryFile(paths), []byte("{not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	if got := ReadGeometry(paths); got != DefaultGeometry() {
		t.Fatalf("a broken file opened %+v, want the default", got)
	}
}

func TestFindChromiumLooksWhereTheInstallersPut(t *testing.T) {
	// PATH first: an operator who put a browser there means it.
	onPath := func(name string) (string, error) {
		if name == "google-chrome" {
			return "/opt/bin/google-chrome", nil
		}
		return "", errors.New("not on PATH")
	}
	if got := findChromium("linux", "/home/x", onPath, func(string) bool { return false }); got != "/opt/bin/google-chrome" {
		t.Fatalf("PATH was not tried first: %s", got)
	}
	// Then the install locations, which is the case that matters: a program started from Finder or
	// from Explorer has a PATH with no browser in it.
	none := func(string) (string, error) { return "", errors.New("not on PATH") }
	edge := "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
	if got := findChromium("darwin", "/Users/x", none, func(path string) bool { return path == edge }); got != edge {
		t.Fatalf("the installed browser was not found: %s", got)
	}
	if got := findChromium("windows", "", none, func(path string) bool { return strings.HasSuffix(path, `Google\Chrome\Application\chrome.exe`) }); got == "" {
		t.Fatal("chrome in Program Files was not found")
	}
	// None installed is not an error: the default browser is the last step of the chain.
	if got := findChromium("linux", "", none, func(string) bool { return false }); got != "" {
		t.Fatalf("a machine with no Chromium answered %s", got)
	}
}
