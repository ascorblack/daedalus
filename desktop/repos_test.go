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
