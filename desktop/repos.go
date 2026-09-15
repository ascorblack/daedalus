package main

import (
	"context"
	"fmt"
	"os"
	"strings"
)

// The two repositories the stack runs: the bot and the core it is built on. A fork is used by
// setting DAEDALUS_GIT_REMOTE or DAEDALUS_CORE_GIT_REMOTE before starting the launcher.
const (
	defaultBotRemote  = "https://github.com/ascorblack/daedalus"
	defaultCoreRemote = "https://github.com/ascorblack/protocore-exp"
	gitImage          = "alpine/git"
)

// cloneDepth keeps the checkouts small. It is not 1: the supervisor rolls back to an earlier
// known-good commit, and a single-commit history has nothing to roll back to.
const cloneDepth = "50"

func botRemote() string {
	if remote := strings.TrimSpace(os.Getenv("DAEDALUS_GIT_REMOTE")); remote != "" {
		return remote
	}
	return defaultBotRemote
}

func coreRemote() string {
	if remote := strings.TrimSpace(os.Getenv("DAEDALUS_CORE_GIT_REMOTE")); remote != "" {
		return remote
	}
	return defaultCoreRemote
}

// gitArgs runs git inside a container: git is not assumed to be installed on the host, and Docker
// is there by definition because the stack needs it.
func gitArgs(p Paths, args ...string) []string {
	run := []string{"run", "--rm", "-v", p.Data + ":/work", "-w", "/work"}
	// The container writes into the data folder as whoever runs it. Left to itself that is root, and a
	// checkout root owns is one the launcher (and the operator) can no longer write .env into; so on the
	// systems that have a uid, git runs as the launcher's own user. Windows has none, and Docker Desktop
	// maps the files to the desktop user there anyway.
	if uid := os.Getuid(); uid >= 0 {
		run = append(run, "--user", fmt.Sprintf("%d:%d", uid, os.Getgid()), "-e", "HOME=/tmp")
	}
	return append(append(run, gitImage), args...)
}

// EnsureRepos clones what is missing. An existing checkout is left as it is — it may hold the
// agent's own work in progress — and is only moved by Update.
func EnsureRepos(ctx context.Context, p Paths, log func(string, ...any)) error {
	if err := p.EnsureDirs(); err != nil {
		return err
	}
	for _, repo := range []struct {
		dir    string
		name   string
		remote string
	}{
		{p.Bot, "daedalus", botRemote()},
		{p.Core, "protocore-exp", coreRemote()},
	} {
		if exists(repo.dir) {
			continue
		}
		log("cloning %s", repo.name)
		if _, err := runOut(ctx, dockerCommand(), gitArgs(p, "clone", "--depth", cloneDepth, repo.remote, "/work/"+repo.name)...); err != nil {
			return err
		}
	}
	return nil
}

// UpdateRepos fetches and resets both checkouts to origin/main. This is a reset, not a merge: the
// checkouts are the stack's copy of the published code, and the agent's own changes reach them
// through a merged pull request, never as an uncommitted edit that survives an update.
func UpdateRepos(ctx context.Context, p Paths, log func(string, ...any)) error {
	for _, repo := range []struct {
		dir  string
		name string
	}{{p.Bot, "daedalus"}, {p.Core, "protocore-exp"}} {
		if !exists(repo.dir) {
			continue
		}
		log("updating %s", repo.name)
		if _, err := runOut(ctx, dockerCommand(), gitArgs(p, "-C", "/work/"+repo.name, "fetch", "--depth", cloneDepth, "origin", "main")...); err != nil {
			return err
		}
		if _, err := runOut(ctx, dockerCommand(), gitArgs(p, "-C", "/work/"+repo.name, "reset", "--hard", "origin/main")...); err != nil {
			return err
		}
	}
	return nil
}
