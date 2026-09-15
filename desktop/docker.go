package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strings"
)

// projectName keeps every container, network and volume of the desktop install under one compose
// project, whatever the folder is called.
const projectName = "daedalus"

// The images the launcher prefers over a local build. They are published from the repository's main
// branch; when a platform has no published image the launcher builds it on the spot instead.
const (
	agentImage    = "ghcr.io/ascorblack/daedalus:latest"
	keyproxyImage = "ghcr.io/ascorblack/daedalus-keyproxy:latest"
)

// dockerMissing is what the operator sees when Docker is not there. Docker is never installed for
// them: it is a system-wide decision, and on macOS and Windows it is a desktop application.
const dockerMissing = "Daedalus needs Docker Desktop (macOS/Windows) or Docker Engine with the compose plugin. " +
	"Install it from https://docs.docker.com/get-docker/ and start it, then run this again."

// CheckDocker fails when docker is absent, not running, or without the compose plugin. All three
// look the same to the operator — the stack cannot start — so all three carry the same message.
func CheckDocker(ctx context.Context) error {
	if _, err := exec.LookPath("docker"); err != nil {
		return errors.New(dockerMissing)
	}
	if _, err := runOut(ctx, "docker", "info", "--format", "{{.ServerVersion}}"); err != nil {
		return errors.New(dockerMissing)
	}
	if _, err := runOut(ctx, "docker", "compose", "version", "--short"); err != nil {
		return errors.New(dockerMissing)
	}
	return nil
}

// DockerVersion is the daemon's version, for the status line. An empty string means not reachable.
func DockerVersion(ctx context.Context) string {
	out, err := runOut(ctx, "docker", "info", "--format", "{{.ServerVersion}}")
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
	return runOut(ctx, "docker", composeArgs(p, telegram, args...)...)
}

// composeQuiet runs a compose command and returns only what the command itself wrote. docker and uv
// both report progress on stderr, which would otherwise end up inside a value that is read back —
// a pairing link is one line, and one line is all it may be.
func composeQuiet(ctx context.Context, p Paths, telegram bool, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, "docker", composeArgs(p, telegram, args...)...)
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
	cmd := exec.CommandContext(ctx, "docker", composeArgs(p, telegram, args...)...)
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
