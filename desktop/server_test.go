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
	for _, want := range []string{"DeepSeek", messages[LangEN]["setup.cap.field"], messages[LangEN]["setup.submit"], "/assets/style.css"} {
		if !strings.Contains(page, want) {
			t.Fatalf("the setup page does not mention %q", want)
		}
	}
	// The same page in the other language, and not a word of the first one left in it.
	russian := get(t, client, server.URL()+"setup", "Accept-Language", "ru-RU,ru;q=0.9")
	if !strings.Contains(russian, messages[LangRU]["setup.submit"]) || strings.Contains(russian, messages[LangEN]["setup.submit"]) {
		t.Fatal("the setup page does not answer a Russian browser in Russian")
	}

	if !strings.Contains(page, server.csrf) {
		t.Fatal("the form carries no token, so nothing could ever be posted to it")
	}
	form := url.Values{"deepseek": {"sk-page"}, "usd_per_day": {"11"}, "bot_token": {""}, "lang": {"ru"}, "csrf": {server.csrf}}
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
	// The language the questions were answered in is the installation's from then on.
	if got := LangFor(paths, "en-GB"); got != LangRU {
		t.Fatalf("the form was answered in Russian and the installation speaks %q", got)
	}

	var status struct {
		Status
		DockerMissing bool `json:"docker_missing"`
	}
	if err := json.Unmarshal([]byte(get(t, client, server.URL()+"api/status")), &status); err != nil {
		t.Fatal(err)
	}
	if !status.Configured || status.Telegram {
		t.Fatalf("status reads %+v", status.Status)
	}
	if status.Docker == "" && !status.DockerMissing {
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
	// An installation that has never been brought up lands on the waiting page, which draws the
	// steps of the mode it is in.
	waiting := get(t, client, server.URL())
	for _, want := range []string{messages[LangEN]["progress.title"], `data-stage="images"`, `data-stage="checkouts"`, server.csrf} {
		if !strings.Contains(waiting, want) {
			t.Fatalf("the waiting page does not mention %q", want)
		}
	}
	if strings.Contains(waiting, `data-stage="environment"`) {
		t.Fatal("the waiting page draws a Docker installation a step that belongs to a native one")
	}
	page := get(t, client, server.URL()+"status")
	for _, want := range []string{messages[LangEN]["status.open"], messages[LangEN]["status.update"], "/assets/app.js", paths.Data, server.csrf} {
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

func get(t *testing.T, client *http.Client, address string, headers ...string) string {
	t.Helper()
	req, err := http.NewRequest(http.MethodGet, address, nil)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i+1 < len(headers); i += 2 {
		req.Header.Set(headers[i], headers[i+1])
	}
	resp, err := client.Do(req)
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

// The language is a change to the installation, so it is refused from anywhere but this page — and
// once it is accepted every page is served in it, whatever the browser asks for.
func TestTheLanguageSwitchIsTheLaunchersOwn(t *testing.T) {
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
		{"no token", nil},
		{"the wrong token", map[string]string{csrfHeader: "not-the-token"}},
		{"the right token from another site", map[string]string{csrfHeader: server.csrf, "Sec-Fetch-Site": "cross-site"}},
	} {
		if got := postBody(t, server.URL()+"api/lang", refused.headers, `{"lang":"ru"}`); got != http.StatusForbidden {
			t.Fatalf("a language change with %s answered %d, want %d", refused.name, got, http.StatusForbidden)
		}
	}
	if got := StoredLang(paths); got != "" {
		t.Fatalf("a refused request still wrote %q", got)
	}
	if got := postBody(t, server.URL()+"api/lang", map[string]string{csrfHeader: server.csrf}, `{"lang":"ru"}`); got != http.StatusOK {
		t.Fatalf("the page's own request answered %d", got)
	}
	if got := StoredLang(paths); got != "ru" {
		t.Fatalf("the choice was not written: %q", got)
	}
	client := &http.Client{Timeout: 30 * time.Second}
	page := get(t, client, server.URL()+"status", "Accept-Language", "en-GB,en;q=0.9")
	if !strings.Contains(page, messages[LangRU]["status.open"]) {
		t.Fatal("the installation's own language lost to the browser's")
	}
}

func postBody(t *testing.T, address string, headers map[string]string, body string) int {
	t.Helper()
	req, err := http.NewRequest(http.MethodPost, address, strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	for name, value := range headers {
		req.Header.Set(name, value)
	}
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	_, _ = io.ReadAll(resp.Body)
	return resp.StatusCode
}
