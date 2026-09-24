package main

import "os/exec"

// macOS through AppleScript, which every Mac has: no helper to install, nothing bundled. The title
// and the body are passed as arguments and read out of `argv` rather than pasted into the script,
// so a notification with a quote in it is text and not syntax.
//
// The click goes nowhere in particular, and that is a limit of the helper, not an omission: a
// notification centre notification raised by `osascript` belongs to Script Editor, carries no link
// and tells nobody it was clicked, so neither the link nor open is used. What the operator clicks
// through to is the launcher's window, which is already showing the app. Doing better needs the
// launcher to post through UserNotifications as itself, which means cgo and a signed bundle.
func notify(n Notification, _ func(string)) error {
	return exec.Command("osascript",
		"-e", "on run argv",
		"-e", "display notification (item 2 of argv) with title (item 1 of argv)",
		"-e", "end run",
		n.Title, n.Body,
	).Run()
}
