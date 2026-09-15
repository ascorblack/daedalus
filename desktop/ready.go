package main

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"time"
)

// readyTimeout covers a first start on a cold machine: pulling the agent image, or building it,
// then the supervisor's own startup.
const readyTimeout = 3 * time.Minute

// AppURL is the address of the app on this machine.
func AppURL(port string) string { return "http://127.0.0.1:" + port + "/app/" }

// WaitReady polls the app until it answers 200. A redirect is followed, so /app answering with the
// index page counts. The last status seen goes into the error: an API that answers 404 means the
// container is up but the app was not built, which is a different problem from nothing listening.
func WaitReady(ctx context.Context, port string, timeout time.Duration) error {
	url := "http://127.0.0.1:" + port + "/app"
	client := &http.Client{Timeout: 5 * time.Second}
	deadline := time.Now().Add(timeout)
	last := "no answer"
	for time.Now().Before(deadline) {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
		if err != nil {
			return err
		}
		resp, err := client.Do(req)
		if err == nil {
			resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return nil
			}
			last = resp.Status
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(2 * time.Second):
		}
	}
	return fmt.Errorf("the app at %s did not come up within %s (%s)", url, timeout, last)
}

// PairingURL asks the running container for the link that signs the operator in. The state
// directory is a named Docker volume, so it is read through the container rather than from the
// host; when the file is not there the logs are read for the line the server prints at startup.
func PairingURL(ctx context.Context, p Paths, telegram bool) string {
	if out, err := compose(ctx, p, telegram, "exec", "-T", "daedalus", "cat", "/srv/state/pairing-url"); err == nil {
		if url := parsePairingURL(out); url != "" {
			return url
		}
	}
	out, err := compose(ctx, p, telegram, "logs", "--no-color", "--tail", "500", "daedalus")
	if err != nil {
		return ""
	}
	return pairingFromLogs(out)
}

// parsePairingURL takes the last address out of the file's contents. The file holds one line, but a
// truncated write or a shell banner ahead of it should not turn into a URL the browser cannot open.
func parsePairingURL(out string) string {
	found := ""
	for _, line := range strings.Split(out, "\n") {
		for _, field := range strings.Fields(line) {
			if isHTTPURL(field) {
				found = strings.TrimRight(field, ".,)")
			}
		}
	}
	return found
}

// pairingFromLogs reads the same link off the log line the server prints. The last occurrence wins:
// a restart prints a new link and the old one may already be spent.
func pairingFromLogs(out string) string {
	found := ""
	for _, line := range strings.Split(out, "\n") {
		lower := strings.ToLower(line)
		idx := strings.Index(lower, "pairing link:")
		if idx < 0 {
			continue
		}
		if url := parsePairingURL(line[idx:]); url != "" {
			found = url
		}
	}
	return found
}

func isHTTPURL(value string) bool {
	return strings.HasPrefix(value, "http://") || strings.HasPrefix(value, "https://")
}
