package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"sync"
)

// projectName keeps every container, network and volume of the desktop install under one compose
// project, whatever the folder is called.
const projectName = "daedalus"

// defaultImageOwner is the namespace the published images live in when the remote does not name one.
const defaultImageOwner = "ascorblack"

// agentImage and keyproxyImage are what the launcher prefers over a local build. The namespace
// follows the remote the checkout comes from: a fork's code running upstream's image is a mismatch
// nothing would report. A fork that publishes no images has nothing to pull, and the start falls
// back to building, which is what happens on a platform without a published image anyway.
func agentImage() string { return "ghcr.io/" + imageOwner(botRemote()) + "/daedalus:latest" }

func keyproxyImage() string {
	return "ghcr.io/" + imageOwner(botRemote()) + "/daedalus-keyproxy:latest"
}

// imageOwner is the owner segment of a git remote — github.com/<owner>/<repo> — in the lowercase a
// registry namespace has to be, whatever case the account is written in.
func imageOwner(remote string) string {
	rest := strings.TrimSpace(remote)
	if _, after, ok := strings.Cut(rest, "://"); ok {
		rest = after
		if _, host, ok := strings.Cut(rest, "@"); ok { // ssh://git@host/owner/repo
			rest = host
		}
	} else if _, host, ok := strings.Cut(rest, "@"); ok { // git@host:owner/repo
		rest = strings.Replace(host, ":", "/", 1)
	}
	parts := strings.Split(strings.Trim(rest, "/"), "/")
	if len(parts) < 2 || parts[1] == "" {
		return defaultImageOwner
	}
	return strings.ToLower(parts[1])
}

// dockerMissing is what the operator sees when Docker is not there. Docker is never installed for
// them: it is a system-wide decision, and on macOS and Windows it is a desktop application.
const dockerMissing = "Daedalus needs Docker Desktop (macOS/Windows) or Docker Engine with the compose plugin. " +
	"Install it from https://docs.docker.com/get-docker/ and start it, then run this again."

