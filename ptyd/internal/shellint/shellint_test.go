package shellint

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestKindOf(t *testing.T) {
	for program, want := range map[string]string{
		"/bin/bash": Bash, "-bash": Bash, "zsh": Zsh, "/usr/bin/fish": Fish, "pwsh": Pwsh,
		`C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe`: Pwsh, "PWSH.EXE": Pwsh,
		"/bin/sh": "", "dash": "", "nu": "", "": "",
	} {
		if got := KindOf(program); got != want {
			t.Errorf("KindOf(%q) = %q, want %q", program, got, want)
		}
	}
}

func TestInstallWritesEveryScript(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "shell")
	// An older daemon's copy is replaced.
	if err := os.MkdirAll(filepath.Join(dir, "bash"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "bash", "init.sh"), []byte("old"), 0o600); err != nil {
		t.Fatal(err)
	}
	got, err := Install(dir)
	if err != nil || got != dir {
		t.Fatalf("Install: %q %v", got, err)
	}
	for _, name := range []string{"bash/init.sh", "zsh/.zshenv", "zsh/.zprofile", "zsh/.zshrc", "fish/init.fish", "pwsh/init.ps1"} {
		path := filepath.Join(dir, filepath.FromSlash(name))
		st, err := os.Stat(path)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if st.Mode().Perm() != 0o644 {
			t.Errorf("%s: mode %v", name, st.Mode().Perm())
		}
		data, _ := os.ReadFile(path)
		want, _ := files.ReadFile("files/" + name)
		if string(data) != string(want) {
			t.Errorf("%s: not the embedded script", name)
		}
	}
	if st, _ := os.Stat(filepath.Join(dir, "bash")); st.Mode().Perm() != 0o755 {
		t.Errorf("directory mode %v", st.Mode().Perm())
	}
	leftovers, _ := filepath.Glob(filepath.Join(dir, "*", "*.tmp"))
	if len(leftovers) > 0 {
		t.Errorf("left behind: %v", leftovers)
	}
}

func TestRewrite(t *testing.T) {
	dir := "/state/shell"
	cases := []struct {
		kind, shell string
		login       bool
		zdot        string
		hasZdot     bool
		argv        string
		env         map[string]string
	}{
		{Bash, "/bin/bash", true, "", false, "/bin/bash --init-file /state/shell/bash/init.sh -i",
			map[string]string{NonceEnv: "n", LoginEnv: "1"}},
		{Bash, "bash", false, "", false, "bash --init-file /state/shell/bash/init.sh -i",
			map[string]string{NonceEnv: "n"}},
		{Zsh, "zsh", true, "", false, "zsh -l", map[string]string{NonceEnv: "n", "ZDOTDIR": "/state/shell/zsh"}},
		{Zsh, "zsh", false, "/opt/zdot", true, "zsh",
			map[string]string{NonceEnv: "n", "ZDOTDIR": "/state/shell/zsh", UserZdotdirEnv: "/opt/zdot"}},
		{Fish, "fish", true, "", false, "fish --login --init-command source '/state/shell/fish/init.fish'",
			map[string]string{NonceEnv: "n"}},
		{Pwsh, "pwsh", true, "", false, "pwsh -Login -NoLogo -NoExit -Command try { . '/state/shell/pwsh/init.ps1' } catch { }",
			map[string]string{NonceEnv: "n"}},
	}
	for _, c := range cases {
		l, ok := Rewrite(c.kind, c.shell, c.login, dir, "n", c.zdot, c.hasZdot)
		if !ok {
			t.Fatalf("%s: not rewritten", c.kind)
		}
		if got := strings.Join(l.Argv, " "); got != c.argv {
			t.Errorf("%s argv: %q, want %q", c.kind, got, c.argv)
		}
		if len(l.Env) != len(c.env) {
			t.Errorf("%s env: %v, want %v", c.kind, l.Env, c.env)
		}
		for k, v := range c.env {
			if l.Env[k] != v {
				t.Errorf("%s env %s: %q, want %q", c.kind, k, l.Env[k], v)
			}
		}
	}
	if _, ok := Rewrite("", "sh", true, dir, "n", "", false); ok {
		t.Error("sh has no integration")
	}
}

func TestQuoting(t *testing.T) {
	if got := fishQuote(`/a b/it's\x`); got != `'/a b/it\'s\\x'` {
		t.Errorf("fish: %s", got)
	}
	if got := pwshQuote(`C:\it's`); got != `'C:\it''s'` {
		t.Errorf("pwsh: %s", got)
	}
}

func TestNonces(t *testing.T) {
	a, b := NewNonce(), NewNonce()
	if len(a) != 32 || a == b || strings.Trim(a, "0123456789abcdef") != "" {
		t.Fatalf("nonces %q %q", a, b)
	}
}
