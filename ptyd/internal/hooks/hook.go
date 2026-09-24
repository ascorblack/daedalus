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
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
)

// hookSlack is how much longer than its hold `ptyd hook` waits for the listener before giving up.
// It is shorter than Post's own slack: a CLI runs its hook in the foreground, and a listener that
// does not answer in ten seconds is not going to.
const hookSlack = 10 * time.Second

// Hook is `ptyd hook <source> [--wait-ms N]`: the command a CLI's command hook runs. The payload on
// stdin is posted to the launch's hook URL under the source's name, and a 2xx reply's body is
// printed for the CLI to read as the hook's answer.
//
// It exits 0 whatever goes wrong — no launch, a wrong token, a dead or slow listener, a bad
// argument — and says why on stderr only. Claude Code treats exit 2 from a command hook as a
// blocking error and feeds stderr back to the model as if the hook had refused the action, so the
// harness must never be able to stop a CLI's work by failing; that is what `hook-post`, which
// reports a stale launch with 2, is not safe for.
func Hook(args []string, env func(string) string, stdin io.Reader, stdout, stderr io.Writer) int {
	if err := hook(args, env, stdin, stdout); err != nil {
		fmt.Fprintln(stderr, "ptyd hook:", err)
	}
	return 0
}

func hook(args []string, env func(string) string, stdin io.Reader, stdout io.Writer) error {
	fs := flag.NewFlagSet("hook", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	wait := fs.Int64("wait-ms", 0, "hold the post up to this long for a reply")
	source := ""
	if len(args) > 0 && len(args[0]) > 0 && args[0][0] != '-' {
		source, args = args[0], args[1:]
	}
	if err := fs.Parse(args); err != nil {
		return fmt.Errorf("%w; usage: ptyd hook <source> [--wait-ms N] < body", err)
	}
	if source == "" && fs.NArg() == 1 {
		source = fs.Arg(0)
	} else if fs.NArg() > 0 {
		return errors.New("one source, then flags")
	}
	if !validName.MatchString(source) {
		return fmt.Errorf("source %q: 1 to 64 letters, digits, '.', '-' or '_'", source)
	}
	if *wait < 0 || time.Duration(*wait)*time.Millisecond > config.MaxHookHold {
		return fmt.Errorf("--wait-ms must be 0..%d", config.MaxHookHold.Milliseconds())
	}
	// Read before anything can fail on the environment, so the CLI writing the payload never meets
	// a closed pipe.
	body, err := io.ReadAll(io.LimitReader(stdin, config.HookBodyBytes+1))
	if err != nil {
		return fmt.Errorf("reading stdin: %w", err)
	}
	if len(body) > config.HookBodyBytes {
		return errors.New("the payload is larger than a megabyte; not posted")
	}
	hookURL, token := env("DAEDALUS_HOOK_URL"), env("DAEDALUS_HOOK_TOKEN")
	if hookURL == "" || token == "" {
		return errors.New("DAEDALUS_HOOK_URL and DAEDALUS_HOOK_TOKEN are not set; this is not a launch")
	}
	hold := time.Duration(*wait) * time.Millisecond
	ctx, cancel := context.WithTimeout(context.Background(), hold+hookSlack)
	defer cancel()
	status, reply, err := Post(ctx, hookURL, token, source, body, hold)
	if err != nil {
		var ue *url.Error
		if errors.As(err, &ue) {
			err = ue.Err
		}
		return err
	}
	if status < 200 || status > 299 {
		// Nothing on stdout: whatever the listener said is not an answer the CLI should act on.
		return fmt.Errorf("%d %s: %s", status, http.StatusText(status), bytes.TrimSpace(reply))
	}
	_, err = stdout.Write(reply)
	return err
}
