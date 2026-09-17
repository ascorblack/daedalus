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

	if !strings.Contains(page, server.csrf) {
		t.Fatal("the form carries no token, so nothing could ever be posted to it")
	}
	form := url.Values{"deepseek": {"sk-page"}, "usd_per_day": {"11"}, "bot_token": {""}, "csrf": {server.csrf}}
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
	if err := WriteSetup(paths, Setup{}, ModeDocker); err != nil {
		t.Fatal(err)
	}
	server := NewServer(NewApp(paths), 0)
	if err := server.Start(); err != nil {
		t.Fatal(err)
	}
	defer server.Stop(context.Background())
	client := &http.Client{Timeout: 30 * time.Second}
	page := get(t, client, server.URL())
	for _, want := range []string{"Open the app", "Update", "/assets/app.js", paths.Data, server.csrf} {
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

// The launcher listens on the loopback address, which every page in the operator's browser can
// reach as well. A form post that rewrites the keys and the Telegram identity, and an action that
// restarts the stack under them, are both requests a browser sends cross-site without asking.
func TestTheSetupFormRefusesWhatAnotherPageCouldSend(t *testing.T) {
	paths := setupTempInstall(t)
	if err := WriteSetup(paths, Setup{DeepseekKey: "sk-mine", BotToken: "123:mine", OwnerID: "1"}, ModeDocker); err != nil {
		t.Fatal(err)
	}
	server := NewServer(NewApp(paths), 0)
	if err := server.Start(); err != nil {
		t.Fatal(err)
	}
	defer server.Stop(context.Background())

	attack := url.Values{"bot_token": {"666:theirs"}, "owner_id": {"666"}, "csrf": {server.csrf}}
	for _, refused := range []struct {
		name    string
		headers map[string]string
		form    url.Values
	}{
		{"a browser that says the request is cross-site", map[string]string{"Sec-Fetch-Site": "cross-site"}, attack},
		{"an origin that is not this page", map[string]string{"Origin": "http://evil.example"}, attack},
		{"an origin on the right host but the wrong port", map[string]string{"Origin": "http://127.0.0.1:1"}, attack},
		{"a form with no token at all", nil, url.Values{"bot_token": {"666:theirs"}, "owner_id": {"666"}}},
		{"a form with the wrong token", nil, url.Values{"bot_token": {"666:theirs"}, "csrf": {"not-the-token"}}},
	} {
		resp := post(t, server.URL()+"setup", refused.headers, refused.form)
		if resp != http.StatusForbidden {
			t.Fatalf("%s answered %d, want %d", refused.name, resp, http.StatusForbidden)
		}
	}
	if got := CurrentSetup(paths); got.BotToken != "123:mine" || got.OwnerID != "1" || got.DeepseekKey != "sk-mine" {
		t.Fatalf("a refused form still rewrote the configuration: %+v", got)
	}
}

func TestAnActionNeedsTheLauncherOwnHeader(t *testing.T) {
	paths := setupTempInstall(t)
	if err := WriteSetup(paths, Setup{}, ModeDocker); err != nil {
		t.Fatal(err)
	}
	server := NewServer(NewApp(paths), 0)
	if err := server.Start(); err != nil {
		t.Fatal(err)
	}
	defer server.Stop(context.Background())

	for _, refused := range []struct {
		name    string
		headers map[string]string
	}{
		{"no header", nil},
		{"the wrong token", map[string]string{csrfHeader: "not-the-token"}},
		{"the right token from another site", map[string]string{csrfHeader: server.csrf, "Sec-Fetch-Site": "cross-site"}},
	} {
		if got := post(t, server.URL()+"api/action/start", refused.headers, nil); got != http.StatusForbidden {
			t.Fatalf("an action with %s answered %d, want %d", refused.name, got, http.StatusForbidden)
		}
	}
	// A name that is not an action proves the token was accepted without anything being started.
	if got := post(t, server.URL()+"api/action/nonsense", map[string]string{csrfHeader: server.csrf}, nil); got != http.StatusNotFound {
		t.Fatalf("the page's own request answered %d, want %d", got, http.StatusNotFound)
	}
}

func post(t *testing.T, address string, headers map[string]string, form url.Values) int {
	t.Helper()
	body := ""
	if form != nil {
		body = form.Encode()
	}
	req, err := http.NewRequest(http.MethodPost, address, strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	for name, value := range headers {
		req.Header.Set(name, value)
	}
	client := &http.Client{
		Timeout:       10 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
	resp, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	_, _ = io.ReadAll(resp.Body)
	return resp.StatusCode
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
