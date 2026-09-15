package main

import (
	"os"
	"path/filepath"
)

// Paths is the folder layout the launcher owns. It is the layout deploy/compose.yaml already
// expects on a server — the compose file mounts "..", "../../protocore-exp" and
// "../../daedalus-secrets/..." relative to deploy/ — so compose runs unchanged against it.
//
//	<data>/daedalus                       the bot checkout (compose lives in its deploy/)
//	<data>/protocore-exp                  the core checkout
//	<data>/daedalus-secrets/keyproxy.env  provider keys, never inside a mounted checkout
//	<data>/daedalus-secrets/ssh           hosts the agent may reach (may stay empty)
//	<data>/.env                           the values compose interpolates
//	<data>/compose.desktop.yaml           the override that points at the published images
type Paths struct {
	Data        string
	Bot         string
	Core        string
	Secrets     string
	KeyproxyEnv string
	SSH         string
	Env         string
	BotEnv      string
	Compose     string
	Override    string
}

// NewPaths resolves the data directory: --data when given, otherwise ./data next to the binary's
// working directory, so the binary can be dropped into any folder and run from there.
func NewPaths(dataDir string) (Paths, error) {
	if dataDir == "" {
		dataDir = "data"
	}
	abs, err := filepath.Abs(dataDir)
	if err != nil {
		return Paths{}, err
	}
	bot := filepath.Join(abs, "daedalus")
	secrets := filepath.Join(abs, "daedalus-secrets")
	return Paths{
		Data:        abs,
		Bot:         bot,
		Core:        filepath.Join(abs, "protocore-exp"),
		Secrets:     secrets,
		KeyproxyEnv: filepath.Join(secrets, "keyproxy.env"),
		SSH:         filepath.Join(secrets, "ssh"),
		Env:         filepath.Join(abs, ".env"),
		BotEnv:      filepath.Join(bot, ".env"),
		Compose:     filepath.Join(bot, "deploy", "compose.yaml"),
		Override:    filepath.Join(abs, "compose.desktop.yaml"),
	}, nil
}

// EnsureDirs creates the folders that must exist before anything is written into them. The secrets
// directory is 0700: it holds the file the key proxy reads.
func (p Paths) EnsureDirs() error {
	if err := os.MkdirAll(p.Data, 0o755); err != nil {
		return err
	}
	if err := os.MkdirAll(p.Secrets, 0o700); err != nil {
		return err
	}
	return os.MkdirAll(p.SSH, 0o700)
}

// Configured reports whether a previous run already wrote the environment. A missing .env is the
// signal to ask the questions again; an existing one means start straight away. The checkouts are a
// separate matter: they are cloned on the first start, with or without answers.
func (p Paths) Configured() bool { return exists(p.Env) }

func exists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}
