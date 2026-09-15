package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"strings"
	"testing"
	"time"
)

func TestTheSetupPageAsksAndWrites(t *testing.T) {
	paths := setupTempInstall(t)
	server := NewServer(NewApp(paths), 0)
	if err := server.Start(); err != nil {
		t.Fatal(err)
	}
	defer server.Stop(context.Background())

	client := &http.Client{
		Timeout: 5 * time.Second,
		// The redirect to the setup page is the answer under test.
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
	resp, err := client.Get(server.URL())
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusSeeOther || resp.Header.Get("Location") != "/setup" {
		t.Fatalf("an unconfigured install landed on %d %s", resp.StatusCode, resp.Header.Get("Location"))
	}

	page := get(t, client, server.URL()+"setup")
	for _, want := range []string{"DeepSeek API key", "Telegram is optional", "Daily cap", "/assets/style.css"} {
		if !strings.Contains(page, want) {
			t.Fatalf("the setup page does not mention %q", want)
		}
	}

	form := url.Values{"deepseek": {"sk-page"}, "usd_per_day": {"11"}, "bot_token": {""}}
	posted, err := client.PostForm(server.URL()+"setup", form)
	if err != nil {
		t.Fatal(err)
	}
	posted.Body.Close()
	if posted.StatusCode != http.StatusSeeOther {
		t.Fatalf("the form answered %d", posted.StatusCode)
	}
	if err := server.WaitForSetup(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := CurrentSetup(paths); got.DeepseekKey != "sk-page" || got.USDPerDay != "11" {
		t.Fatalf("the form was not written: %+v", got)
	}

	var status struct {
		Status
		DockerMissing string `json:"docker_missing"`
	}
	if err := json.Unmarshal([]byte(get(t, client, server.URL()+"api/status")), &status); err != nil {
		t.Fatal(err)
	}
	if !status.Configured || status.Telegram {
		t.Fatalf("status reads %+v", status.Status)
	}
	if status.Docker == "" && status.DockerMissing == "" {
		t.Fatal("without Docker the page must say so")
	}
}

func TestTheStatusPageRendersOnceConfigured(t *testing.T) {
	paths := setupTempInstall(t)
	if err := WriteSetup(paths, Setup{}); err != nil {
		t.Fatal(err)
	}
	server := NewServer(NewApp(paths), 0)
	if err := server.Start(); err != nil {
		t.Fatal(err)
	}
	defer server.Stop(context.Background())
	client := &http.Client{Timeout: 30 * time.Second}
	page := get(t, client, server.URL())
	for _, want := range []string{"Open the app", "Update", "/assets/app.js", paths.Data} {
		if !strings.Contains(page, want) {
			t.Fatalf("the status page does not mention %q", want)
		}
	}
	if css := get(t, client, server.URL()+"assets/style.css"); !strings.Contains(css, "--bg") {
		t.Fatal("the stylesheet is not served")
	}
	if page := get(t, client, server.URL()+"assets/status.html"); page != "" {
		t.Fatal("the templates are served as assets")
	}
}

func get(t *testing.T, client *http.Client, address string) string {
	t.Helper()
	resp, err := client.Get(address)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusOK {
		return ""
	}
	return string(body)
}
