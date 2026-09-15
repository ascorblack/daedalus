package main

import (
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestComposeArgsPutTheServerFileFirst(t *testing.T) {
	paths, err := NewPaths(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	got := composeArgs(paths, false, "up", "-d")
	want := []string{
		"compose",
		"-f", paths.Compose,
		"-f", paths.Override,
		"--env-file", paths.Env,
		"--project-name", "daedalus",
		"up", "-d",
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("args are\n%v\nwant\n%v", got, want)
	}
	// Compose resolves the relative mounts of a compose file against the project directory, which
	// is the directory of the first -f. Anything but the checkout's deploy/ breaks every mount.
	if filepath.Dir(got[2]) != filepath.Join(paths.Bot, "deploy") {
		t.Fatalf("the project directory would be %s", filepath.Dir(got[2]))
	}
}

func TestComposeArgsEnableTheTelegramProfileOnlyWithAToken(t *testing.T) {
	paths, err := NewPaths(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	with := strings.Join(composeArgs(paths, true, "up"), " ")
	if !strings.Contains(with, "--profile telegram") {
		t.Fatalf("no profile with a token: %s", with)
	}
	without := strings.Join(composeArgs(paths, false, "up"), " ")
	if strings.Contains(without, "--profile") {
		t.Fatalf("a profile without a token: %s", without)
	}
	if !strings.HasSuffix(with, "up") || !strings.HasSuffix(without, "up") {
		t.Fatal("the subcommand must stay last")
	}
}

func TestOverrideNamesThePublishedImagesAndFreesTelegram(t *testing.T) {
	paths, err := NewPaths(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	if err := paths.EnsureDirs(); err != nil {
		t.Fatal(err)
	}
	if err := WriteOverride(paths); err != nil {
		t.Fatal(err)
	}
	body, err := os.ReadFile(paths.Override)
	if err != nil {
		t.Fatal(err)
	}
	text := string(body)
	for _, want := range []string{
		agentImage,
		keyproxyImage,
		`profiles: ["telegram"]`,
		// Without required:false the whole project refuses to load while the profile is off,
		// because daedalus depends on a service that is not in the project.
		"required: false",
	} {
		if !strings.Contains(text, want) {
			t.Fatalf("the override is missing %q:\n%s", want, text)
		}
	}
}

func TestGitRunsInAContainerOverTheDataFolder(t *testing.T) {
	paths, err := NewPaths(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	args := strings.Join(gitArgs(paths, "clone", "--depth", cloneDepth, botRemote(), "/work/daedalus"), " ")
	if !strings.Contains(args, paths.Data+":/work") || !strings.Contains(args, gitImage) {
		t.Fatalf("git would not see the data folder: %s", args)
	}
	if !strings.Contains(args, "--depth 50") {
		t.Fatalf("the clone has no history for the supervisor to roll back to: %s", args)
	}
	if os.Getuid() >= 0 && !strings.Contains(args, fmt.Sprintf("--user %d:%d", os.Getuid(), os.Getgid())) {
		t.Fatalf("the checkout would belong to root, and the launcher could not write .env into it: %s", args)
	}
}

func TestAForkOverridesTheRemote(t *testing.T) {
	t.Setenv("DAEDALUS_GIT_REMOTE", "https://example.invalid/fork")
	t.Setenv("DAEDALUS_CORE_GIT_REMOTE", "https://example.invalid/core")
	if botRemote() != "https://example.invalid/fork" || coreRemote() != "https://example.invalid/core" {
		t.Fatal("the fork remotes are ignored")
	}
}
