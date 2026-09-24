package main

import (
	"strings"
	"testing"
)

// A clickable notification waits for its click and names the default action; the plain form, for an
// older notify-send or a burst past the cap, is the one every notify-send understands. Either way
// the text comes after "--".
func TestNotifySendIsAskedForAClickOnlyWhenOneCanBeHeard(t *testing.T) {
	n := Notification{Title: "-Finished", Body: "the tests pass", Link: "http://127.0.0.1:8765/app/agents/s1", Action: "Open"}
	got := strings.Join(notifySendArgs(n, true), " ")
	want := "--app-name=Daedalus --icon=daedalus-desktop --action=default=Open --wait -- -Finished the tests pass"
	if got != want {
		t.Fatalf("clickable: %q\nwant       %q", got, want)
	}
	got = strings.Join(notifySendArgs(n, false), " ")
	want = "--app-name=Daedalus --icon=daedalus-desktop -- -Finished the tests pass"
	if got != want {
		t.Fatalf("plain: %q\nwant   %q", got, want)
	}
	if args := notifySendArgs(Notification{Title: "t"}, false); args[len(args)-1] != "t" || args[len(args)-2] != "--" {
		t.Fatalf("an empty body became an argument: %q", args)
	}
}
