package main

import (
	"encoding/xml"
	"fmt"
	"strings"
)

// Notification is what the operator sees on their desktop when the installation has something for
// them. It is deliberately small — a title, a line, and where a click goes — because that is all
// every one of the three platforms can carry without a framework.
type Notification struct {
	Title string
	Body  string
	// Link is the address on this machine that shows the thing the notification is about, or empty
	// when there is nothing more specific than the app to open.
	Link string
	// Session is set when the thing is a conversation. Windows opens it through the daedalus:// link
	// the launcher registered, because a toast can only hand a click to a registered scheme.
	Session string
	// Action is the label of the click target where the desktop shows one as a button, in the
	// language the launcher speaks.
	Action string
}

// Notify shows one. Each platform has its own file and its own way — AppleScript, a PowerShell
// toast, notify-send — and all three go through whatever the machine already has rather than
// through anything the launcher ships. A machine with no notification daemon at all returns an
// error, which the caller treats as "notifications are not available here" and stops trying.
//
// open is called with the notification's Link when the operator clicks it, on the platforms where
// the launcher is the one told about the click (Linux). It may be called from another goroutine,
// long after Notify has returned.
func Notify(n Notification, open func(link string)) error {
	if strings.TrimSpace(n.Title) == "" {
		n.Title = "Daedalus"
	}
	// The title and the body come from the agent's own notifications, and they are handed to a
	// helper as arguments. A helper reads an argument beginning with "-" as one of its own options —
	// another -e script for osascript, another flag for notify-send — so the argument boundary would
	// be the only thing between a notification and a shell. It is not asked to be: a leading dash is
	// fenced off here, before any helper sees it.
	n.Title = fenceDash(n.Title)
	n.Body = fenceDash(n.Body)
	n.Action = fenceDash(n.Action)
	if open == nil {
		open = func(string) {}
	}
	return notify(n, open)
}

// fenceDash keeps text that starts with a dash from being read as an option by whatever shows it.
func fenceDash(text string) string {
	if strings.HasPrefix(strings.TrimSpace(text), "-") {
		return " " + strings.TrimLeft(text, " \t")
	}
	return text
}

// toastXML is the Windows toast for a notification. It lives here rather than in the Windows file so
// that the document every Windows machine is handed is tested on every machine the tests run on.
//
// A conversation gets activationType="protocol" with the daedalus:// link as its launch argument:
// Windows follows the link on a click, the registered scheme hands it to this executable, and a
// launcher that is already running shows the conversation (deeplink.go). Anything else is a plain
// toast, because the toast is raised under PowerShell's identity and a click with no protocol would
// open PowerShell, not Daedalus. The session is checked again, not trusted: it came over the network.
func toastXML(n Notification) string {
	var b strings.Builder
	if n.Session != "" && isIdentifier(n.Session) {
		b.WriteString(`<toast activationType="protocol" launch="`)
		b.WriteString(xmlText(linkScheme + "://open/" + n.Session))
		b.WriteString(`">`)
	} else {
		b.WriteString(`<toast>`)
	}
	b.WriteString(`<visual><binding template="ToastGeneric"><text>`)
	b.WriteString(xmlText(n.Title))
	b.WriteString(`</text>`)
	if n.Body != "" {
		b.WriteString(`<text>`)
		b.WriteString(xmlText(n.Body))
		b.WriteString(`</text>`)
	}
	b.WriteString(`</binding></visual></toast>`)
	return b.String()
}

// xmlText escapes text for an element or a double-quoted attribute, quotes included.
func xmlText(text string) string {
	var b strings.Builder
	_ = xml.EscapeText(&b, []byte(text))
	return b.String()
}

// combinedError turns a failed helper into one line that says which helper and what it printed.
func combinedError(name string, out []byte, err error) error {
	text := strings.TrimSpace(string(out))
	if text == "" {
		return fmt.Errorf("%s: %w", name, err)
	}
	return fmt.Errorf("%s: %w: %s", name, err, text)
}
