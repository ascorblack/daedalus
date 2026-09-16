package main

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"strings"
)

// The two repositories the stack runs: the bot and the core it is built on. A fork is used by
// setting DAEDALUS_GIT_REMOTE or DAEDALUS_CORE_GIT_REMOTE before starting the launcher.
const (
	defaultBotRemote  = "https://github.com/ascorblack/daedalus"
	defaultCoreRemote = "https://github.com/ascorblack/protocore-exp"
)

// branch is what a checkout follows. There is one: the published code is what main says it is.
const branch = "main"

// tarballLimit caps what a single download may expand to. Both repositories are a few megabytes;
// this is the wall between a bad URL and a full disk, not a size anyone should approach.
const tarballLimit = 512 << 20

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

// repoPath is the owner/repo of a git remote, whatever form the remote is written in.
func repoPath(remote string) string {
	rest := strings.TrimSuffix(strings.TrimSpace(remote), ".git")
	if _, after, ok := strings.Cut(rest, "://"); ok {
		rest = after
		if _, host, ok := strings.Cut(rest, "@"); ok { // ssh://git@host/owner/repo
			rest = host
		}
	} else if _, host, ok := strings.Cut(rest, "@"); ok { // git@host:owner/repo
		rest = strings.Replace(host, ":", "/", 1)
	}
	parts := strings.Split(strings.Trim(rest, "/"), "/")
	if len(parts) < 3 {
		return ""
	}
	return parts[1] + "/" + parts[2]
}

// tarballURL is where GitHub serves a branch as a gzipped tar. A checkout is made from this rather
// than by cloning: it needs no git on the host and no git in a container of its own, and what
// arrives is the tree, which is all a checkout of a published branch ever was.
func tarballURL(remote string) string {
	owner := repoPath(remote)
	if owner == "" {
		return ""
	}
	return "https://codeload.github.com/" + owner + "/tar.gz/refs/heads/" + branch
}

// fetchTarball downloads the whole archive before anything is written: a download that fails
// halfway must leave the checkout it was going to replace exactly as it was.
func fetchTarball(ctx context.Context, url string) ([]byte, error) {
	if url == "" {
		return nil, errors.New("the checkouts are fetched as GitHub tarballs; DAEDALUS_GIT_REMOTE and DAEDALUS_CORE_GIT_REMOTE must name a github.com/owner/repo")
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("%s: HTTP %d", url, response.StatusCode)
	}
	return io.ReadAll(io.LimitReader(response.Body, tarballLimit))
}

// unpackTarball writes the archive into dir, dropping the single folder GitHub wraps a tree in.
// Files already there are overwritten and everything else in dir is left alone, so a checkout keeps
// its .git, its .env and whatever the agent built in it.
func unpackTarball(archive []byte, dir string) error {
	zipped, err := gzip.NewReader(bytes.NewReader(archive))
	if err != nil {
		return err
	}
	defer zipped.Close()
	reader := tar.NewReader(zipped)
	for {
		header, err := reader.Next()
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return err
		}
		// Every path in the archive is under one folder named after the repository and the commit;
		// that folder is the tarball's, not the tree's, so the first segment goes.
		name := strings.TrimPrefix(header.Name, "./")
		_, name, ok := strings.Cut(name, "/")
		if !ok || name == "" {
			continue
		}
		target, err := safeJoin(dir, name)
		if err != nil {
			return err
		}
		switch header.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(target, 0o755); err != nil {
				return err
			}
		case tar.TypeReg:
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return err
			}
			mode := os.FileMode(header.Mode).Perm()
			if mode == 0 {
				mode = 0o644
			}
			file, err := os.OpenFile(target, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, mode)
			if err != nil {
				return err
			}
			if _, err := io.Copy(file, io.LimitReader(reader, tarballLimit)); err != nil {
				file.Close()
				return err
			}
			if err := file.Close(); err != nil {
				return err
			}
		case tar.TypeSymlink:
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return err
			}
			os.Remove(target)
			if err := os.Symlink(header.Linkname, target); err != nil {
				return err
			}
		}
	}
}

// safeJoin refuses a name that would land outside dir. An archive is downloaded code and is treated
// as such: "../../.ssh/authorized_keys" is a plausible entry in an archive nobody checked.
func safeJoin(dir, name string) (string, error) {
	clean := path.Clean("/" + name)
	if clean == "/" {
		return "", fmt.Errorf("archive entry %q has no name", name)
	}
	return filepath.Join(dir, filepath.FromSlash(clean[1:])), nil
}

