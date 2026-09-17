package main

import "testing"

// The failures a first run actually hits, each in the shape the program that reported it writes.
func TestFailureKeyNamesWhatWentWrong(t *testing.T) {
	for _, one := range []struct {
		failure string
		want    string
	}{
		{"uv 0.4.18 could not be downloaded: dial tcp: lookup example.invalid: no such host", "trouble.network"},
		{"Get \"https://example.invalid/x\": net/http: TLS handshake timeout", "trouble.network"},
		{"Cannot connect to the Docker daemon at unix:///var/run/docker.sock", "trouble.docker"},
		{"exec: \"docker\": executable file not found in $PATH: docker", "trouble.docker"},
		{"listen tcp 127.0.0.1:8765: bind: address already in use", "trouble.port"},
		{"write /srv/state/x: no space left on device", "trouble.disk"},
		// Nothing recognised keeps the text it had: a gap that is visible rather than one that
		// pretends to know.
		{"the supervisor refused the change", ""},
		{"", ""},
	} {
		if got := FailureKey(one.failure); got != one.want {
			t.Fatalf("%q was read as %q, want %q", one.failure, got, one.want)
		}
	}
}

// A sentence that is chosen and then not written is worse than none: the page would show the key.
func TestEveryFailureSentenceExists(t *testing.T) {
	for _, one := range failureKeys {
		for _, lang := range []Lang{LangEN, LangRU} {
			if Translate(lang, one.key) == one.key {
				t.Fatalf("%s has no line in %s", one.key, lang)
			}
		}
	}
}

// Every stage the progress page can be in has a live line, in both languages: the line is the only
// thing on that page that moves, and a missing one would show its own key.
func TestEveryStageHasALiveLine(t *testing.T) {
	for _, mode := range []Mode{ModeNative, ModeDocker} {
		for _, stage := range Stages(mode) {
			key := "live." + stage
			for _, lang := range []Lang{LangEN, LangRU} {
				if Translate(lang, key) == key {
					t.Fatalf("%s has no line in %s", key, lang)
				}
			}
		}
	}
}
