package main

import (
	"context"
	"encoding/json"
	"strings"
	"time"
)

// The agent can change its own code in a desktop installation. There is no pull request to review
// here: the change is committed to the checkout and runs on the next start, so the launcher has to
// say so — a Stop and a Start from this page are one of the two ways the operator applies it, and
// the other is closing the window and opening it again.
//
// The two files live in the state volume, which only the container can see, so the launcher asks the
// container to print them. It is one command for both, because each `docker compose exec` costs the
// better part of a second and this page polls.

const (
	pendingFile   = "/srv/state/selfdev/pending.json"
	resultFile    = "/srv/state/selfdev/result.json"
	pendingMarker = "--- pending ---"
	resultMarker  = "--- result ---"
	changeMaxAge  = 10 * time.Second
)

// ChangeNotice is what the page says about the agent's own code.
type ChangeNotice struct {
	Pending bool   `json:"pending"`
	Summary string `json:"summary"`
	Commit  string `json:"commit"`
	Status  string `json:"status"`
	Detail  string `json:"detail"`
}

// changeFiles is what the container prints: both files, each behind a marker, and neither an error
// when it is not there — a first install has no change to report and that is not a failure.
const changeFiles = "echo '" + pendingMarker + "'; cat " + pendingFile + " 2>/dev/null; echo; echo '" + resultMarker + "'; cat " + resultFile + " 2>/dev/null"

// ReadChange asks the running container what it knows about the agent's own changes.
func ReadChange(ctx context.Context, p Paths, telegram bool) ChangeNotice {
	out, err := composeQuiet(ctx, p, telegram, "exec", "-T", "daedalus", "sh", "-c", changeFiles)
	if err != nil {
		return ChangeNotice{}
	}
	return parseChangeNotice(out)
}

// parseChangeNotice turns the two printed files into the one line the page shows. A change waiting
// for a restart wins over one that has already been decided: the decided one is history the moment
// there is a new change sitting in the checkout.
func parseChangeNotice(out string) ChangeNotice {
	pending := changeRecord(section(out, pendingMarker, resultMarker))
	if pending.Commit != "" {
		return ChangeNotice{Pending: true, Summary: pending.Summary, Commit: pending.Commit}
	}
	last := changeRecord(section(out, resultMarker, ""))
	if last.Commit == "" {
		return ChangeNotice{}
	}
	return ChangeNotice{Summary: last.Summary, Commit: last.Commit, Status: last.Status, Detail: last.Detail}
}

// section takes what lies between two markers. An absent marker yields nothing rather than the
// whole output: printing a file that is not there leaves the marker and an empty line, and reading
// the rest of the output as that file's content would invent a change out of the other one.
func section(out, from, to string) string {
	_, rest, ok := strings.Cut(out, from)
	if !ok {
		return ""
	}
	if to == "" {
		return rest
	}
	body, _, _ := strings.Cut(rest, to)
	return body
}

type changeFile struct {
	Commit  string `json:"commit"`
	Summary string `json:"summary"`
	Status  string `json:"status"`
	Detail  string `json:"detail"`
}

func changeRecord(text string) changeFile {
	var record changeFile
	if err := json.Unmarshal([]byte(strings.TrimSpace(text)), &record); err != nil {
		return changeFile{}
	}
	return record
}

// change returns what the container last said, asking it again only when the cached answer has
// aged. The page polls every two seconds and an exec into the container is not a two-second
// operation; ten seconds is well inside the time it takes anyone to read the line and click.
func (a *App) change(ctx context.Context) ChangeNotice {
	a.mu.Lock()
	cached, at := a.changeNotice, a.changeAt
	a.mu.Unlock()
	if time.Since(at) < changeMaxAge {
		return cached
	}
	notice := ReadChange(ctx, a.paths, a.Telegram())
	a.mu.Lock()
	a.changeNotice, a.changeAt = notice, time.Now()
	a.mu.Unlock()
	return notice
}
