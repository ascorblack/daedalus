package main

import (
	"encoding/xml"
	"strings"
	"testing"
)

// A session's toast opens the session through the registered scheme; anything else is a plain toast
// whose click opens nothing, rather than PowerShell.
func TestTheWindowsToastOpensASessionByItsLink(t *testing.T) {
	session := toastXML(Notification{Title: `Done "fast" <now>`, Body: "a & b", Session: "a1b2-c3"})
	want := `<toast activationType="protocol" launch="daedalus://open/a1b2-c3"><visual><binding template="ToastGeneric">` +
		`<text>Done &#34;fast&#34; &lt;now&gt;</text><text>a &amp; b</text></binding></visual></toast>`
	if session != want {
		t.Fatalf("the toast reads\n%s\nwant\n%s", session, want)
	}
	plain := toastXML(Notification{Title: "Moved", Session: `x" launch="https://elsewhere.test`})
	if strings.Contains(plain, "activationType") || strings.Contains(plain, "launch") {
		t.Fatalf("a session that is not an id reached the toast: %s", plain)
	}
	for _, document := range []string{session, plain} {
		if err := xml.Unmarshal([]byte(document), new(struct{})); err != nil {
			t.Fatalf("the toast is not well formed: %v\n%s", err, document)
		}
	}
}
