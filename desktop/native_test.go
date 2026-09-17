package main

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

// The delay before another start doubles and then stops doubling: fast enough that a transient
// failure costs nothing, slow enough that a process which cannot start at all does not spin.
func TestTheRestartDelayDoublesAndIsCapped(t *testing.T) {
	want := []time.Duration{time.Second, time.Second, 2 * time.Second, 4 * time.Second, 8 * time.Second}
	for i, expected := range want {
		if got := backoffFor(i); got != expected {
			t.Errorf("backoffFor(%d) = %s, want %s", i, got, expected)
		}
	}
	if got := backoffFor(20); got != 60*time.Second {
		t.Fatalf("the delay is not capped: backoffFor(20) = %s", got)
	}
}

// The log is rolled before a start, never while it is being written: the launcher runs for weeks and
// a supervisor log that grows without a limit is a disk that fills without a warning.
func TestTheLogIsRolledOverOnceItIsLarge(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "supervisor.log")
	if err := os.WriteFile(path, []byte("small"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := rotateLog(path, 1024, 3); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(path + ".1"); err == nil {
		t.Fatal("a small log was rolled over")
	}
	for round := 1; round <= 4; round++ {
		if err := os.WriteFile(path, []byte(strings.Repeat("x", 2048)), 0o600); err != nil {
			t.Fatal(err)
		}
		if err := rotateLog(path, 1024, 3); err != nil {
			t.Fatal(err)
		}
		if _, err := os.Stat(path); err == nil {
			t.Fatal("the log was not moved out of the way")
		}
	}
	if _, err := os.Stat(path + ".3"); err != nil {
		t.Fatalf("the kept copies are missing: %v", err)
	}
	if _, err := os.Stat(path + ".4"); err == nil {
		t.Fatal("more copies are kept than were asked for")
	}
}

// A child that dies is started again, and a child that is stopped is not. Both with a shell in the
// place of the supervisor: what is being tested is the loop, not what it runs.
func TestAChildThatDiesIsStartedAgain(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("the fake child is a POSIX shell")
	}
	dir := t.TempDir()
	counter := filepath.Join(dir, "starts")
	pr := &Process{
		Name:    "fake supervisor",
		Argv:    []string{"sh", "-c", fmt.Sprintf("echo . >> %q; exit 1", counter)},
		LogPath: filepath.Join(dir, "child.log"),
		Log:     func(string, ...any) {},
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	pr.Start(ctx)
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) && pr.Starts() < 2 {
		time.Sleep(50 * time.Millisecond)
	}
	if pr.Starts() < 2 {
		t.Fatalf("a child that exited was started %d time(s), not again", pr.Starts())
	}
	pr.Stop(ctx)
	after := pr.Starts()
	time.Sleep(2500 * time.Millisecond)
	if pr.Starts() != after {
		t.Fatalf("a stopped child was started again (%d → %d)", after, pr.Starts())
	}
	if _, err := os.Stat(pr.LogPath); err != nil {
		t.Fatalf("the child's output was not kept: %v", err)
	}
}

