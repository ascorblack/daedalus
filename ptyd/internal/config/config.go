// Package config reads the daemon's flags and its optional JSON file.
package config

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// Config is everything `ptyd serve` runs with.
type Config struct {
	Env         string // the environment's name, echoed to clients: "container" or "host"
	RunDir      string // endpoint, token and socket
	StateDir    string // logs, the agent-write journal, terminal logs
	Listen      string // "unix", or "tcp:127.0.0.1:<port>"
	Home        string
	Shell       string // resolved login shell
	HooksListen string
	ConfigFile  string
	LogFile     string
	LogLevel    string

	Limits Limits
}

// Limits are the numbers a deployment may tune from the JSON file.
type Limits struct {
	MaxTerminals int           `json:"max_terminals"`
	RingBytes    int           `json:"ring_bytes"`
	InputIdle    time.Duration `json:"-"`
	KillGrace    time.Duration `json:"-"`
}

// file is the JSON file's shape. Unknown keys are refused: a misspelt limit that silently keeps its
// default is how a deployment ends up not doing what its operator wrote.
type file struct {
	Limits struct {
		MaxTerminals *int   `json:"max_terminals"`
		RingBytes    *int   `json:"ring_bytes"`
		InputIdleMs  *int64 `json:"input_idle_ms"`
		KillGraceMs  *int64 `json:"kill_grace_ms"`
	} `json:"limits"`
}

// Parse reads the arguments of `serve`.
func Parse(args []string) (*Config, error) {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	c := &Config{}
	fs.StringVar(&c.Env, "env", "", "environment name (container or host)")
	fs.StringVar(&c.RunDir, "run-dir", "", "run directory: endpoint, token and socket")
	fs.StringVar(&c.StateDir, "state-dir", "", "state directory: logs and journals")
	fs.StringVar(&c.Listen, "listen", "unix", `"unix" or "tcp:127.0.0.1:<port>"`)
	fs.StringVar(&c.Home, "home", "", "home directory of spawned shells (default $HOME)")
	fs.StringVar(&c.Shell, "shell", "", "login shell (default $SHELL, then the passwd entry)")
	fs.StringVar(&c.HooksListen, "hooks-listen", "127.0.0.1:0", "loopback address of the hook listener")
	fs.StringVar(&c.ConfigFile, "config", "", "JSON file with limits")
	fs.StringVar(&c.LogFile, "log-file", "", "log file (default stderr)")
	fs.StringVar(&c.LogLevel, "log-level", "info", "debug, info, warn or error")
	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	if fs.NArg() > 0 {
		return nil, fmt.Errorf("unexpected argument %q", fs.Arg(0))
	}
	if c.Env == "" {
		return nil, errors.New("--env is required")
	}
	if c.RunDir == "" || c.StateDir == "" {
		return nil, errors.New("--run-dir and --state-dir are required")
	}
	var err error
	if c.RunDir, err = filepath.Abs(c.RunDir); err != nil {
		return nil, err
	}
	if c.StateDir, err = filepath.Abs(c.StateDir); err != nil {
		return nil, err
	}
	if c.Listen != "unix" && !strings.HasPrefix(c.Listen, "tcp:127.0.0.1:") {
		// Only loopback: the token is the whole of the authentication, and it is not meant to cross a
		// network.
		return nil, fmt.Errorf("--listen must be unix or tcp:127.0.0.1:<port>, not %q", c.Listen)
	}
	if c.Home == "" {
		c.Home, _ = os.UserHomeDir()
	}
	if c.Home == "" {
		c.Home = "/"
	}
	c.Limits = Limits{
		MaxTerminals: DefaultMaxTerminals,
		RingBytes:    DefaultRingBytes,
		InputIdle:    DefaultInputIdle,
		KillGrace:    DefaultKillGrace,
	}
	if c.ConfigFile != "" {
		if err := c.load(c.ConfigFile); err != nil {
			return nil, fmt.Errorf("config %s: %w", c.ConfigFile, err)
		}
	}
	if c.Shell == "" {
		c.Shell = DefaultShell()
	}
	return c, nil
}

func (c *Config) load(path string) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	var f file
	if err := dec.Decode(&f); err != nil {
		return err
	}
	if v := f.Limits.MaxTerminals; v != nil {
		if *v < 1 || *v > 10000 {
			return fmt.Errorf("limits.max_terminals %d is outside 1..10000", *v)
		}
		c.Limits.MaxTerminals = *v
	}
	if v := f.Limits.RingBytes; v != nil {
		if *v < MinRingBytes || *v > MaxRingBytes {
			return fmt.Errorf("limits.ring_bytes %d is outside %d..%d", *v, MinRingBytes, MaxRingBytes)
		}
		c.Limits.RingBytes = *v
	}
	if v := f.Limits.InputIdleMs; v != nil {
		if *v < 0 || *v > 600000 {
			return fmt.Errorf("limits.input_idle_ms %d is outside 0..600000", *v)
		}
		c.Limits.InputIdle = time.Duration(*v) * time.Millisecond
	}
	if v := f.Limits.KillGraceMs; v != nil {
		if *v < 0 || time.Duration(*v)*time.Millisecond > MaxKillGrace {
			return fmt.Errorf("limits.kill_grace_ms %d is outside 0..%d", *v, MaxKillGrace.Milliseconds())
		}
		c.Limits.KillGrace = time.Duration(*v) * time.Millisecond
	}
	return nil
}

// DefaultShell is `$SHELL`, then the user's passwd entry, then bash, then sh. `$SHELL` comes first
// because a service manager may have set it deliberately; the passwd entry is what a login would use
// when nothing did, which is the case inside a container started with a bare environment.
func DefaultShell() string {
	if runtime.GOOS == "windows" {
		return "powershell.exe"
	}
	if s := os.Getenv("SHELL"); s != "" && executable(s) {
		return s
	}
	if s := passwdShell(os.Getuid()); s != "" && executable(s) {
		return s
	}
	for _, s := range []string{"/bin/bash", "/usr/bin/bash", "/bin/sh"} {
		if executable(s) {
			return s
		}
	}
	return "/bin/sh"
}

func passwdShell(uid int) string {
	f, err := os.Open("/etc/passwd")
	if err != nil {
		return ""
	}
	defer f.Close()
	want := fmt.Sprint(uid)
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		fields := strings.Split(sc.Text(), ":")
		if len(fields) >= 7 && fields[2] == want {
			return fields[6]
		}
	}
	return ""
}

func executable(path string) bool {
	st, err := os.Stat(path)
	return err == nil && !st.IsDir() && st.Mode()&0o111 != 0
}

// Shells lists the login shells installed here, from /etc/shells, for `daemon.info`.
func Shells() []string {
	data, err := os.ReadFile("/etc/shells")
	if err != nil {
		return nil
	}
	seen := map[string]bool{}
	var out []string
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") || !executable(line) {
			continue
		}
		// /bin and /usr/bin are one directory on merged-usr systems; list each shell once.
		base := filepath.Base(line)
		if seen[base] {
			continue
		}
		seen[base] = true
		out = append(out, line)
	}
	return out
}
