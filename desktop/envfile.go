package main

import (
	"crypto/rand"
	"encoding/hex"
	"os"
	"strings"
)

// envVar is one line of an env file. The launcher keeps the order fixed so a rewritten file reads
// the same way every time and a diff shows only what changed.
type envVar struct {
	Key   string
	Value string
}

// Setup holds the answers the setup page collects. Everything is optional except that the stack is
// only useful with either a provider key or a CLI login on the host.
type Setup struct {
	DeepseekKey   string
	OpenrouterKey string
	OpencodeKey   string
	BotToken      string
	OwnerID       string
	APIID         string
	APIHash       string
	USDPerDay     string
}

// Telegram reports whether the Telegram side of the stack should run. Without a token the bot is
// unreachable from Telegram and the local Bot API server has nothing to serve, so it stays down and
// the operator uses the browser app.
func (s Setup) Telegram() bool { return strings.TrimSpace(s.BotToken) != "" }

// mergeEnv rewrites the values of the given keys in an existing env file and appends the ones it
// does not find. Comments, blank lines and keys the operator added by hand are left alone: the file
// is theirs to edit after the first run.
func mergeEnv(existing string, updates []envVar) string {
	lines := []string{}
	if existing != "" {
		lines = strings.Split(strings.TrimSuffix(existing, "\n"), "\n")
	}
	for _, v := range updates {
		replaced := false
		for i, line := range lines {
			trimmed := strings.TrimLeft(line, " \t")
			if strings.HasPrefix(trimmed, v.Key+"=") || strings.HasPrefix(trimmed, "export "+v.Key+"=") {
				lines[i] = v.Key + "=" + v.Value
				replaced = true
				break
			}
		}
		if !replaced {
			lines = append(lines, v.Key+"="+v.Value)
		}
	}
	return strings.Join(lines, "\n") + "\n"
}

// readEnv parses an env file into a map. Comments and malformed lines are skipped; a quoted value
// is unquoted, because a value written by hand often is.
func readEnv(text string) map[string]string {
	out := map[string]string{}
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		line = strings.TrimPrefix(line, "export ")
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		value = strings.TrimSpace(value)
		if len(value) >= 2 && (value[0] == '"' || value[0] == '\'') && value[len(value)-1] == value[0] {
			value = value[1 : len(value)-1]
		}
		out[strings.TrimSpace(key)] = value
	}
	return out
}

// clean strips what a pasted value drags along. A newline inside a value would silently turn the
// rest of the key into a second variable, so it is removed rather than escaped.
func clean(value string) string {
	value = strings.ReplaceAll(value, "\r", "")
	value = strings.ReplaceAll(value, "\n", "")
	return strings.TrimSpace(value)
}

// envUpdates is everything the launcher sets in <data>/.env. The three DAEDALUS_* paths are
// absolute because the rebuilder container resolves them on the host, not inside a project
// directory; SERVICES_PUBLIC_HOST is the loopback address because a desktop install serves the
// operator's own machine.
func envUpdates(p Paths, s Setup, searxngSecret, home string) []envVar {
	daily := clean(s.USDPerDay)
	if daily == "" {
		daily = "20"
	}
	return []envVar{
		{"TELEGRAM_BOT_TOKEN", clean(s.BotToken)},
		{"OWNER_USER_ID", clean(s.OwnerID)},
		{"TELEGRAM_API_ID", clean(s.APIID)},
		{"TELEGRAM_API_HASH", clean(s.APIHash)},
		{"USD_PER_DAY", daily},
		{"SEARXNG_SECRET", searxngSecret},
		{"MINIAPP_PUBLIC_URL", ""},
		{"API_PORT", "8765"},
		{"SERVICES_PORT_RANGE", "8100-8119"},
		{"SERVICES_PUBLIC_HOST", "127.0.0.1"},
		{"DAEDALUS_COMPOSE_PROJECT_DIR", p.Bot},
		{"DAEDALUS_COMPOSE_FILE", p.Compose},
		{"DAEDALUS_CORE_PROJECT_DIR", p.Core},
		{"DAEDALUS_SECRETS_FILE", p.KeyproxyEnv},
		{"DAEDALUS_SSH_DIR", p.SSH},
		{"DAEDALUS_HARNESS_HOME", home},
	}
}

// keyproxyUpdates is everything the launcher sets in daedalus-secrets/keyproxy.env. Provider keys
// live only here: the file is outside every mount the agent container gets, so the agent process
// never holds a key even when it edits its own code.
func keyproxyUpdates(s Setup) []envVar {
	daily := clean(s.USDPerDay)
	if daily == "" {
		daily = "20"
	}
	return []envVar{
		{"DEEPSEEK_API_KEY", clean(s.DeepseekKey)},
		{"OPENROUTER_API_KEY", clean(s.OpenrouterKey)},
		{"OPENCODE_API_KEY", clean(s.OpencodeKey)},
		{"KEYPROXY_USD_PER_DAY", daily},
	}
}

// WriteSetup writes both env files. The existing values are read first, so re-running setup keeps
// a key the operator does not retype and keeps the SearXNG secret stable across runs.
func WriteSetup(p Paths, s Setup) error {
	if err := p.EnsureDirs(); err != nil {
		return err
	}
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		home = p.Data // the CLI logins are mounted from here; a missing home is not fatal
	}
	current := readEnv(readFile(p.Env))
	secret := current["SEARXNG_SECRET"]
	if secret == "" {
		secret, err = randomSecret()
		if err != nil {
			return err
		}
	}
	if err := os.WriteFile(p.Env, []byte(mergeEnv(readFile(p.Env), envUpdates(p, s, secret, home))), 0o600); err != nil {
		return err
	}
	if err := os.WriteFile(p.KeyproxyEnv, []byte(mergeEnv(readFile(p.KeyproxyEnv), keyproxyUpdates(s))), 0o600); err != nil {
		return err
	}
	return SyncBotEnv(p)
}

// SyncBotEnv copies <data>/.env to the checkout root. The compose file reads ../.env as the agent
// container's environment and the rebuilder reads .env from the project directory, both of which
// are the checkout, while the launcher keeps the file it owns next to the data folder.
func SyncBotEnv(p Paths) error {
	if !exists(p.Bot) {
		return nil
	}
	body, err := os.ReadFile(p.Env)
	if err != nil {
		return err
	}
	return os.WriteFile(p.BotEnv, body, 0o600)
}

// CurrentSetup reads back what a previous run wrote, so the setup page opens with the values that
// are in force rather than with empty fields. Keys come from the secrets file.
func CurrentSetup(p Paths) Setup {
	env := readEnv(readFile(p.Env))
	keys := readEnv(readFile(p.KeyproxyEnv))
	return Setup{
		DeepseekKey:   keys["DEEPSEEK_API_KEY"],
		OpenrouterKey: keys["OPENROUTER_API_KEY"],
		OpencodeKey:   keys["OPENCODE_API_KEY"],
		BotToken:      env["TELEGRAM_BOT_TOKEN"],
		OwnerID:       env["OWNER_USER_ID"],
		APIID:         env["TELEGRAM_API_ID"],
		APIHash:       env["TELEGRAM_API_HASH"],
		USDPerDay:     env["USD_PER_DAY"],
	}
}

// APIPort is the port the bot's API and app listen on, as the env file has it.
func APIPort(p Paths) string {
	if port := readEnv(readFile(p.Env))["API_PORT"]; port != "" {
		return port
	}
	return "8765"
}

func readFile(path string) string {
	body, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return string(body)
}

func randomSecret() (string, error) {
	buf := make([]byte, 24)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	return hex.EncodeToString(buf), nil
}