// gitArgs runs git inside the agent's own container image: git is not assumed to be installed on
// the host, and that image is there by definition — it is the one the stack runs.
func gitArgs(p Paths, args ...string) []string {
	run := []string{"run", "--rm", "-v", p.Data + ":/work", "-w", "/work"}
	// The container writes into the data folder as whoever runs it. Left to itself that is root, and a
	// checkout root owns is one the launcher (and the operator) can no longer write .env into; so on the
	// systems that have a uid, git runs as the launcher's own user. Windows has none, and Docker Desktop
	// maps the files to the desktop user there anyway.
	if uid := os.Getuid(); uid >= 0 {
		run = append(run, "--user", fmt.Sprintf("%d:%d", uid, os.Getgid()), "-e", "HOME=/tmp")
	}
	return append(append(run, "--entrypoint", "git", agentImage()), args...)
}

// commitAll records the tree as one commit. The checkouts have a real history and no remote: that
// is what local self-development works against — the agent commits, and a bad commit is undone by
// going back to the one before it — and an update is the next commit on top, so the history stays
// linear and nothing the agent did is thrown away by a fetch.
func commitAll(ctx context.Context, p Paths, name, message string) error {
	if _, err := runDocker(ctx, gitArgs(p, "-C", "/work/"+name, "add", "-A")...); err != nil {
		return err
	}
	pending, err := runDocker(ctx, gitArgs(p, "-C", "/work/"+name, "status", "--porcelain")...)
	if err != nil {
		return err
	}
	if strings.TrimSpace(pending) == "" {
		return nil
	}
	_, err = runDocker(ctx, gitArgs(p,
		"-C", "/work/"+name,
		"-c", "user.name=daedalus-desktop",
		"-c", "user.email=daedalus-desktop@localhost",
		"commit", "-q", "-m", message)...)
	return err
}

// removeTracked deletes everything the last commit holds, so files dropped upstream do not survive
// an update. Untracked and ignored files — the .env, a virtualenv, whatever a session left — are
// not touched, which is the difference between this and emptying the folder.
func removeTracked(ctx context.Context, p Paths, dir, name string) error {
	listing, err := runDocker(ctx, gitArgs(p, "-C", "/work/"+name, "ls-files", "-z")...)
	if err != nil {
		return err
	}
	for _, entry := range strings.Split(listing, "\x00") {
		if entry == "" {
			continue
		}
		target, err := safeJoin(dir, entry)
		if err != nil {
			return err
		}
		if err := os.Remove(target); err != nil && !os.IsNotExist(err) {
			return err
		}
	}
	return nil
}

type repo struct {
	dir    string
	name   string
	remote string
}

func repos(p Paths) []repo {
	return []repo{
		{p.Bot, "daedalus", botRemote()},
		{p.Core, "protocore-exp", coreRemote()},
	}
}

// EnsureRepos makes what is missing. An existing checkout is left as it is — it may hold the
// agent's own work in progress — and is only moved by Update.
func EnsureRepos(ctx context.Context, p Paths, log func(string, ...any)) error {
	if err := p.EnsureDirs(); err != nil {
		return err
	}
	for _, r := range repos(p) {
		if exists(r.dir) {
			continue
		}
		log("fetching %s", r.name)
		archive, err := fetchTarball(ctx, tarballURL(r.remote))
		if err != nil {
			return err
		}
		if err := os.MkdirAll(r.dir, 0o755); err != nil {
			return err
		}
		if err := unpackTarball(archive, r.dir); err != nil {
			return err
		}
		if _, err := runDocker(ctx, gitArgs(p, "-C", "/work/"+r.name, "init", "-q", "-b", branch)...); err != nil {
			return err
		}
		if err := commitAll(ctx, p, r.name, "the published "+branch); err != nil {
			return err
		}
	}
	return nil
}

// UpdateRepos moves both checkouts to what is published, as a commit on top of what they hold. The
// agent's own merged work arrives this way, and so does everything else in the published tree.
func UpdateRepos(ctx context.Context, p Paths, log func(string, ...any)) error {
	for _, r := range repos(p) {
		if !exists(r.dir) {
			continue
		}
		log("updating %s", r.name)
		archive, err := fetchTarball(ctx, tarballURL(r.remote))
		if err != nil {
			return err
		}
		if err := removeTracked(ctx, p, r.dir, r.name); err != nil {
			return err
		}
		if err := unpackTarball(archive, r.dir); err != nil {
			return err
		}
		if err := commitAll(ctx, p, r.name, "the published "+branch); err != nil {
			return err
		}
	}
	return nil
}