// Stopping ends the whole tree: the supervisor starts the bot in a session of its own, so
// signalling only the process the launcher knows about would leave the agent running behind it.
func TestStoppingEndsWhatTheChildStarted(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("the fake child is a POSIX shell")
	}
	dir := t.TempDir()
	marker := filepath.Join(dir, "grandchild-alive")
	pr := &Process{
		Name: "fake supervisor",
		// A child that starts a child of its own and then waits, as the supervisor does.
		Argv:    []string{"sh", "-c", fmt.Sprintf("(while true; do touch %q; sleep 0.2; done) & wait", marker)},
		LogPath: filepath.Join(dir, "child.log"),
		Log:     func(string, ...any) {},
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	pr.Start(ctx)
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(marker); err == nil {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if _, err := os.Stat(marker); err != nil {
		t.Skip("the fake child never got going on this machine")
	}
	pr.Stop(ctx)
	if err := os.Remove(marker); err != nil {
		t.Fatal(err)
	}
	time.Sleep(time.Second)
	if _, err := os.Stat(marker); err == nil {
		t.Fatal("something the stopped child started is still running")
	}
}

// The supervisor reads its whole world from the environment, which is what lets one supervisor serve
// both modes. These are the names that have to be in it, and the values that have to be this
// installation's own rather than a container's.
func TestTheSupervisorIsGivenThisInstallationsPaths(t *testing.T) {
	data := t.TempDir()
	paths, err := NewPaths(data)
	if err != nil {
		t.Fatal(err)
	}
	env := envMap(supervisorEnv(paths, nil, map[string]string{"API_PORT": "19985", "SERVICES_PORT_RANGE": "19170-19179", "USD_PER_DAY": "20"}))
	for key, want := range map[string]string{
		"DAEDALUS_NATIVE":        "1",
		"DAEDALUS_BOT_REPO":      paths.Bot,
		"DAEDALUS_CORE_REPO":     paths.Core,
		"DAEDALUS_STATE":         paths.State,
		"DAEDALUS_WORKSPACES":    paths.Workspaces,
		"UV_PROJECT_ENVIRONMENT": paths.RuntimeVenv,
		"SERVICES_PUBLIC_HOST":   "127.0.0.1",
		"API_PORT":               "19985",
		"SERVICES_PORT_RANGE":    "19170-19179",
	} {
		if env[key] != want {
			t.Errorf("%s = %q, want %q", key, env[key], want)
		}
	}
	if !strings.Contains(env["DAEDALUS_BOT_CMD"], paths.RuntimeVenv) {
		t.Errorf("the bot is not started out of the runtime's environment: %q", env["DAEDALUS_BOT_CMD"])
	}
	if strings.Contains(strings.Join(supervisorEnv(paths, nil, nil), " "), "/srv/") {
		t.Error("a container path reached a native installation's environment")
	}
	if runtime.GOOS == "windows" {
		if !strings.HasPrefix(env["DAEDALUS_SUPERVISOR_TCP"], "127.0.0.1:") {
			t.Errorf("Windows was not given a loopback port: %q", env["DAEDALUS_SUPERVISOR_TCP"])
		}
		if env["DAEDALUS_SUPERVISOR_SOCKET"] != "" {
			t.Error("Windows was given a unix socket path as well as a port")
		}
	} else {
		if env["DAEDALUS_SUPERVISOR_SOCKET"] != filepath.Join(paths.State, "supervisor.sock") {
			t.Errorf("the socket is not in the state directory: %q", env["DAEDALUS_SUPERVISOR_SOCKET"])
		}
		if env["DAEDALUS_SUPERVISOR_TCP"] != "" {
			t.Error("a loopback port was opened where a socket file will do")
		}
	}
}

// The port the launcher hands out is the one in the env file when that is a port, and the pinned
// default otherwise: a process told to listen on nonsense listens nowhere and says nothing.
func TestAPortIsOnlyTakenFromTheFileWhenItIsAPort(t *testing.T) {
	for _, bad := range []string{"", "  ", "0", "-1", "70000", "3201abc", "http://x"} {
		if got := parsePort(bad, "3201"); got != "3201" {
			t.Errorf("parsePort(%q) = %q, want the default", bad, got)
		}
	}
	if got := parsePort(" 19160 ", "3201"); got != "19160" {
		t.Errorf("parsePort of a real port = %q", got)
	}
}

// The provider keys reach the key proxy and nothing else. This is the one part of the container's
// isolation that survives without a container, so it is asserted rather than assumed.
func TestTheKeysGoToTheKeyProxyAndNotToTheAgent(t *testing.T) {
	paths, err := NewPaths(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	keys := map[string]string{"DEEPSEEK_API_KEY": "sk-secret", "OPENROUTER_API_KEY": "or-secret"}
	settings := map[string]string{"API_PORT": "19985", "KEYPROXY_PORT": "19160"}
	proxy := envMap(keyproxyEnv(paths, nil, keys, settings, "/home/someone"))
	if proxy["DEEPSEEK_API_KEY"] != "sk-secret" {
		t.Error("the key proxy was not given the key it exists to hold")
	}
	if proxy["KEYPROXY_PORT"] != "19160" {
		t.Errorf("the key proxy is on %q, not the port the file names", proxy["KEYPROXY_PORT"])
	}
	agent := envMap(supervisorEnv(paths, nil, settings))
	for name := range keys {
		if _, found := agent[name]; found {
			t.Errorf("%s reached the agent's environment", name)
		}
	}
	if agent["KEYPROXY_BASE_URL"] != "http://127.0.0.1:19160" {
		t.Errorf("the agent was pointed at %q", agent["KEYPROXY_BASE_URL"])
	}
}

// The runtime's own binaries come first on PATH: the pinned ripgrep is the one Search finds, not
// whatever happens to be installed on the machine.
func TestTheRuntimesBinariesComeFirstOnPath(t *testing.T) {
	paths, err := NewPaths(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	n := NewNative(paths, func(string, ...any) {})
	dirs := strings.Split(n.searchPath(), string(os.PathListSeparator))
	if dirs[0] != paths.RuntimeBin {
		t.Fatalf("PATH starts with %q, not the runtime's binaries", dirs[0])
	}
	if !strings.Contains(n.searchPath(), filepath.Dir(venvPython(paths))) {
		t.Fatal("the environment's own scripts are not on PATH")
	}
}

// A path with a space in it is a Windows installation more often than not, and the bot command is a
// command line rather than an argument list.
func TestASpaceInThePathIsQuotedInTheBotCommand(t *testing.T) {
	if got := quoteArgv(`C:\Program Files\Daedalus\python.exe`); !strings.HasPrefix(got, `"`) {
		t.Fatalf("a path with a space came out unquoted: %s", got)
	}
	if got := quoteArgv("/home/x/venv/bin/python"); strings.Contains(got, `"`) {
		t.Fatalf("a path with no space was quoted: %s", got)
	}
}

// The launcher runs commands of the installation's own — a pairing link, a restart request — and
// those read their settings under the names the agent uses, not the ones the supervisor is given.
// Both spellings have to be there or the command reads the defaults of a container that is not here.
func TestACommandRunForTheInstallationSeesBothSpellings(t *testing.T) {
	paths, err := NewPaths(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	env := envMap(botEnv(paths, supervisorEnv(paths, nil, map[string]string{"API_PORT": "19985"})))
	for key, want := range map[string]string{
		"STATE_DIR":      paths.State,
		"WORKSPACES_DIR": paths.Workspaces,
		"BOT_REPO_DIR":   paths.Bot,
		"CORE_REPO_DIR":  paths.Core,
	} {
		if env[key] != want {
			t.Errorf("%s = %q, want %q", key, env[key], want)
		}
	}
	if runtime.GOOS == "windows" {
		if env["SUPERVISOR_TCP"] == "" {
			t.Error("the command has no supervisor to talk to")
		}
	} else if env["SUPERVISOR_SOCKET"] != filepath.Join(paths.State, "supervisor.sock") {
		t.Errorf("the command was pointed at %q", env["SUPERVISOR_SOCKET"])
	}
}

func envMap(env []string) map[string]string {
	out := map[string]string{}
	for _, kv := range env {
		key, value, _ := strings.Cut(kv, "=")
		out[key] = value
	}
	return out
}

// The one value a command is run for is on the first line that carries anything; what comes before
// it is the environment clearing its throat. (It once called itself, which no test had asked it to
// do and which took the launcher down with a stack overflow the moment a token was read.)
func TestTheFirstLineThatCarriesSomethingIsTheAnswer(t *testing.T) {
	cases := map[string]string{
		"":                                    "",
		"\n\n":                                "",
		"token-abc\n":                         "token-abc",
		"\n  warning: something\ntoken-abc\n": "warning: something",
		"  token-abc  ":                       "token-abc",
	}
	for out, want := range cases {
		if got := firstLine(out); got != want {
			t.Errorf("firstLine(%q) = %q, want %q", out, got, want)
		}
	}
}
