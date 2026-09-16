package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"time"
)

// Two launchers against one installation is two processes reconciling the same compose project, two
// windows showing the same app and two answers to the same port. So the second one does not start:
// it tells the first to come to the front, hands over the link it was opened with, and exits.
//
// The handover is the file below, written by the launcher that owns the installation while its page
// is listening and removed when it stops. It names the port and carries the token that page mints
// per process, because the launcher refuses anything that changes something without it — including
// this. It is 0600: the token is the key to the buttons that start and stop the stack.
type instance struct {
	Port  int    `json:"port"`
	Token string `json:"token"`
	PID   int    `json:"pid"`
}

func instanceFile(p Paths) string { return filepath.Join(p.Data, "launcher.json") }

// WriteInstance records this launcher as the one that owns the installation.
func WriteInstance(p Paths, port int, token string) error {
	data, err := json.Marshal(instance{Port: port, Token: token, PID: os.Getpid()})
	if err != nil {
		return err
	}
	return os.WriteFile(instanceFile(p), data, 0o600)
}

// RemoveInstance gives the installation up. A launcher that is killed rather than stopped leaves
// the file behind; that costs nothing, because the file is only ever believed when the port it
// names answers with the token it carries.
func RemoveInstance(p Paths) { _ = os.Remove(instanceFile(p)) }

func readInstance(p Paths) (instance, bool) {
	data, err := os.ReadFile(instanceFile(p))
	if err != nil {
		return instance{}, false
	}
	var found instance
	if err := json.Unmarshal(data, &found); err != nil || found.Port == 0 || found.Token == "" {
		return instance{}, false
	}
	return found, true
}

// FocusRunning asks a launcher that is already running to come to the front, with the deep link this
// start was opened with when there is one. It reports whether one answered — and only a launcher of
// this installation can, since anything else listening on that port has neither the token nor the
// endpoint, and answers 403 or 404.
func FocusRunning(ctx context.Context, p Paths, link string) bool {
	found, ok := readInstance(p)
	if !ok {
		return false
	}
	body, err := json.Marshal(struct {
		URL string `json:"url"`
	}{URL: link})
	if err != nil {
		return false
	}
	url := fmt.Sprintf("http://127.0.0.1:%d/focus", found.Port)
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return false
	}
	request.Header.Set(csrfHeader, found.Token)
	request.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: 3 * time.Second}
	response, err := client.Do(request)
	if err != nil {
		return false
	}
	defer response.Body.Close()
	return response.StatusCode == http.StatusOK
}
