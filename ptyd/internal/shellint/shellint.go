// Package shellint is shell integration: the scripts that make bash, zsh, fish and PowerShell mark
// their prompts and commands, and the rewriting of a shell's launch that loads them.
//
// The scripts are compiled into the daemon and written into its state directory at start, because
// the daemon runs where the Daedalus checkout is not (a server's host bridge, a desktop). They are
// loaded by the launch alone — an init file, a ZDOTDIR, an init command — and never by editing the
// user's own startup files, which each script reads first, as the shell would have.
//
// Every mark the scripts print carries k=<nonce>, where the nonce is random per launch and reaches
// the shell in DAEDALUS_SI_NONCE, which the script removes from its environment straight away. The
// daemon believes only marks that carry it (see term), so output replayed from a recorded session,
// or printed by a nested shell over ssh, cannot report a command that never ran.
package shellint

import (
	"crypto/rand"
	"embed"
	"encoding/hex"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
)

//go:embed all:files
var files embed.FS

// Environment variables the daemon passes to an integrated shell. The scripts remove each one once
// read, so a program started from the shell inherits none of them.
const (
	NonceEnv       = "DAEDALUS_SI_NONCE"
	LoginEnv       = "DAEDALUS_SHELL_LOGIN"  // bash: read a login shell's files
	UserZdotdirEnv = "DAEDALUS_USER_ZDOTDIR" // zsh: where the user's own files are
)

// Kinds of shell this package integrates with.
const (
	Bash = "bash"
	Zsh  = "zsh"
	Fish = "fish"
	Pwsh = "pwsh"
)

// Kinds lists them, in the order `daemon.info` reports them.
var Kinds = []string{Bash, Zsh, Fish, Pwsh}

// KindsOn is what a daemon on goos can integrate with: on Windows only PowerShell, the host shell
// there, since the other shells' scripts assume a Unix underneath them.
func KindsOn(goos string) []string {
	if goos == "windows" {
		return []string{Pwsh}
	}
	return Kinds
}

// PlainArgs is the argv of a shell started without its integration: a login shell takes -l on Unix.
// A Windows shell has no login flag (PowerShell reads the same profile in every console, and
// Windows PowerShell and cmd.exe would take -l for something else or refuse it).
func PlainArgs(shell string, login bool, goos string) []string {
	if login && goos != "windows" {
		return []string{shell, "-l"}
	}
	return []string{shell}
}

