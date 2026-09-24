package term

import (
	"sort"
	"strings"
)

// TagName is the variable every process of a terminal inherits, set to the terminal's id. It finds
// a process that left both the terminal's tree and its session, to end it and to count what it costs.
const TagName = "DAEDALUS_TERMINAL_ID"

// strippedEnv are removed from every spawned process's environment, whatever the caller asks. Each
// either belongs to the daemon (its own settings, or the ids of a terminal it runs inside when it is
// being developed from one), or tells a program that it runs inside some other terminal or
// multiplexer and so changes its behaviour, or — CLAUDE* — would make a coding CLI believe it is
// nested inside itself.
var strippedEnv = []string{
	"DAEDALUS_PTYD_*", "DAEDALUS_TERMINAL_ID", "DAEDALUS_LAUNCH_*", "DAEDALUS_HOOK_*", "DAEDALUS_DIAL_DIR",
	"DAEDALUS_SI_*", "DAEDALUS_SHELL_LOGIN", "DAEDALUS_USER_ZDOTDIR",
	"TERM_PROGRAM*", "VSCODE_*", "TMUX*", "STY", "WINDOW", "KITTY_*", "ITERM_*", "WT_SESSION",
	"CLAUDE*",
}

// keptEnv are inherited although a pattern above matches them; only the caller's strip_env removes
// one. CLAUDE_CONFIG_DIR is where
// a Claude Code whose configuration is not in ~/.claude keeps its login and settings: stripped with
// the rest of CLAUDE*, that Claude starts signed out. The host cannot put it back, since it lives
// in the daemon's own environment on the operator's machine.
var keptEnv = map[string]bool{"CLAUDE_CONFIG_DIR": true}

// matchEnv reports whether name matches pattern: an exact name, or a prefix followed by '*'.
func matchEnv(pattern, name string) bool {
	if p, ok := strings.CutSuffix(pattern, "*"); ok {
		return strings.HasPrefix(name, p)
	}
	return pattern == name
}

func isUTF8Locale(v string) bool {
	v = strings.ToLower(v)
	return strings.Contains(v, "utf-8") || strings.Contains(v, "utf8")
}

// BuildEnv is the environment of a spawned process: the inherited one without the stripped names
// and the caller's strip patterns, plus the terminal's own settings, plus the caller's additions on
// top (so the host can still choose, say, a different TERM for one launch).
func BuildEnv(inherited []string, strip []string, extra map[string]string, terminalID string) []string {
	env := map[string]string{}
	var order []string
	for _, kv := range inherited {
		k, v, ok := strings.Cut(kv, "=")
		if !ok || k == "" {
			continue
		}
		dropped := false
		for _, p := range strippedEnv {
			if matchEnv(p, k) && !keptEnv[k] {
				dropped = true
				break
			}
		}
		for _, p := range strip {
			if matchEnv(p, k) {
				dropped = true
				break
			}
		}
		if dropped {
			continue
		}
		if _, seen := env[k]; !seen {
			order = append(order, k)
		}
		env[k] = v
	}
	set := func(k, v string) {
		if _, seen := env[k]; !seen {
			order = append(order, k)
		}
		env[k] = v
	}
	set("TERM", "xterm-256color")
	set("COLORTERM", "truecolor")
	set("CLAUDE_CODE_NO_FLICKER", "1")
	set("CLAUDE_CODE_SCROLL_SPEED", "3")
	set("DAEDALUS_TERMINAL_ID", terminalID)

	// A UTF-8 character type, without overriding a locale the user chose. The effective one is the
	// first of LC_ALL, LC_CTYPE and LANG that is set; when it is not UTF-8, the non-UTF-8 overrides
	// are removed (they would win over LANG) and LANG becomes C.UTF-8. A host terminal whose LANG
	// is ru_RU.UTF-8 keeps it.
	effective := ""
	for _, k := range []string{"LC_ALL", "LC_CTYPE", "LANG"} {
		if v := env[k]; v != "" {
			effective = v
			break
		}
	}
	if !isUTF8Locale(effective) {
		for _, k := range []string{"LC_ALL", "LC_CTYPE"} {
			if v, ok := env[k]; ok && !isUTF8Locale(v) {
				delete(env, k)
			}
		}
		set("LANG", "C.UTF-8")
	}

	keys := make([]string, 0, len(extra))
	for k := range extra {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		set(k, extra[k])
	}

	out := make([]string, 0, len(env))
	for _, k := range order {
		if v, ok := env[k]; ok {
			out = append(out, k+"="+v)
		}
	}
	return out
}

// LookPath resolves a program name against the PATH of the environment it will run in, not the
// daemon's own: the two differ as soon as the host passes a PATH for a launch.
func LookPath(name string, env []string, dir string) (string, bool) {
	if strings.Contains(name, "/") {
		p := name
		if !strings.HasPrefix(p, "/") {
			p = dir + "/" + p
		}
		return p, isExecutable(p)
	}
	path := ""
	for _, kv := range env {
		if v, ok := strings.CutPrefix(kv, "PATH="); ok {
			path = v
		}
	}
	if path == "" {
		path = "/usr/local/bin:/usr/bin:/bin"
	}
	for _, d := range strings.Split(path, ":") {
		if d == "" {
			d = "."
		}
		p := d + "/" + name
		if !strings.HasPrefix(p, "/") {
			p = dir + "/" + p
		}
		if isExecutable(p) {
			return p, true
		}
	}
	return "", false
}
