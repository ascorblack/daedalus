package main

// Projects, in Docker mode. A project is a folder of the operator's own that the agents started in
// it work inside. Natively there is nothing to do: the bot is a process of the operator's user and
// the folder is simply there. In a container it is not — the agent sees only what is mounted — so a
// project whose folder is not mounted is a row the app reports with "reachable": false, and this is
// what closes that gap.
//
// The entry written is the folder mapped to itself, the same absolute path inside the container as
// outside, because the project stores the path the operator gave and that one string has to name the
// folder from both sides. A mount at a different container path would leave the project unreachable
// at the path it is stored under, which is worse than no mount because nothing would say so.
//
// The mounts are remembered in a file of the launcher's own rather than left in the override, which
// is rewritten from scratch on every start. Removing a project does not remove its mount and removing
// a mount does not remove the project: the mount is only how the container comes to see the folder.

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// mountsFile is where the launcher keeps the folders it has mounted, one absolute path per line.
func mountsFile(p Paths) string { return filepath.Join(p.Data, "project-mounts") }

// Project is the part of the app's answer this file needs.
type Project struct {
	Name      string `json:"name"`
	Root      string `json:"root"`
	Reachable bool   `json:"reachable"`
}

// Projects asks the running app which folders the operator has added and which of them it can see.
func Projects(ctx context.Context, port, token string) ([]Project, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://127.0.0.1:"+port+"/api/projects", nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("X-Daedalus-Token", token)
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("the app answered %s to /api/projects", resp.Status)
	}
	var projects []Project
	if err := json.NewDecoder(resp.Body).Decode(&projects); err != nil {
		return nil, err
	}
	return projects, nil
}

// mountProjects is the whole of the Docker-mode gap, run once the stack is up: ask the app what it
// cannot see, write those folders into the launcher's mount list, and say what is now one restart
// away. It never restarts anything itself — a restart takes the agent away from whatever it is
// doing, and that is the operator's call, taken with the Stop and Start the page already has.
//
// Nothing here is a hard failure. An installation whose app is not answering, or whose token cannot
// be read, is told how to add the entry by hand instead, which is the same entry.
func (a *App) mountProjects(ctx context.Context) {
	p, log := a.paths, a.log
	if a.Native() {
		return // no container, nothing to mount: the folder is simply there
	}
	token := a.apiToken(ctx)
	if token == "" {
		log("the launcher could not read the app's API token, so it cannot ask which project folders are not mounted")
		explainByHand(p, nil, log)
		return
	}
	projects, err := Projects(ctx, APIPort(p), token)
	if err != nil {
		log("could not read the projects (%s)", err)
		explainByHand(p, nil, log)
		return
	}
	var wanted []string
	for _, project := range projects {
		if project.Reachable {
			continue
		}
		if reason := refuseMount(project.Root); reason != "" {
			log("%s cannot be mounted: %s", project.Name, reason)
			continue
		}
		wanted = append(wanted, project.Root)
	}
	if len(wanted) == 0 {
		return
	}
	added, err := addMounts(p, wanted)
	if err != nil {
		log("the mount for %d folder(s) could not be written (%s)", len(wanted), err)
		explainByHand(p, wanted, log)
		return
	}
	if len(added) == 0 {
		// The entries are already in the file, so the restart that would apply them has not happened.
		log("%d project folder(s) are mounted in %s but the running container was started before that — Stop and Start to let it see them", len(wanted), filepath.Base(p.Override))
		return
	}
	for _, root := range added {
		log("%s is not visible to the agent; it is now in the mount list", root)
	}
	log("restart to mount: press Stop and then Start, and the agent sees %s", strings.Join(added, ", "))
}

// explainByHand says what the operator does when the launcher cannot do it for them. It is the same
// entry, in the file the launcher would have put it in.
func explainByHand(p Paths, roots []string, log func(string, ...any)) {
	if len(roots) == 0 {
		log("a project folder the container cannot see is mounted by adding it to %s under services.daedalus.volumes, as \"/your/folder:/your/folder\" — the same path on both sides — and starting the stack again", mountsFile(p))
		return
	}
	for _, root := range roots {
		log("add %q to %s (one path per line), or put \"%s:%s\" under services.daedalus.volumes in %s, then start the stack again", root, mountsFile(p), root, root, p.Override)
	}
}

// refuseMount reports why a path may not become a bind mount, or "" when it may. A project root is
// typed by the operator and stored as they gave it; a path that cannot be written as one compose
// entry must not be written as a broken one.
func refuseMount(root string) string {
	switch {
	case !strings.HasPrefix(root, "/"):
		return "it is not an absolute path"
	case strings.ContainsAny(root, ":\n\r\t\""):
		return "a compose volume entry is separated by colons and cannot carry one in the path"
	case root == "/":
		return "the whole filesystem is not a project"
	}
	return ""
}

// readMounts reads the folders the launcher has been asked to mount.
func readMounts(p Paths) []string {
	var out []string
	for _, line := range strings.Split(readFile(mountsFile(p)), "\n") {
		if path := strings.TrimSpace(line); path != "" && refuseMount(path) == "" {
			out = append(out, path)
		}
	}
	return out
}

// addMounts adds what is not there already and returns what it added.
func addMounts(p Paths, roots []string) ([]string, error) {
	have := map[string]bool{}
	current := readMounts(p)
	for _, path := range current {
		have[path] = true
	}
	var added []string
	for _, root := range roots {
		if have[root] {
			continue
		}
		have[root] = true
		current = append(current, root)
		added = append(added, root)
	}
	if len(added) == 0 {
		return nil, nil
	}
	sort.Strings(current)
	body := strings.Join(current, "\n") + "\n"
	if err := os.WriteFile(mountsFile(p), []byte(body), 0o644); err != nil {
		return nil, err
	}
	return added, nil
}

// withProjectMounts puts the mount list into the override the launcher writes. It is spliced into
// the agent service rather than appended to the document because a YAML file has one `services` key,
// and compose merges the volumes of an override into the ones the compose file already declares.
func withProjectMounts(p Paths, body string) string {
	roots := readMounts(p)
	if len(roots) == 0 {
		return body
	}
	var entries strings.Builder
	entries.WriteString("    volumes:\n")
	for _, root := range roots {
		// The same absolute path on both sides: the project is stored under the path the operator
		// gave, and that is the path the agent has to find it at.
		entries.WriteString(fmt.Sprintf("      - %s:%s\n", root, root))
	}
	const anchor = "  daedalus:\n"
	at := strings.Index(body, anchor)
	if at < 0 {
		return body
	}
	at += len(anchor)
	return body[:at] + entries.String() + body[at:]
}
