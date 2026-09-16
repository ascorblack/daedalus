package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"
)

// The launcher watches the stack so that the operator does not have to keep the window in front of
// them: when the agent's inbox gains something, or an agent stops and waits for an answer, the
// desktop says so. It is a poll of one endpoint the app already has — /api/status carries the
// unread count and every session's state — and not a socket, because the launcher is a small
// process that may be in the middle of a build and a missed poll costs nothing.
const watchInterval = 20 * time.Second

// tokenScript reads the token the app minted for its own API out of the state database, through the
// container, exactly as the pairing link is read: the token is generated on a first start and kept
// in the database, so this is the only place it can be had from. It is used for loopback requests
// and never written anywhere — not to the log, not to the data folder.
const tokenScript = "import json, sqlite3;" +
	"from daedalus.config import Settings;" +
	"row = sqlite3.connect(str(Settings().db_path)).execute(\"select value from kv where key = 'api_token'\").fetchone();" +
	"print(json.loads(row[0]) if row else '')"

// APIToken asks the container for it. An empty answer means the launcher cannot read the API, which
// is not a failure: it means this installation gets no desktop notifications, and the launcher says
// so once.
func APIToken(ctx context.Context, p Paths, telegram bool) string {
	out, err := composeQuiet(ctx, p, telegram, "exec", "-T", "daedalus",
		"uv", "run", "--frozen", "python", "-c", tokenScript)
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(strings.TrimSpace(out), "\n") {
		if value := strings.TrimSpace(line); value != "" {
			return value
		}
	}
	return ""
}

// stackStatus is the part of the app's own status the launcher reads.
type stackStatus struct {
	InboxUnread int `json:"inbox_unread"`
	Sessions    []struct {
		ID     string `json:"id"`
		Title  string `json:"title"`
		Status string `json:"status"`
	} `json:"sessions"`
}

// Watch raises a notification when something happens that the operator would want to know about
// while looking at something else. The first answer is only a baseline — an installation with nine
// unread entries does not announce all nine the moment the launcher starts — and everything after
// it is compared against what was there before.
func (a *App) Watch(ctx context.Context, show func(Notification)) {
	token := APIToken(ctx, a.paths, a.Telegram())
	if token == "" {
		a.log("desktop notifications are off: the launcher could not read the app's API token")
		return
	}
	base := "http://127.0.0.1:" + APIPort(a.paths)
	client := &http.Client{Timeout: 5 * time.Second}
	waiting := map[string]bool{}
	unread := -1
	for {
		status, err := fetchStatus(ctx, client, base, token)
		if err == nil {
			for _, notification := range changes(status, &unread, waiting, base) {
				show(notification)
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(watchInterval):
		}
	}
}

func fetchStatus(ctx context.Context, client *http.Client, base, token string) (stackStatus, error) {
	var status stackStatus
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, base+"/api/status", nil)
	if err != nil {
		return status, err
	}
	request.Header.Set("Authorization", "Bearer "+token)
	response, err := client.Do(request)
	if err != nil {
		return status, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return status, fmt.Errorf("the app answered %s", response.Status)
	}
	return status, json.NewDecoder(response.Body).Decode(&status)
}

// changes turns one answer into what is worth telling the operator, and updates what is known. The
// unread count is announced when it grows, never when it shrinks — reading the inbox is not news —
// and a session is announced the first time it is seen waiting and not again until it has stopped
// waiting and started again.
func changes(status stackStatus, unread *int, waiting map[string]bool, base string) []Notification {
	var out []Notification
	if *unread >= 0 && status.InboxUnread > *unread {
		out = append(out, Notification{
			Title: "Daedalus",
			Body:  inboxLine(status.InboxUnread - *unread),
			Link:  base + "/app/inbox",
		})
	}
	seeding := *unread < 0
	*unread = status.InboxUnread
	present := map[string]bool{}
	for _, session := range status.Sessions {
		if session.Status != "waiting" {
			continue
		}
		present[session.ID] = true
		if waiting[session.ID] {
			continue
		}
		waiting[session.ID] = true
		if seeding {
			continue
		}
		title := strings.TrimSpace(session.Title)
		if title == "" {
			title = "An agent"
		}
		out = append(out, Notification{
			Title: "Waiting for you",
			Body:  title + " has stopped and is waiting for an answer.",
			Link:  base + "/app/agents/" + session.ID,
		})
	}
	for id := range waiting {
		if !present[id] {
			delete(waiting, id)
		}
	}
	return out
}

func inboxLine(count int) string {
	if count == 1 {
		return "One new entry in the inbox."
	}
	return fmt.Sprintf("%d new entries in the inbox.", count)
}
