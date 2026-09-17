package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestOnlyTheTwoModesAreAccepted(t *testing.T) {
	for _, value := range []string{"docker", " Docker ", "native", "NATIVE"} {
		if _, err := ParseMode(value); err != nil {
			t.Errorf("%q was refused: %v", value, err)
		}
	}
	if mode, err := ParseMode(""); err != nil || mode != ModeUnset {
		t.Fatalf("an empty mode is not the unchosen one: %v %v", mode, err)
	}
	// A typo starts the wrong kind of installation, so it is refused by name rather than guessed at.
	for _, value := range []string{"dockr", "container", "local", "podman"} {
		if _, err := ParseMode(value); err == nil {
			t.Errorf("%q was taken for a mode", value)
		}
	}
}

// The choice is made once and remembered: a start after the first asks nothing.
func TestTheChoiceIsRememberedAndTheFlagWins(t *testing.T) {
	paths, err := NewPaths(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if mode, err := ResolveMode(paths, ModeUnset); err != nil || mode != ModeUnset {
		t.Fatalf("a fresh folder has a mode already: %v %v", mode, err)
	}
	if err := StoreMode(paths, ModeNative); err != nil {
		t.Fatal(err)
	}
	if mode, _ := ResolveMode(paths, ModeUnset); mode != ModeNative {
		t.Fatalf("the stored choice was not read back: %v", mode)
	}
	if mode, _ := ResolveMode(paths, ModeDocker); mode != ModeDocker {
		t.Fatal("--mode did not win over the stored choice")
	}
	// DAEDALUS_MODE is as deliberate as the flag — a shell that sets it up once and runs the
	// launcher many times — so it wins over the stored choice in the same way and for the same reason.
	t.Setenv("DAEDALUS_MODE", "docker")
	if mode, _ := ResolveMode(paths, ModeUnset); mode != ModeDocker {
		t.Fatal("the environment did not win over the stored choice")
	}
	if _, err := ResolveMode(paths, ModeUnset); err != nil {
		t.Fatal(err)
	}
	t.Setenv("DAEDALUS_MODE", "podman")
	if _, err := ResolveMode(paths, ModeUnset); err == nil {
		t.Fatal("an environment naming no mode at all was taken for one")
	}
}

// An installation made before there was a choice is a Docker one, and is not asked about it: its
// environment file is already written and its containers already exist.
func TestAnExistingInstallationIsADockerOne(t *testing.T) {
	data := t.TempDir()
	paths, err := NewPaths(data)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(paths.Env, []byte("API_PORT=8765\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if mode, _ := ResolveMode(paths, ModeUnset); mode != ModeDocker {
		t.Fatalf("a configured installation with no mode file read as %v", mode)
	}
}

// A file holding something unrecognisable is a question to ask again, not a reason to refuse to
// start: the operator gets the choice back rather than an error about one word in one file.
func TestAnUnreadableChoiceIsAskedAgain(t *testing.T) {
	data := t.TempDir()
	paths, err := NewPaths(data)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(data, "mode"), []byte("podman\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if mode := StoredMode(paths); mode != ModeUnset {
		t.Fatalf("an unrecognisable mode file read as %v", mode)
	}
}

// What the operator is told about each mode has to name the trade, not just the technology.
func TestEachModeSaysWhatItCosts(t *testing.T) {
	if !strings.Contains(ModeNative.Describe(), "no container boundary") {
		t.Errorf("native mode does not say what it gives up: %q", ModeNative.Describe())
	}
	if !strings.Contains(ModeDocker.Describe(), "container") {
		t.Errorf("docker mode does not say what it buys: %q", ModeDocker.Describe())
	}
	if ModeUnset.Describe() == "" {
		t.Error("an unchosen mode says nothing at all")
	}
}
