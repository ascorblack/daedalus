package main

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// tarGz builds the shape codeload serves: every path under one wrapper folder named after the
// repository and the commit.
func tarGz(t *testing.T, root string, files map[string]string) []byte {
	t.Helper()
	var buf bytes.Buffer
	zipped := gzip.NewWriter(&buf)
	archive := tar.NewWriter(zipped)
	if err := archive.WriteHeader(&tar.Header{Name: root + "/", Typeflag: tar.TypeDir, Mode: 0o755}); err != nil {
		t.Fatal(err)
	}
	for name, body := range files {
		header := &tar.Header{Name: root + "/" + name, Typeflag: tar.TypeReg, Mode: 0o644, Size: int64(len(body))}
		if strings.HasSuffix(name, ".sh") {
			header.Mode = 0o755
		}
		if err := archive.WriteHeader(header); err != nil {
			t.Fatal(err)
		}
		if _, err := archive.Write([]byte(body)); err != nil {
			t.Fatal(err)
		}
	}
	if err := archive.Close(); err != nil {
		t.Fatal(err)
	}
	if err := zipped.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func TestTheTarballURLFollowsTheRemote(t *testing.T) {
	for remote, want := range map[string]string{
		"https://github.com/ascorblack/daedalus":     "https://codeload.github.com/ascorblack/daedalus/tar.gz/refs/heads/main",
		"https://github.com/ascorblack/daedalus.git": "https://codeload.github.com/ascorblack/daedalus/tar.gz/refs/heads/main",
		"git@github.com:a-fork/protocore-exp.git":    "https://codeload.github.com/a-fork/protocore-exp/tar.gz/refs/heads/main",
		"ssh://git@github.com/a-fork/daedalus":       "https://codeload.github.com/a-fork/daedalus/tar.gz/refs/heads/main",
		"nonsense":                                   "",
	} {
		if got := tarballURL(remote); got != want {
			t.Fatalf("%s: got %q, want %q", remote, got, want)
		}
	}
}

func TestFetchAndUnpackDropTheWrapperFolder(t *testing.T) {
	archive := tarGz(t, "daedalus-abc123", map[string]string{
		"README.md":        "the bot",
		"deploy/setup.sh":  "#!/bin/sh\n",
		"daedalus/main.py": "print(1)\n",
	})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/ascorblack/daedalus/tar.gz/refs/heads/main" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		w.Write(archive)
	}))
	defer server.Close()

	body, err := fetchTarball(context.Background(), server.URL+"/ascorblack/daedalus/tar.gz/refs/heads/main")
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	if err := unpackTarball(body, dir); err != nil {
		t.Fatal(err)
	}
	for name, want := range map[string]string{"README.md": "the bot", "daedalus/main.py": "print(1)\n"} {
		got, err := os.ReadFile(filepath.Join(dir, filepath.FromSlash(name)))
		if err != nil {
			t.Fatal(err)
		}
		if string(got) != want {
			t.Fatalf("%s: got %q", name, got)
		}
	}
	// An executable in the tree stays executable: deploy/setup.sh is run, not read.
	info, err := os.Stat(filepath.Join(dir, "deploy", "setup.sh"))
	if err != nil {
		t.Fatal(err)
	}
	if os.Getuid() >= 0 && info.Mode().Perm()&0o111 == 0 {
		t.Fatalf("the mode was dropped: %v", info.Mode())
	}
}

func TestAMissingTarballIsAnError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	}))
	defer server.Close()
	if _, err := fetchTarball(context.Background(), server.URL+"/nope"); err == nil {
		t.Fatal("a 404 passed for a tree")
	}
	if _, err := fetchTarball(context.Background(), ""); err == nil {
		t.Fatal("a remote that is not on GitHub passed for one")
	}
}

// An archive is downloaded code. An entry that climbs out of the checkout is refused rather than
// written where it asked to be.
func TestAnEntryCannotEscapeTheCheckout(t *testing.T) {
	archive := tarGz(t, "daedalus-abc123", map[string]string{"../../escaped": "no"})
	dir := t.TempDir()
	if err := unpackTarball(archive, filepath.Join(dir, "checkout")); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dir, "escaped")); !os.IsNotExist(err) {
		t.Fatal("an entry was written outside the checkout")
	}
}

func TestUnpackOverwritesWithoutEmptyingTheCheckout(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "README.md"), []byte("old"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, ".env"), []byte("KEY=1"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := unpackTarball(tarGz(t, "daedalus-abc123", map[string]string{"README.md": "new"}), dir); err != nil {
		t.Fatal(err)
	}
	if body, _ := os.ReadFile(filepath.Join(dir, "README.md")); string(body) != "new" {
		t.Fatalf("the tracked file was not replaced: %q", body)
	}
	// The .env is the installation's, not the repository's: an update must not take it.
	if body, _ := os.ReadFile(filepath.Join(dir, ".env")); string(body) != "KEY=1" {
		t.Fatalf("the .env did not survive the update: %q", body)
	}
}

