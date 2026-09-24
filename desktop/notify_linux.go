package main

import (
	"bytes"
	"context"
	"errors"
	"os/exec"
	"strings"
	"sync"
	"time"
)

// Linux through notify-send, which is what the desktop notification daemons are spoken to with and
// what libnotify installs. It is not on every machine — a bare window manager has neither — and the
// launcher says so once and stops rather than failing repeatedly.
//
// A click is only reported to a process that is still waiting for it, so a notification with
// somewhere to go is sent with --action and --wait and its notify-send is kept in a goroutine until
// the operator clicks, dismisses, or the wait runs out. That is a process per notification on the
// screen, which is why both the number and the time are bounded.
const (
	// clickWait is how long a notification stays clickable. After it the helper is ended; the
	// notification may still be on the screen, and clicking it then does nothing.
	clickWait = 10 * time.Minute
	// clickWaiters is how many notify-send processes may wait at once. A burst beyond it is shown
	// in the plain form, which can still be read, only not clicked.
	clickWaiters = 8
)

var (
	waiters = make(chan struct{}, clickWaiters)

	actionsOnce sync.Once
	actionsOK   bool

	// failedMu guards failed: a waiting notify-send can fail after Notify returned (no daemon on the
	// session bus), and the failure is handed to the next call so the caller still stops asking.
	failedMu sync.Mutex
	failed   error
)

func notify(n Notification, open func(string)) error {
	if _, err := exec.LookPath("notify-send"); err != nil {
		return errors.New("notify-send is not installed, so the desktop has no way to be told")
	}
	failedMu.Lock()
	err := failed
	failedMu.Unlock()
	if err != nil {
		return err
	}
	clickable := n.Link != "" && notifySendHasActions()
	if clickable {
		select {
		case waiters <- struct{}{}:
		default:
			clickable = false
		}
	}
	if !clickable {
		if out, err := exec.Command("notify-send", notifySendArgs(n, false)...).CombinedOutput(); err != nil {
			return combinedError("notify-send", out, err)
		}
		return nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), clickWait)
	cmd := exec.CommandContext(ctx, "notify-send", notifySendArgs(n, true)...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Start(); err != nil {
		cancel()
		<-waiters
		return combinedError("notify-send", nil, err)
	}
	go func() {
		defer func() { <-waiters }()
		defer cancel()
		err := cmd.Wait()
		// With --wait notify-send prints the name of the action that was invoked; "default" is a
		// click on the notification itself. A dismissal prints nothing.
		if strings.TrimSpace(stdout.String()) == "default" {
			open(n.Link)
			return
		}
		if err != nil && ctx.Err() == nil {
			failedMu.Lock()
			failed = combinedError("notify-send", stderr.Bytes(), err)
			failedMu.Unlock()
		}
	}()
	return nil
}

// notifySendArgs is the argument list. "--" ends notify-send's options, so the summary and the body
// are text whatever they begin with; everything the notification carries goes after it, and the
// action label, which goes before it, has been fenced by Notify.
func notifySendArgs(n Notification, clickable bool) []string {
	args := []string{"--app-name=Daedalus", "--icon=daedalus-desktop"}
	if clickable {
		label := strings.TrimSpace(n.Action)
		if label == "" {
			label = "Open"
		}
		args = append(args, "--action=default="+label, "--wait")
	}
	args = append(args, "--", n.Title)
	if n.Body != "" {
		args = append(args, n.Body)
	}
	return args
}

// notifySendHasActions asks the installed notify-send, once, whether it knows --action and --wait.
// Both arrived in libnotify 0.7.10; an older one refuses the whole call over an unknown option, and
// that machine gets the plain form instead of no notification.
func notifySendHasActions() bool {
	actionsOnce.Do(func() {
		out, _ := exec.Command("notify-send", "--help").CombinedOutput()
		help := string(out)
		actionsOK = strings.Contains(help, "--action") && strings.Contains(help, "--wait")
	})
	return actionsOK
}
