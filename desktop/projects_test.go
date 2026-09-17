package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func paths(t *testing.T) Paths {
	t.Helper()
	dir := t.TempDir()
	return Paths{Data: dir, Override: filepath.Join(dir, "compose.desktop.yaml")}
}

// The entry is the folder mapped to itself. A mount at any other container path leaves the project
// unreachable at the path it is stored under, which is worse than no mount because nothing says so.
func TestAMountedFolderIsTheSamePathOnBothSides(t *testing.T) {
	p := paths(t)
	if _, err := addMounts(p, []string{"/home/someone/work/bakery"}); err != nil {
		t.Fatal(err)
	}
	body := withProjectMounts(p, "services:\n  daedalus:\n    image: x\n  keyproxy:\n    image: x\n")
	want := "services:\n  daedalus:\n    volumes:\n      - /home/someone/work/bakery:/home/someone/work/bakery\n    image: x\n"
	if !strings.HasPrefix(body, want) {
		t.Fatalf("the override is:\n%s\nwant it to start with:\n%s", body, want)
	}
	if strings.Count(body, "services:") != 1 {
		t.Fatalf("a YAML document has one services key; the override has %d:\n%s", strings.Count(body, "services:"), body)
	}
}

// The override is rewritten from scratch on every start, so the mounts have to live somewhere the
// launcher owns or they would last exactly one start.
func TestTheMountListSurvivesTheOverrideBeingRewritten(t *testing.T) {
	p := paths(t)
	if _, err := addMounts(p, []string{"/a/one"}); err != nil {
		t.Fatal(err)
	}
	added, err := addMounts(p, []string{"/a/one", "/a/two"})
	if err != nil {
		t.Fatal(err)
	}
	if len(added) != 1 || added[0] != "/a/two" {
		t.Fatalf("added %v, want only /a/two — /a/one was already there", added)
	}
	if again, err := addMounts(p, []string{"/a/one"}); err != nil || len(again) != 0 {
		t.Fatalf("adding a folder twice added %v (%v)", again, err)
	}
	if got := readMounts(p); len(got) != 2 || got[0] != "/a/one" || got[1] != "/a/two" {
		t.Fatalf("the mount list is %v", got)
	}
	body := withProjectMounts(p, "services:\n  daedalus:\n    image: x\n")
	for _, root := range []string{"/a/one", "/a/two"} {
		if !strings.Contains(body, "      - "+root+":"+root+"\n") {
			t.Fatalf("%s is missing from the override:\n%s", root, body)
		}
	}
}

// With nothing to mount the override is exactly what it was, which is every native installation and
// every Docker one whose folders are all visible already.
func TestAnEmptyMountListChangesNothing(t *testing.T) {
	p := paths(t)
	body := "services:\n  daedalus:\n    image: x\n"
	if got := withProjectMounts(p, body); got != body {
		t.Fatalf("the override was changed with nothing to mount:\n%s", got)
	}
}

// A path that cannot be written as one compose entry must not be written as a broken one.
func TestAPathThatWouldBreakTheEntryIsRefused(t *testing.T) {
	for _, bad := range []string{"work/bakery", "/home/a:b", "/home/a\nservices:", "/", ""} {
		if refuseMount(bad) == "" {
			t.Fatalf("%q was accepted as a mount", bad)
		}
	}
	if reason := refuseMount("/home/someone/work/bakery"); reason != "" {
		t.Fatalf("an ordinary folder was refused: %s", reason)
	}
	// And one that slipped into the file by hand is ignored rather than spliced into the YAML.
	p := paths(t)
	if err := os.WriteFile(mountsFile(p), []byte("/a/good\n/bad:path\n\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := readMounts(p); len(got) != 1 || got[0] != "/a/good" {
		t.Fatalf("the mount list is %v", got)
	}
}
