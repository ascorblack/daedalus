package main

import "os/exec"

// macOS through AppleScript, which every Mac has: no helper to install, nothing bundled. The title
// and the body are passed as arguments and read out of `argv` rather than pasted into the script,
// so an inbox entry with a quote in it is text and not syntax.
//
// A notification centre notification cannot carry a link, so the link is not passed here. What the
// operator clicks is the launcher's window, which is already showing the app.
func notify(n Notification) error {
	return exec.Command("osascript",
		"-e", "on run argv",
		"-e", "display notification (item 2 of argv) with title (item 1 of argv)",
		"-e", "end run",
		n.Title, n.Body,
	).Run()
}
