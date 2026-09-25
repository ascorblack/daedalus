package shellint

import (
	"slices"
	"strings"
	"testing"
)

// What a Windows daemon starts, checked on every platform.

func TestAWindowsDaemonIntegratesPowerShellOnly(t *testing.T) {
	if got := KindsOn("windows"); !slices.Equal(got, []string{Pwsh}) {
		t.Errorf("windows: %v", got)
	}
	if got := KindsOn("linux"); !slices.Equal(got, Kinds) {
		t.Errorf("linux: %v", got)
	}
}

func TestAWindowsShellGetsNoLoginFlag(t *testing.T) {
	for _, shell := range []string{"powershell.exe", "pwsh.exe", "cmd.exe"} {
		if got := PlainArgs(shell, true, "windows"); !slices.Equal(got, []string{shell}) {
			t.Errorf("%s: %v", shell, got)
		}
	}
	if got := PlainArgs("/bin/bash", true, "linux"); !slices.Equal(got, []string{"/bin/bash", "-l"}) {
		t.Errorf("bash: %v", got)
	}
	if got := PlainArgs("/bin/bash", false, "linux"); !slices.Equal(got, []string{"/bin/bash"}) {
		t.Errorf("bash, not login: %v", got)
	}
}

// Windows PowerShell has no -Login, and the script reaches it as text run as a script block, which
// no execution policy stops: a dot-sourced file would be refused under the default Restricted one.
func TestWindowsPowerShellLoadsTheScriptPastAnExecutionPolicy(t *testing.T) {
	l, ok := Rewrite(Pwsh, `C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe`, true, `C:\data\ptyd\shell`, "n", "", false)
	if !ok {
		t.Fatal("not rewritten")
	}
	if slices.Contains(l.Argv, "-Login") {
		t.Errorf("Windows PowerShell given -Login: %v", l.Argv)
	}
	if len(l.Argv) != 5 || l.Argv[1] != "-NoLogo" || l.Argv[2] != "-NoExit" || l.Argv[3] != "-Command" {
		t.Fatalf("argv: %q", l.Argv)
	}
	cmd := l.Argv[4]
	if !strings.HasPrefix(cmd, "try { . ([scriptblock]::Create([IO.File]::ReadAllText('C:\\data\\ptyd\\shell") ||
		!strings.HasSuffix(cmd, "init.ps1'))) } catch { }") {
		t.Errorf("command: %s", cmd)
	}
	if l.Env[NonceEnv] != "n" {
		t.Errorf("env: %v", l.Env)
	}
}
