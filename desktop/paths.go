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
//	<data>/mode                           docker or native, chosen once and remembered
//
// Native mode adds the pieces a container would otherwise have held. The database, the workspaces
// and the runtime are files under the same folder rather than Docker volumes, which is what makes
// the whole installation one directory to back up and one directory to delete:
//
//	<data>/runtime/uv/uv                  the installer for everything below it
//	<data>/runtime/python/                the CPython uv manages
//	<data>/runtime/venv/                  the app's environment
//	<data>/runtime/bin/rg                 the one binary Search needs
//	<data>/runtime/git/                   MinGit, on Windows only
//	<data>/runtime/node/                  an extra, on demand
//	<data>/runtime/browsers/              an extra, on demand
//	<data>/runtime/logs/                  the supervisor's output, rotated by the launcher
//	<data>/state/  <data>/workspaces/     what the volumes hold in Docker mode
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
	Mode        string

	Runtime         string
	RuntimeUV       string
	RuntimePython   string
	RuntimeVenv     string
	RuntimeBin      string
	RuntimeGit      string
	RuntimeNode     string
	RuntimeBrowsers string
	RuntimeStamps   string
	RuntimeLogs     string
	State           string
	Workspaces      string
}

// NewPaths resolves the data directory: --data when given, otherwise the default the executable's
// own location implies, so the launcher can be dropped into any folder and run from there.
func NewPaths(dataDir string) (Paths, error) {
	if dataDir == "" {
		exe, err := os.Executable()
		if err != nil {
			exe = ""
		}
		dataDir = DefaultDataDir(exe)
	}
	abs, err := filepath.Abs(dataDir)
	if err != nil {
		return Paths{}, err
	}
	bot := filepath.Join(abs, "daedalus")
	secrets := filepath.Join(abs, "daedalus-secrets")
	runtimeDir := filepath.Join(abs, "runtime")
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
		Mode:        filepath.Join(abs, "mode"),

		Runtime:         runtimeDir,
		RuntimeUV:       filepath.Join(runtimeDir, "uv"),
		RuntimePython:   filepath.Join(runtimeDir, "python"),
		RuntimeVenv:     filepath.Join(runtimeDir, "venv"),
		RuntimeBin:      filepath.Join(runtimeDir, "bin"),
		RuntimeGit:      filepath.Join(runtimeDir, "git"),
		RuntimeNode:     filepath.Join(runtimeDir, "node"),
		RuntimeBrowsers: filepath.Join(runtimeDir, "browsers"),
		RuntimeStamps:   filepath.Join(runtimeDir, "installed"),
		RuntimeLogs:     filepath.Join(runtimeDir, "logs"),
		State:           filepath.Join(abs, "state"),
		Workspaces:      filepath.Join(abs, "workspaces"),
	}, nil
}

// bundleRoot reports the .app directory an executable is running out of, and whether it is running
// out of one at all. The layout macOS requires is <Something>.app/Contents/MacOS/<executable>.
func bundleRoot(exe string) (string, bool) {
	if exe == "" {
		return "", false
	}
	macos := filepath.Dir(exe)      // .../Contents/MacOS
	contents := filepath.Dir(macos) // .../Contents
	app := filepath.Dir(contents)   // .../Something.app
	if filepath.Base(macos) != "MacOS" || filepath.Base(contents) != "Contents" || filepath.Ext(app) != ".app" {
		return "", false
	}
	return app, true
}

// DefaultDataDir is where everything the installation owns goes when --data does not say. From a
// bundle it is next to the .app — the folder the operator dropped the app into, which is the "one
// folder" the whole installation is. It is deliberately not inside the bundle, which the next
// download replaces, and deliberately not relative: Finder starts a bundled program with "/" as its
// working directory, so a relative default would try to write into the root of the disk. Started
// from a terminal as a plain executable it stays relative, so the folder follows the shell.
func DefaultDataDir(exe string) string {
	if app, ok := bundleRoot(exe); ok {
		return filepath.Join(filepath.Dir(app), "data")
	}
	return "data"
}

// Bundled reports whether this process is the executable inside a .app — which is also to say that
// it was most likely started from Finder, with no terminal to print to.
func Bundled() bool {
	exe, err := os.Executable()
	if err != nil {
		return false
	}
	_, ok := bundleRoot(exe)
	return ok
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

// EnsureNativeDirs creates what native mode writes into and Docker mode keeps in volumes. The state
// directory is 0700: it holds the database, the sessions and the pairing links.
func (p Paths) EnsureNativeDirs() error {
	if err := p.EnsureDirs(); err != nil {
		return err
	}
	for _, dir := range []string{p.Runtime, p.RuntimeBin, p.RuntimeStamps, p.RuntimeLogs, p.Workspaces} {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return err
		}
	}
	return os.MkdirAll(p.State, 0o700)
}

// Configured reports whether a previous run already wrote the environment. A missing .env is the
// signal to ask the questions again; an existing one means start straight away. The checkouts are a
// separate matter: they are cloned on the first start, with or without answers.
func (p Paths) Configured() bool { return exists(p.Env) }

func exists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}
