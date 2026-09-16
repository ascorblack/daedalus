package main

import "testing"

const bothFiles = `--- pending ---
{"repo":"bot","commit":"abc1234567","summary":"A quieter retry when the provider is busy"}

--- result ---
{"commit":"old9999999","summary":"Something earlier","status":"applied","detail":"the change is live"}
`

const resultOnly = `--- pending ---

--- result ---
{"commit":"bad5555555","summary":"A broken import","status":"preflight_failed","detail":"the checks did not pass"}
`

func TestAChangeWaitingWinsOverOneAlreadyDecided(t *testing.T) {
	notice := parseChangeNotice(bothFiles)
	if !notice.Pending {
		t.Fatalf("the pending change was not reported: %+v", notice)
	}
	if notice.Commit != "abc1234567" || notice.Summary != "A quieter retry when the provider is busy" {
		t.Fatalf("wrong change: %+v", notice)
	}
}

func TestTheLastOutcomeIsReportedWhenNothingIsWaiting(t *testing.T) {
	notice := parseChangeNotice(resultOnly)
	if notice.Pending {
		t.Fatalf("nothing is waiting, yet the page would ask for a restart: %+v", notice)
	}
	if notice.Status != "preflight_failed" || notice.Detail != "the checks did not pass" {
		t.Fatalf("wrong outcome: %+v", notice)
	}
}

func TestAFirstInstallReportsNothing(t *testing.T) {
	if notice := parseChangeNotice("--- pending ---\n\n--- result ---\n"); notice.Commit != "" || notice.Pending {
		t.Fatalf("an installation with no changes reported one: %+v", notice)
	}
	// Whatever a failed exec printed is not a change; an unmarked blob must not be read as one.
	if notice := parseChangeNotice("Error: no such container\n"); notice.Commit != "" {
		t.Fatalf("an error was read as a change: %+v", notice)
	}
}

func TestTheResultIsNotReadOutOfThePendingSection(t *testing.T) {
	// Only the pending marker is present: the result section must stay empty rather than swallow
	// everything after it, which would report a change as decided the moment it was made.
	notice := parseChangeNotice("--- pending ---\n{\"commit\":\"abc\",\"summary\":\"x\"}\n")
	if !notice.Pending || notice.Status != "" {
		t.Fatalf("the sections ran into each other: %+v", notice)
	}
}