// A symlink entry is the other half of keeping an archive inside the checkout: the entry's own name
// is cleaned, but a link laid down first and traversed by a later entry writes wherever it points.
func TestASymlinkEntryIsRefused(t *testing.T) {
	var buf bytes.Buffer
	zipped := gzip.NewWriter(&buf)
	archive := tar.NewWriter(zipped)
	if err := archive.WriteHeader(&tar.Header{Name: "daedalus-abc/escape", Typeflag: tar.TypeSymlink, Linkname: "../../victim", Mode: 0o777}); err != nil {
		t.Fatal(err)
	}
	body := "OWNED BY THE ARCHIVE\n"
	if err := archive.WriteHeader(&tar.Header{Name: "daedalus-abc/escape/authorized_keys", Typeflag: tar.TypeReg, Mode: 0o644, Size: int64(len(body))}); err != nil {
		t.Fatal(err)
	}
	if _, err := archive.Write([]byte(body)); err != nil {
		t.Fatal(err)
	}
	if err := archive.Close(); err != nil {
		t.Fatal(err)
	}
	if err := zipped.Close(); err != nil {
		t.Fatal(err)
	}

	dir := t.TempDir()
	victim := filepath.Join(dir, "victim")
	if err := os.MkdirAll(victim, 0o755); err != nil {
		t.Fatal(err)
	}
	checkout := filepath.Join(dir, "home", "checkout")
	if err := os.MkdirAll(checkout, 0o755); err != nil {
		t.Fatal(err)
	}
	err := unpackTarball(buf.Bytes(), checkout)
	if err == nil {
		t.Fatal("a symlink entry was accepted")
	}
	if !strings.Contains(err.Error(), "not a file or a directory") {
		t.Fatalf("refused for the wrong reason: %v", err)
	}
	if _, err := os.Stat(filepath.Join(victim, "authorized_keys")); !os.IsNotExist(err) {
		t.Fatal("the archive wrote outside the checkout")
	}
}

// And a link already in the checkout is not what a write follows either: the archive may have been
// unpacked before this was checked, or the agent may have made one.
func TestAWriteDoesNotFollowALinkAlreadyInTheCheckout(t *testing.T) {
	dir := t.TempDir()
	outside := filepath.Join(dir, "outside")
	if err := os.MkdirAll(outside, 0o755); err != nil {
		t.Fatal(err)
	}
	checkout := filepath.Join(dir, "checkout")
	if err := os.MkdirAll(filepath.Join(checkout, "deploy"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(outside, "taken"), filepath.Join(checkout, "README.md")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(checkout, "linked")); err != nil {
		t.Fatal(err)
	}
	if err := unpackTarball(tarGz(t, "daedalus-abc", map[string]string{"README.md": "new"}), checkout); err != nil {
		t.Fatal(err)
	}
	if body, _ := os.ReadFile(filepath.Join(checkout, "README.md")); string(body) != "new" {
		t.Fatalf("the file was not written in the link's place: %q", body)
	}
	if _, err := os.Stat(filepath.Join(outside, "taken")); !os.IsNotExist(err) {
		t.Fatal("the write followed the link")
	}
	// A directory in the path that is a link is refused rather than walked through.
	err := unpackTarball(tarGz(t, "daedalus-abc", map[string]string{"linked/pwned": "x"}), checkout)
	if err == nil || !strings.Contains(err.Error(), "symbolic link") {
		t.Fatalf("a link in the path was walked through: %v", err)
	}
}

// The tarball comes from codeload.github.com, so a remote that is not on GitHub must fail rather
// than fetch the GitHub repository of the same owner and name — a different repository entirely.
func TestANonGitHubRemoteIsNotFetchedFromGitHub(t *testing.T) {
	for _, remote := range []string{
		"https://gitlab.com/someone/daedalus",
		"git@gitlab.com:someone/daedalus.git",
		"ssh://git@git.example.invalid/someone/daedalus",
		"https://github.example.invalid/someone/daedalus",
	} {
		if got := tarballURL(remote); got != "" {
			t.Fatalf("%s: got %q, want no URL at all", remote, got)
		}
	}
	if _, err := fetchTarball(context.Background(), tarballURL("https://gitlab.com/someone/daedalus")); err == nil {
		t.Fatal("a non-GitHub remote was fetched")
	}
}

// A directory dropped upstream must not survive an update as an empty shell.
func TestRemovingTrackedFilesTakesTheDirectoriesWithThem(t *testing.T) {
	dir := t.TempDir()
	for _, name := range []string{"gone/deep/file.py", "kept/file.py", "kept/untracked.log"} {
		if err := os.MkdirAll(filepath.Dir(filepath.Join(dir, filepath.FromSlash(name))), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, filepath.FromSlash(name)), []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	for _, name := range []string{"gone/deep/file.py", "kept/file.py"} {
		target, err := safeJoin(dir, name)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.Remove(target); err != nil {
			t.Fatal(err)
		}
		pruneEmpty(dir, filepath.Dir(target))
	}
	if _, err := os.Stat(filepath.Join(dir, "gone")); !os.IsNotExist(err) {
		t.Fatal("an empty directory survived the update")
	}
	if _, err := os.Stat(filepath.Join(dir, "kept", "untracked.log")); err != nil {
		t.Fatalf("a directory that still holds something was removed: %v", err)
	}
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("the checkout root was removed: %v", err)
	}
}
