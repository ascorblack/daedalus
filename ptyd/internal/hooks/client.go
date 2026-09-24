package hooks

import (
	"bytes"
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
)

// postSlack is how much longer than its hold a post may take: connecting, and the host's reply
// being written.
const postSlack = 30 * time.Second

// Post sends one hook post from inside a launch and returns the status and the reply body. hold is
// how long the listener should hold it for a reply; zero posts and returns at once.
func Post(ctx context.Context, hookURL, token, name string, body []byte, hold time.Duration) (int, []byte, error) {
	if !validName.MatchString(name) {
		return 0, nil, fmt.Errorf("hook name %q: letters, digits, '.', '-' or '_'", name)
	}
	target := hookURL + "/" + url.PathEscape(name)
	if hold > 0 {
		target += "?wait_ms=" + strconv.FormatInt(hold.Milliseconds(), 10)
	}
	ctx, cancel := context.WithTimeout(ctx, hold+postSlack)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, target, bytes.NewReader(body))
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	// No proxy: the listener is on loopback, and a proxy from the environment would only carry the
	// token somewhere else.
	client := &http.Client{Transport: &http.Transport{Proxy: nil}}
	resp, err := client.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	reply, err := io.ReadAll(io.LimitReader(resp.Body, config.HookBodyBytes))
	return resp.StatusCode, reply, err
}

// HookPost is `ptyd hook-post <name> [--wait-ms N]`: the body on stdin is posted to the launch's
// hook URL with its token, both from the environment, and the reply is printed. It exits 0 on a
// success, 2 when the launch is unknown or over (401, 410) and 1 on anything else — so a CLI's
// hook configuration needs one portable command and no curl.
func HookPost(args []string, env func(string) string, stdin io.Reader, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("hook-post", flag.ContinueOnError)
	fs.SetOutput(stderr)
	wait := fs.Int64("wait-ms", 0, "hold the post up to this long for a reply")
	name := ""
	// The name comes first in the documented form; flags are accepted on either side of it.
	if len(args) > 0 && len(args[0]) > 0 && args[0][0] != '-' {
		name, args = args[0], args[1:]
	}
	if err := fs.Parse(args); err != nil {
		return 1
	}
	if name == "" && fs.NArg() > 0 {
		name = fs.Arg(0)
	} else if fs.NArg() > 0 {
		fmt.Fprintln(stderr, "ptyd hook-post: one hook name, then flags")
		return 1
	}
	if name == "" {
		fmt.Fprintln(stderr, "usage: ptyd hook-post <name> [--wait-ms N] < body")
		return 1
	}
	hookURL, token := env("DAEDALUS_HOOK_URL"), env("DAEDALUS_HOOK_TOKEN")
	if hookURL == "" || token == "" {
		fmt.Fprintln(stderr, "ptyd hook-post: DAEDALUS_HOOK_URL and DAEDALUS_HOOK_TOKEN are not set; this is not a launch")
		return 1
	}
	if *wait < 0 || time.Duration(*wait)*time.Millisecond > config.MaxHookHold {
		fmt.Fprintf(stderr, "ptyd hook-post: --wait-ms must be 0..%d\n", config.MaxHookHold.Milliseconds())
		return 1
	}
	body, err := io.ReadAll(io.LimitReader(stdin, config.HookBodyBytes+1))
	if err != nil {
		fmt.Fprintln(stderr, "ptyd hook-post: reading stdin:", err)
		return 1
	}
	status, reply, err := Post(context.Background(), hookURL, token, name, body, time.Duration(*wait)*time.Millisecond)
	if err != nil {
		var ue *url.Error
		if errors.As(err, &ue) {
			err = ue.Err
		}
		fmt.Fprintln(stderr, "ptyd hook-post:", err)
		return 1
	}
	if status >= 200 && status < 300 {
		_, _ = stdout.Write(reply)
		return 0
	}
	// An error's text goes to stderr: stdout is what a CLI reads as the hook's answer.
	fmt.Fprintf(stderr, "ptyd hook-post: %d %s: %s\n", status, http.StatusText(status), bytes.TrimSpace(reply))
	if status == http.StatusUnauthorized || status == http.StatusGone {
		return 2
	}
	return 1
}
