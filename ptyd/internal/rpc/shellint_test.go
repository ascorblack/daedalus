//go:build unix

package rpc_test

import (
	"bytes"
	"encoding/base64"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/emulator/production"
	"github.com/ascorblack/daedalus/ptyd/internal/term"
	"github.com/ascorblack/daedalus/ptyd/internal/wire"
)

// These tests run real shells with their integration loaded, as the daemon launches them, and drive
// them as a person would: type a line, wait for the command to end. A shell that is not installed is
// skipped with a message; the gate's image carries bash, zsh and fish.

// shellSession is one integrated shell under test.
type shellSession struct {
	t    *testing.T
	f    *fixture
	id   string
	home string
}

// shellHome is a home directory with the given files, and the listing it starts with.
func shellHome(t *testing.T, files map[string]string) string {
	t.Helper()
	home := t.TempDir()
	for name, text := range files {
		path := filepath.Join(home, name)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(text), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return home
}

// listing is every path under dir, for "nothing was written here".
func listing(t *testing.T, dir string) []string {
	t.Helper()
	var out []string
	err := filepath.WalkDir(dir, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(dir, path)
		out = append(out, rel)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	sort.Strings(out)
	return out
}

func startShell(t *testing.T, f *fixture, id, program string, home string, env map[string]string, wantKind string) *shellSession {
	t.Helper()
	if _, err := exec.LookPath(program); err != nil {
		t.Skipf("%s is not installed here; the gate's image has it", program)
	}
	all := map[string]string{"HOME": home}
	for k, v := range env {
		all[k] = v
	}
	var created struct {
		ShellIntegration string `json:"shell_integration"`
	}
	f.call(t, "terminal.create", map[string]any{"id": id, "shell": map[string]any{"program": program},
		"cwd": f.dir, "env": all, "cols": 120, "rows": 40}, &created)
	if created.ShellIntegration != wantKind {
		t.Fatalf("shell_integration %q, want %q", created.ShellIntegration, wantKind)
	}
	// The first prompt, before typing: PSReadLine loses a line typed while it starts. Quiet output
	// alone does not say the prompt is there: a PowerShell starting cold on a busy machine prints
	// nothing for longer than any idle time, and a line typed into that silence reached the terminal
	// before PSReadLine did, which took its Enter for a line feed and never ran it. So the prompt's
	// own mark first, then the idle time in which the line editor takes the terminal over.
	waitForPromptMark(t, f, id)
	var idle struct {
		Matched string `json:"matched"`
	}
	f.call(t, "terminal.wait_for", map[string]any{"id": id, "idle_ms": 500, "timeout_ms": 15000}, &idle)
	return &shellSession{t: t, f: f, id: id, home: home}
}

// waitForPromptMark waits until the terminal's raw output holds the integration's first prompt mark.
func waitForPromptMark(t *testing.T, f *fixture, id string) {
	t.Helper()
	deadline := time.Now().Add(60 * time.Second)
	for {
		var out output
		f.call(t, "terminal.read_output", map[string]any{"id": id, "since_seq": 0, "max_bytes": 256 << 10}, &out)
		raw, err := base64.StdEncoding.DecodeString(out.DataB64)
		if err != nil {
			t.Fatal(err)
		}
		if bytes.Contains(raw, []byte("\x1b]133;A;")) {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("%s never printed its first prompt: %q", id, raw)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func (s *shellSession) info() term.Info {
	s.t.Helper()
	var info term.Info
	s.f.call(s.t, "terminal.get", map[string]any{"id": s.id}, &info)
	return info
}

// run types line and waits for the command it starts to end.
func (s *shellSession) run(line string) term.CommandRecord {
	s.t.Helper()
	head := s.info().OutputSeq
	s.type_(line)
	var r struct {
		Matched string              `json:"matched"`
		Command *term.CommandRecord `json:"command"`
	}
	s.f.call(s.t, "terminal.wait_for", map[string]any{"id": s.id, "command_done": true, "since_seq": head,
		"timeout_ms": 15000}, &r)
	if r.Matched != "command_done" || r.Command == nil {
		out := s.f.waitOutput(s.t, s.id, "")
		s.t.Fatalf("%q: wait ended with %q; output so far:\n%s", line, r.Matched, out)
	}
	return *r.Command
}

func (s *shellSession) type_(line string) {
	s.t.Helper()
	s.f.call(s.t, "terminal.write", map[string]any{"id": s.id, "text": line + "\r", "wait": "none",
		"origin": map[string]any{"kind": "agent", "actor": "test"}}, nil)
}

func (s *shellSession) commands(withOutput bool) []term.CommandRecord {
	s.t.Helper()
	var r struct {
		Commands []term.CommandRecord `json:"commands"`
	}
	s.f.call(s.t, "terminal.commands", map[string]any{"id": s.id, "last": 500, "with_output": withOutput}, &r)
	return r.Commands
}

// eventually polls cond for up to five seconds.
func eventually(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("never: %s", what)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// exercise is the scripted session every shell goes through: plain commands and their status, a
// directory change, the user's alias, forged marks, a nested shell, a long command and its busy
// flag, and the nonce kept from the programs the shell starts.
func (s *shellSession) exercise(nested, innerEcho string) {
	t := s.t
	check := func(r term.CommandRecord, command string, code int) {
		t.Helper()
		if r.Command != command || r.ExitCode == nil || *r.ExitCode != code {
			t.Fatalf("record %+v, want %q exit %d", r, command, code)
		}
	}
	check(s.run("true"), "true", 0)
	check(s.run("false"), "false", 1)

	r := s.run("cd /tmp")
	check(r, "cd /tmp", 0)
	eventually(t, "the directory reported as /tmp", func() bool { return s.info().Cwd == "/tmp" })
	if r.Cwd != s.f.dir {
		t.Fatalf("a command's directory is the one it started in: %q", r.Cwd)
	}
	after := s.run("true")
	if after.Cwd != "/tmp" {
		t.Fatalf("the next command's directory: %q", after.Cwd)
	}

	check(s.run("ll"), "ll", 0) // the user's alias, from their own startup file

	// Marks without this launch's nonce, and with a wrong one, change nothing: the command that
	// printed them is one record, and its status is its own.
	before := len(s.commands(false))
	forged := s.run(`printf '\e]133;D;7\a\e]133;C\a\e]133;D;0;k=0123456789abcdef0123456789abcdef\a'; false`)
	if forged.ExitCode == nil || *forged.ExitCode != 1 {
		t.Fatalf("forged marks decided the status: %+v", forged)
	}
	if n := len(s.commands(false)); n != before+1 {
		t.Fatalf("forged marks made records: %d then %d", before, n)
	}
	echoed := `echo "$(printf '\e]133;D;0\a')"`
	check(s.run(echoed), echoed, 0)

	// A nested shell prints no marks of its own: its commands belong to the one that started it.
	before = len(s.commands(false))
	head := s.info().OutputSeq
	s.type_(nested)
	var w struct {
		Matched string `json:"matched"`
	}
	s.f.call(t, "terminal.wait_for", map[string]any{"id": s.id, "idle_ms": 500, "timeout_ms": 15000}, &w)
	s.type_(innerEcho)
	s.f.call(t, "terminal.wait_for", map[string]any{"id": s.id, "regex": "inner-42", "scope": "output",
		"since_seq": head, "timeout_ms": 15000}, &w)
	if w.Matched != "regex" {
		t.Fatalf("the nested shell never answered: %q", w.Matched)
	}
	inner := s.run("exit 5")
	if inner.ExitCode == nil || *inner.ExitCode != 5 || !strings.HasPrefix(inner.Command, strings.Fields(nested)[0]) {
		t.Fatalf("the nested shell's record: %+v", inner)
	}
	if n := len(s.commands(false)); n != before+1 {
		t.Fatalf("the nested shell's commands made records: %d then %d", before, n)
	}

	// Busy while a command runs, idle at the prompt.
	head = s.info().OutputSeq
	s.type_("sleep 1")
	eventually(t, "busy while sleep runs", func() bool { return s.info().Busy })
	var done struct {
		Matched string `json:"matched"`
	}
	s.f.call(t, "terminal.wait_for", map[string]any{"id": s.id, "command_done": true, "since_seq": head,
		"timeout_ms": 15000}, &done)
	if done.Matched != "command_done" {
		t.Fatalf("sleep: %q", done.Matched)
	}
	eventually(t, "idle at the prompt", func() bool { return !s.info().Busy })

	// The nonce stays out of what the shell starts.
	check(s.run("env | grep -c DAEDALUS_SI_"), "env | grep -c DAEDALUS_SI_", 1)

	recs := s.commands(true)
	for i := 1; i < len(recs); i++ {
		if recs[i].N != recs[i-1].N+1 {
			t.Fatalf("records out of order: %+v", recs)
		}
	}
	if !production.HasScreen {
		return
	}
	// The rows, and the output read back from them.
	byCommand := map[string]term.CommandRecord{}
	for _, r := range recs {
		byCommand[r.Command] = r
	}
	ll := byCommand["ll"]
	if ll.Output == nil || *ll.Output != "LL-ALIAS" {
		t.Fatalf("the output of ll: %+v", ll)
	}
	if ll.PromptRow == nil || *ll.PromptRow >= ll.OutputRow || ll.EndRow == nil || *ll.EndRow != ll.OutputRow+1 {
		t.Fatalf("the rows of ll: prompt %v output %d end %v", ll.PromptRow, ll.OutputRow, ll.EndRow)
	}
	if grep := byCommand["env | grep -c DAEDALUS_SI_"]; grep.Output == nil || *grep.Output != "0" {
		t.Fatalf("the nonce reached a program: %+v", grep)
	}
}

func (s *shellSession) end() {
	s.t.Helper()
	s.f.call(s.t, "terminal.kill", map[string]any{"id": s.id, "grace_ms": 1000}, nil)
}

func TestBashIntegration(t *testing.T) {
	f := startWith(t, production.Factory)
	// A user's prompt command in the manner of starship: it reads $? and rebuilds PS1 every prompt.
	home := shellHome(t, map[string]string{".bashrc": `HISTFILE=
alias ll='echo LL-ALIAS'
user_prompt() { local status=$?; PS1="[$status]\$ "; USER_PROMPT_RAN=$((USER_PROMPT_RAN+1)); }
PROMPT_COMMAND=user_prompt
`})
	before := listing(t, home)
	s := startShell(t, f, "bash1", "bash", home, nil, "bash")
	s.exercise("bash", "echo inner-$((6*7))")

	// The user's prompt command still ran, and saw the status of the command before it.
	s.run("false")
	head := s.info().OutputSeq
	s.type_(`echo "ran=$USER_PROMPT_RAN ps1=$PS1"`)
	var w struct {
		Matched string `json:"matched"`
		Match   string `json:"match"`
	}
	f.call(t, "terminal.wait_for", map[string]any{"id": "bash1", "regex": `ran=[1-9][0-9]* ps1=\[1\]`,
		"scope": "output", "since_seq": head, "timeout_ms": 10000}, &w)
	if w.Matched != "regex" {
		t.Fatalf("the user's prompt command: %q\n%s", w.Matched, f.waitOutput(t, "bash1", ""))
	}
	s.end()
	if after := listing(t, home); strings.Join(after, "\n") != strings.Join(before, "\n") {
		t.Fatalf("the home directory changed:\n%v\n%v", before, after)
	}
}

// Older bash has no PS0 and gets the DEBUG trap instead; the user's own trap keeps running.
func TestBashIntegrationThroughTheDebugTrap(t *testing.T) {
	f := startWith(t, production.Factory)
	home := shellHome(t, map[string]string{".bashrc": `HISTFILE=
alias ll='echo LL-ALIAS'
trap 'USER_TRAP=$((USER_TRAP+1))' DEBUG
`})
	s := startShell(t, f, "bash2", "bash", home, map[string]string{"DAEDALUS_SI_BASH_MODE": "debug"}, "bash")
	s.exercise("bash", "echo inner-$((6*7))")
	head := s.info().OutputSeq
	s.type_(`echo "trap=$USER_TRAP"`)
	var w struct {
		Matched string `json:"matched"`
	}
	f.call(t, "terminal.wait_for", map[string]any{"id": "bash2", "regex": `trap=[1-9]`, "scope": "output",
		"since_seq": head, "timeout_ms": 10000}, &w)
	if w.Matched != "regex" {
		t.Fatalf("the user's DEBUG trap stopped: %q", w.Matched)
	}
	s.end()
}

// A login shell reads the login files; a shell asked not to be one reads ~/.bashrc.
func TestBashLoginFiles(t *testing.T) {
	f := startWith(t, production.Factory)
	home := shellHome(t, map[string]string{
		".bash_profile": "HISTFILE=\nFROM=profile\n",
		".bashrc":       "HISTFILE=\nFROM=bashrc\n",
	})
	for _, c := range []struct {
		id    string
		login bool
		want  string
	}{{"login", true, "profile"}, {"plain", false, "bashrc"}} {
		if _, err := exec.LookPath("bash"); err != nil {
			t.Skip("bash is not installed")
		}
		f.call(t, "terminal.create", map[string]any{"id": c.id, "shell": map[string]any{"program": "bash", "login": c.login},
			"cwd": f.dir, "env": map[string]string{"HOME": home}}, nil)
		s := &shellSession{t: t, f: f, id: c.id, home: home}
		head := s.info().OutputSeq
		s.type_("echo from-$FROM")
		var w struct {
			Matched string `json:"matched"`
		}
		f.call(t, "terminal.wait_for", map[string]any{"id": c.id, "regex": "from-" + c.want, "scope": "output",
			"since_seq": head, "timeout_ms": 10000}, &w)
		if w.Matched != "regex" {
			t.Fatalf("%s: %q\n%s", c.id, w.Matched, f.waitOutput(t, c.id, ""))
		}
		if r := s.run("true"); r.Command != "true" {
			t.Fatalf("%s: %+v", c.id, r)
		}
		s.end()
	}
}

func TestZshIntegration(t *testing.T) {
	f := startWith(t, production.Factory)
	home := shellHome(t, map[string]string{
		".zshenv": "export FROM_ZSHENV=1\n",
		".zshrc": `typeset -A USER_MAP
USER_MAP[kept]=yes
alias ll='echo LL-ALIAS'
user_precmd() { USER_PRECMD=$? }
precmd_functions+=(user_precmd)
PS1='%# '
`,
		".zlogin": "FROM_ZLOGIN=1\n",
	})
	before := listing(t, home)
	s := startShell(t, f, "zsh1", "zsh", home, nil, "zsh")
	s.exercise("zsh", "echo inner-$((6*7))")

	// The user's files ran as their own (a typeset is not a function's local), their precmd hook
	// still sees the status, and ZDOTDIR is theirs again (here: unset, as it started).
	s.run("false")
	head := s.info().OutputSeq
	s.type_(`echo "env=$FROM_ZSHENV login=$FROM_ZLOGIN map=$USER_MAP[kept] pre=$USER_PRECMD zdotdir=${ZDOTDIR-unset}"`)
	var w struct {
		Matched string `json:"matched"`
	}
	f.call(t, "terminal.wait_for", map[string]any{"id": "zsh1", "regex": `env=1 login=1 map=yes pre=1 zdotdir=unset`,
		"scope": "output", "since_seq": head, "timeout_ms": 10000}, &w)
	if w.Matched != "regex" {
		t.Fatalf("the user's zsh files: %q\n%s", w.Matched, f.waitOutput(t, "zsh1", ""))
	}
	s.end()
	if after := listing(t, home); strings.Join(after, "\n") != strings.Join(before, "\n") {
		t.Fatalf("the home directory changed:\n%v\n%v", before, after)
	}
}

// A user whose zsh files live in their own ZDOTDIR gets that directory back.
func TestZshKeepsTheUsersZdotdir(t *testing.T) {
	f := startWith(t, production.Factory)
	home := shellHome(t, map[string]string{"zdot/.zshrc": "FROM_ZDOT=1\n"})
	s := startShell(t, f, "zsh2", "zsh", home, map[string]string{"ZDOTDIR": filepath.Join(home, "zdot")}, "zsh")
	head := s.info().OutputSeq
	s.type_(`echo "zdot=$FROM_ZDOT dir=$ZDOTDIR"`)
	var w struct {
		Matched string `json:"matched"`
	}
	f.call(t, "terminal.wait_for", map[string]any{"id": "zsh2", "regex": "zdot=1 dir=" + filepath.Join(home, "zdot"),
		"scope": "output", "since_seq": head, "timeout_ms": 10000}, &w)
	if w.Matched != "regex" {
		t.Fatalf("the user's ZDOTDIR: %q\n%s", w.Matched, f.waitOutput(t, "zsh2", ""))
	}
	if r := s.run("true"); r.Command != "true" {
		t.Fatalf("%+v", r)
	}
	s.end()
}

func TestFishIntegration(t *testing.T) {
	f := startWith(t, production.Factory)
	// fish keeps its history and variables under the XDG directories; they are pointed outside the
	// home here, so the check that the integration writes nothing there is about the integration.
	xdg := t.TempDir()
	home := shellHome(t, map[string]string{})
	config := filepath.Join(xdg, "config")
	if err := os.MkdirAll(filepath.Join(config, "fish"), 0o755); err != nil {
		t.Fatal(err)
	}
	userConfig := `alias ll 'echo LL-ALIAS'
function fish_prompt
    set -l last $status
    echo -n "[$last]> "
end
`
	if err := os.WriteFile(filepath.Join(config, "fish", "config.fish"), []byte(userConfig), 0o644); err != nil {
		t.Fatal(err)
	}
	before := listing(t, home)
	s := startShell(t, f, "fish1", "fish", home, map[string]string{"XDG_CONFIG_HOME": config,
		"XDG_DATA_HOME": filepath.Join(xdg, "data"), "XDG_CACHE_HOME": filepath.Join(xdg, "cache")}, "fish")
	s.exercise("fish", "echo inner-(math 6 x 7)")

	// The user's prompt is kept and still sees the status of the command before it.
	s.run("false")
	var w struct {
		Matched string `json:"matched"`
	}
	f.call(t, "terminal.wait_for", map[string]any{"id": "fish1", "regex": `\[1\]> `, "scope": "screen",
		"timeout_ms": 10000}, &w)
	if production.HasScreen && w.Matched != "regex" {
		t.Fatalf("the user's prompt: %q\n%s", w.Matched, f.waitOutput(t, "fish1", ""))
	}
	s.end()
	if after := listing(t, home); strings.Join(after, "\n") != strings.Join(before, "\n") {
		t.Fatalf("the home directory changed:\n%v\n%v", before, after)
	}
}

// PowerShell is the Windows host's shell; pwsh runs the same script elsewhere. The gate's image has
// no pwsh, so this runs where one is installed.
func TestPwshIntegration(t *testing.T) {
	f := startWith(t, production.Factory)
	home := shellHome(t, map[string]string{
		".config/powershell/Microsoft.PowerShell_profile.ps1": "function ll { 'LL-ALIAS' }\n",
	})
	s := startShell(t, f, "pwsh1", "pwsh", home, map[string]string{"POWERSHELL_TELEMETRY_OPTOUT": "1"}, "pwsh")
	s.exercise("pwsh -NoLogo", "echo inner-$(6*7)")
	s.end()
}

// A shell started without integration, and a program that is no shell, report no commands; asking
// for them, or waiting for one to end, says so instead of waiting forever.
func TestCommandsNeedIntegration(t *testing.T) {
	f := start(t)
	f.call(t, "terminal.create", map[string]any{"id": "plain", "shell": map[string]any{"program": "sh", "integration": false}}, nil)
	f.call(t, "terminal.create", map[string]any{"id": "cat", "argv": []string{"cat"}}, nil)
	for _, id := range []string{"plain", "cat"} {
		if we := f.callErr("terminal.commands", map[string]any{"id": id}); we == nil || we.Code != wire.CodeUnsupported {
			t.Fatalf("%s commands: %v", id, we)
		}
		if we := f.callErr("terminal.wait_for", map[string]any{"id": id, "command_done": true, "timeout_ms": 1000}); we == nil || we.Code != wire.CodeUnsupported {
			t.Fatalf("%s wait: %v", id, we)
		}
	}
	var info struct {
		Capabilities struct {
			ShellIntegration []string `json:"shell_integration"`
		} `json:"capabilities"`
	}
	f.call(t, "daemon.info", nil, &info)
	if strings.Join(info.Capabilities.ShellIntegration, ",") != "bash,zsh,fish,pwsh" {
		t.Fatalf("capabilities: %v", info.Capabilities.ShellIntegration)
	}
}

// A program that knows its terminal's nonce may report commands itself, as a shell would.
func TestAProgramWithTheNonceReportsCommands(t *testing.T) {
	f := start(t)
	f.call(t, "events.subscribe", map[string]any{"after_seq": 0}, nil)
	script := `printf '\033]633;E;make test;k=%s\007\033]133;C;k=%s\007ok\n\033]133;D;2;k=%s\007' "$DAEDALUS_SI_NONCE" "$DAEDALUS_SI_NONCE" "$DAEDALUS_SI_NONCE"; sleep 5`
	f.call(t, "terminal.create", map[string]any{"id": "p", "argv": []string{"sh", "-c", script}}, nil)
	ev, err := f.client.WaitEvent("terminal.command", "p", 10*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(ev.Data), `"exit_code":2`) || !strings.Contains(string(ev.Data), `"command":"make test"`) {
		t.Fatalf("event %s", ev.Data)
	}
	var r struct {
		Commands []term.CommandRecord `json:"commands"`
	}
	f.call(t, "terminal.commands", map[string]any{"id": "p"}, &r)
	if len(r.Commands) != 1 || r.Commands[0].Command != "make test" || *r.Commands[0].ExitCode != 2 {
		t.Fatalf("commands %+v", r.Commands)
	}
	for _, bad := range []map[string]any{{"id": "p", "last": 501}, {"id": "p", "output_max": -1}, {"id": "p", "extra": 1}} {
		if we := f.callErr("terminal.commands", bad); we == nil || we.Code != wire.CodeInvalidParams {
			t.Fatalf("%v: %v", bad, we)
		}
	}
}
