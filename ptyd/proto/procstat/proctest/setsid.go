// Package proctest holds what the tests of process trees share.
package proctest

import (
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

// setsidScript does what util-linux's setsid does without options: a process that leads its group
// cannot start a session, so it forks first, and the parent returns at once.
const setsidScript = `use POSIX qw(setsid);
if (getpgrp() == $$) { exit 0 if fork(); }
setsid() or die "setsid: $!\n";
exec { $ARGV[0] } @ARGV or die "setsid: $ARGV[0]: $!\n";
`

// Setsid makes a `setsid` command available to the shells a test starts, which is how the tests
// make a process leave its terminal's session. Linux has one; macOS has none, and there Perl, which
// every Mac carries, stands in for it on the PATH the test's processes inherit.
func Setsid(t testing.TB) {
	t.Helper()
	if _, err := exec.LookPath("setsid"); err == nil {
		return
	}
	perl, err := exec.LookPath("perl")
	if err != nil {
		t.Skip("neither setsid nor perl to stand in for it")
	}
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "setsid"), []byte("#!"+perl+"\n"+setsidScript), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
}
