//go:build linux

package chrome

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func TestReadCgroup(t *testing.T) {
	dir := t.TempDir()
	self := filepath.Join(dir, "self-cgroup")
	mount := filepath.Join(dir, "sys")
	cg := filepath.Join(mount, "system.slice", "browser.scope")
	if err := os.MkdirAll(cg, 0o755); err != nil {
		t.Fatal(err)
	}
	write := func(path, text string) {
		t.Helper()
		if err := os.WriteFile(path, []byte(text), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	write(self, "0::/system.slice/browser.scope\n")
	// The page cache (file) and the kernel's own are left out: only what the browsers hold.
	write(filepath.Join(cg, "memory.stat"), "anon 400000000\nfile 3000000\nkernel 21000000\nshmem 2000000\n")
	write(filepath.Join(cg, "cgroup.procs"), "11\n12\n13\n")
	got, ok := readCgroup(self, mount)
	if !ok {
		t.Fatal("a v2 cgroup with its files was not read")
	}
	if got.AnonShmem != 402000000 || !reflect.DeepEqual(got.Procs, []int{11, 12, 13}) || got.Path != "/system.slice/browser.scope" {
		t.Fatalf("read %+v", got)
	}

	// The root cgroup has no memory.stat: nothing to read, and the caller keeps its estimate.
	write(self, "0::/\n")
	if _, ok := readCgroup(self, mount); ok {
		t.Fatal("the root cgroup was read as the daemon's own")
	}
	// A cgroup v1 machine names no unified hierarchy.
	write(self, "12:memory:/user.slice\n")
	if _, ok := readCgroup(self, mount); ok {
		t.Fatal("a v1 hierarchy was read as v2")
	}
}