// KindOf names the integration for a shell program, or "" when there is none. A login shell's argv[0]
// may carry the leading dash, and Windows programs their extension.
func KindOf(program string) string {
	// Either separator: a Windows path names the program on any daemon that is asked to run it.
	base := program
	if i := strings.LastIndexAny(base, `/\`); i >= 0 {
		base = base[i+1:]
	}
	base = strings.ToLower(base)
	base = strings.TrimPrefix(base, "-")
	base = strings.TrimSuffix(base, ".exe")
	switch base {
	case "bash":
		return Bash
	case "zsh":
		return Zsh
	case "fish":
		return Fish
	case "pwsh", "powershell":
		return Pwsh
	}
	return ""
}

// NewNonce returns a fresh nonce: 16 random bytes as hex, which no program can guess and no replay
// of another launch's output carries.
func NewNonce() string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		// crypto/rand does not fail on the platforms the daemon runs on; a daemon that cannot draw
		// randomness should not pretend it can verify anything.
		panic(fmt.Sprintf("shell integration: no randomness: %v", err))
	}
	return hex.EncodeToString(b)
}

// Install writes the scripts under dir (the daemon's `<state>/shell`), replacing what an older
// daemon left there, and returns dir. Directories are 0755 and files 0644: the scripts are read by
// the shells the daemon starts, which run as the daemon's own user, and they hold no secret.
func Install(dir string) (string, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}
	err := fs.WalkDir(files, "files", func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel := strings.TrimPrefix(strings.TrimPrefix(path, "files"), "/")
		target := filepath.Join(dir, filepath.FromSlash(rel))
		if d.IsDir() {
			if err := os.MkdirAll(target, 0o755); err != nil {
				return err
			}
			return os.Chmod(target, 0o755)
		}
		data, err := files.ReadFile(path)
		if err != nil {
			return err
		}
		// Written aside and renamed, so a shell starting while a new daemon installs never reads half
		// a script.
		tmp := target + ".tmp"
		if err := os.WriteFile(tmp, data, 0o644); err != nil {
			return err
		}
		if err := os.Chmod(tmp, 0o644); err != nil {
			return err
		}
		return os.Rename(tmp, target)
	})
	if err != nil {
		return "", fmt.Errorf("installing the shell integration in %s: %w", dir, err)
	}
	return dir, nil
}

// Launch is how an integrated shell is started.
type Launch struct {
	Argv []string          // replaces the plain [shell, -l]
	Env  map[string]string // set on top of the terminal's environment
}

// Rewrite returns the launch of shell (a resolved program, of kind) with its integration loaded
// from dir. login says whether the shell was to be a login shell. userZdotdir is the ZDOTDIR the
// terminal's environment already holds, with has telling an empty value from none. nonce goes into
// the environment for the script.
func Rewrite(kind, shell string, login bool, dir, nonce, userZdotdir string, hasZdotdir bool) (Launch, bool) {
	env := map[string]string{NonceEnv: nonce}
	switch kind {
	case Bash:
		// A login bash never reads --init-file, so the script is loaded by an interactive non-login
		// bash and reads the login files itself when asked to.
		if login {
			env[LoginEnv] = "1"
		}
		return Launch{Argv: []string{shell, "--init-file", filepath.Join(dir, "bash", "init.sh"), "-i"}, Env: env}, true
	case Zsh:
		env["ZDOTDIR"] = filepath.Join(dir, "zsh")
		if hasZdotdir {
			env[UserZdotdirEnv] = userZdotdir
		}
		argv := []string{shell}
		if login {
			argv = append(argv, "-l")
		}
		return Launch{Argv: argv, Env: env}, true
	case Fish:
		argv := []string{shell}
		if login {
			argv = append(argv, "--login")
		}
		argv = append(argv, "--init-command", "source "+fishQuote(filepath.Join(dir, "fish", "init.fish")))
		return Launch{Argv: argv, Env: env}, true
	case Pwsh:
		// -Login must come first and exists only in pwsh off Windows; Windows PowerShell has no
		// such thing and every console of it reads the same profile. A failure inside the script
		// is caught, so a blocked script (an execution policy) costs the marks, not the prompt.
		var argv []string
		argv = append(argv, shell)
		if login && strings.EqualFold(strings.TrimSuffix(filepath.Base(shell), ".exe"), "pwsh") && filepath.Separator == '/' {
			argv = append(argv, "-Login")
		}
		// The script is read as text and run as a script block, not dot-sourced as a file: an execution
		// policy governs script files, and Windows' default one (Restricted) refuses every file, which
		// would cost the marks on most machines. The user's policy is left as it is, for their scripts.
		argv = append(argv, "-NoLogo", "-NoExit", "-Command",
			"try { . ([scriptblock]::Create([IO.File]::ReadAllText("+pwshQuote(filepath.Join(dir, "pwsh", "init.ps1"))+"))) } catch { }")
		return Launch{Argv: argv, Env: env}, true
	}
	return Launch{}, false
}

// fishQuote quotes s as one fish word: inside single quotes, fish takes a backslash before a quote
// or a backslash as an escape and everything else literally.
func fishQuote(s string) string {
	s = strings.ReplaceAll(s, `\`, `\\`)
	s = strings.ReplaceAll(s, `'`, `\'`)
	return "'" + s + "'"
}

// pwshQuote quotes s as a PowerShell single-quoted string, where a quote is doubled.
func pwshQuote(s string) string {
	return "'" + strings.ReplaceAll(s, "'", "''") + "'"
}
