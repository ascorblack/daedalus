package main

import (
	"fmt"
	"strings"
)

// Notification is what the operator sees on their desktop when the installation has something for
// them: an inbox entry, or an agent that has stopped and is waiting for an answer. It is deliberately
// small — a title, a line, and the link that opens the thing it is about — because that is all every
// one of the three platforms can carry without a framework.
type Notification struct {
	Title string
	Body  string
	Link  string
}

// Notify shows one. Each platform has its own file and its own way — AppleScript, a PowerShell
// toast, notify-send — and all three go through whatever the machine already has rather than
// through anything the launcher ships. A machine with no notification daemon at all returns an
// error, which the caller treats as "notifications are not available here" and stops trying.
func Notify(n Notification) error {
	if strings.TrimSpace(n.Title) == "" {
		n.Title = "Daedalus"
	}
	return notify(n)
}

// combinedError turns a failed helper into one line that says which helper and what it printed.
func combinedError(name string, out []byte, err error) error {
	text := strings.TrimSpace(string(out))
	if text == "" {
		return fmt.Errorf("%s: %w", name, err)
	}
	return fmt.Errorf("%s: %w: %s", name, err, text)
}
