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
	// The title and the body come from the agent's own inbox and from any page in the web view, and
	// they are handed to a helper as arguments. A helper reads an argument beginning with "-" as one
	// of its own options — another -e script for osascript, another flag for notify-send — so the
	// argument boundary would be the only thing between an inbox entry and a shell. It is not asked
	// to be: a leading dash is fenced off here, before any helper sees it.
	n.Title = fenceDash(n.Title)
	n.Body = fenceDash(n.Body)
	return notify(n)
}

// fenceDash keeps text that starts with a dash from being read as an option by whatever shows it.
func fenceDash(text string) string {
	if strings.HasPrefix(strings.TrimSpace(text), "-") {
		return " " + strings.TrimLeft(text, " \t")
	}
	return text
}

// combinedError turns a failed helper into one line that says which helper and what it printed.
func combinedError(name string, out []byte, err error) error {
	text := strings.TrimSpace(string(out))
	if text == "" {
		return fmt.Errorf("%s: %w", name, err)
	}
	return fmt.Errorf("%s: %w: %s", name, err, text)
}