// dockerCandidates are the places docker is installed when PATH does not name it. A process started
// from Finder inherits a minimal PATH — /usr/bin:/bin:/usr/sbin:/sbin on macOS — which is exactly
// the PATH that does not contain Docker Desktop's client, so the launcher looks where the installers
// put it rather than telling an operator whose Docker is running that Docker is missing.
func dockerCandidates(goos, home string) []string {
	var dirs []string
	switch goos {
	case "darwin":
		dirs = []string{"/usr/local/bin", "/opt/homebrew/bin"}
		if home != "" {
			dirs = append(dirs, home+"/.docker/bin")
		}
		dirs = append(dirs, "/Applications/Docker.app/Contents/Resources/bin")
	case "windows":
		dirs = []string{`C:\Program Files\Docker\Docker\resources\bin`}
	default:
		dirs = []string{"/usr/bin", "/usr/local/bin", "/snap/bin"}
	}
	name := "docker"
	separator := "/"
	if goos == "windows" {
		name, separator = "docker.exe", `\`
	}
	out := make([]string, 0, len(dirs))
	for _, dir := range dirs {
		out = append(out, dir+separator+name)
	}
	return out
}

// findDocker resolves the docker command: PATH first, because an operator who put docker somewhere
// of their own means it, then the known locations. An empty answer means it is nowhere to be found,
// which is what CheckDocker turns into the one message all of docker's absences share.
func findDocker(goos, home string, lookPath func(string) (string, error), runnable func(string) bool) string {
	if path, err := lookPath("docker"); err == nil {
		return path
	}
	for _, candidate := range dockerCandidates(goos, home) {
		if runnable(candidate) {
			return candidate
		}
	}
	return ""
}

// runnableFile reports whether a path is a file this process may execute.
func runnableFile(path string) bool {
	info, err := os.Stat(path)
	if err != nil || info.IsDir() {
		return false
	}
	// Windows has no execute bit: being a file that exists is the whole test there.
	return runtime.GOOS == "windows" || info.Mode()&0o111 != 0
}

var (
	dockerOnce sync.Once
	dockerFile string
)

// dockerPath is the command every docker invocation in this file runs, resolved once: the answer
// does not change while the launcher is running, and a search per compose call would be noise.
// It is empty when docker was not found; the callers then run the bare name, whose failure carries
// the same message as every other way Docker can be unavailable.
func dockerPath() string { dockerOnce.Do(resolveDocker); return dockerFile }

func dockerCommand() string {
	if path := dockerPath(); path != "" {
		return path
	}
	return "docker"
}

func resolveDocker() {
	home, _ := os.UserHomeDir()
	dockerFile = findDocker(runtime.GOOS, home, exec.LookPath, runnableFile)
}

// CheckDocker fails when docker is absent, not running, or without the compose plugin. All three
// look the same to the operator — the stack cannot start — so all three carry the same message.
func CheckDocker(ctx context.Context) error {
	if dockerPath() == "" {
		return errors.New(dockerMissing)
	}
	if _, err := runOut(ctx, dockerCommand(), "info", "--format", "{{.ServerVersion}}"); err != nil {
		return errors.New(dockerMissing)
	}
	if _, err := runOut(ctx, dockerCommand(), "compose", "version", "--short"); err != nil {
		return errors.New(dockerMissing)
	}
	return nil
}

// DockerVersion is the daemon's version, for the status line. An empty string means not reachable.
func DockerVersion(ctx context.Context) string {
	out, err := runOut(ctx, dockerCommand(), "info", "--format", "{{.ServerVersion}}")
	if err != nil {
		return ""
	}
	return strings.TrimSpace(out)
}

// composeArgs builds the docker argument list. The server's own compose file comes first, so
// compose resolves its relative mounts against the checkout, and the desktop override follows to
// name the published images. The telegram profile is passed only when a bot token is set: without
// one the local Bot API server has nothing to do.
func composeArgs(p Paths, telegram bool, args ...string) []string {
	out := []string{
		"compose",
		"-f", p.Compose,
		"-f", p.Override,
		"--env-file", p.Env,
		"--project-name", projectName,
	}
	if telegram {
		out = append(out, "--profile", "telegram")
	}
	return append(out, args...)
}

// compose runs a compose command and returns its combined output.
func compose(ctx context.Context, p Paths, telegram bool, args ...string) (string, error) {
	return runOut(ctx, dockerCommand(), composeArgs(p, telegram, args...)...)
}

// composeQuiet runs a compose command and returns only what the command itself wrote. docker and uv
// both report progress on stderr, which would otherwise end up inside a value that is read back —
// a pairing link is one line, and one line is all it may be.
func composeQuiet(ctx context.Context, p Paths, telegram bool, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, dockerCommand(), composeArgs(p, telegram, args...)...)
	var out, problem bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &problem
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("docker compose %s: %w: %s", strings.Join(args, " "), err, strings.TrimSpace(problem.String()))
	}
	return out.String(), nil
}

// composeStream runs a compose command with the terminal attached, for logs and builds whose
// progress the operator wants to watch as it happens.
func composeStream(ctx context.Context, p Paths, telegram bool, args ...string) error {
	cmd := exec.CommandContext(ctx, dockerCommand(), composeArgs(p, telegram, args...)...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Stdin = os.Stdin
	return cmd.Run()
}

// run executes a command and returns stdout and stderr together: docker writes progress to stderr
// and the useful part of a failure is usually there.
func runOut(ctx context.Context, name string, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, name, args...)
	var buf bytes.Buffer
	cmd.Stdout = &buf
	cmd.Stderr = &buf
	err := cmd.Run()
	if err != nil {
		return buf.String(), fmt.Errorf("%s %s: %w: %s", name, strings.Join(args, " "), err, strings.TrimSpace(buf.String()))
	}
	return buf.String(), nil
}
