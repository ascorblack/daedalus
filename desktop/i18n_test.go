package main

import (
	"encoding/json"
	"strings"
	"testing"
)

// A page written half in one language and half in another is the failure this prevents. Both tables
// are compared in both directions: a key added to one and forgotten in the other is a missing line
// on a real page, and it is cheaper to find here.
func TestBothLanguagesSayEverything(t *testing.T) {
	for _, pair := range []struct{ have, want Lang }{{LangEN, LangRU}, {LangRU, LangEN}} {
		for _, key := range messageKeys(pair.have) {
			line, ok := messages[pair.want][key]
			if !ok {
				t.Errorf("%q is written in %s and missing from %s", key, pair.have, pair.want)
				continue
			}
			if strings.TrimSpace(line) == "" {
				t.Errorf("%q is empty in %s", key, pair.want)
			}
		}
	}
}

// Every stage of every mode has a line, or the progress page draws a row with a key in it.
func TestEveryStepHasALine(t *testing.T) {
	for _, mode := range []Mode{ModeDocker, ModeNative} {
		for _, stage := range Stages(mode) {
			key := stageKey(Stage(stage))
			for _, lang := range []Lang{LangEN, LangRU} {
				if _, ok := messages[lang][key]; !ok {
					t.Errorf("stage %q of %s mode has no line in %s", stage, mode, lang)
				}
			}
		}
	}
}

// The table the script reads has to be the table Go renders from, and it has to be valid JSON in
// the page.
func TestTheScriptGetsTheSameTable(t *testing.T) {
	for _, lang := range []Lang{LangEN, LangRU} {
		var table map[string]string
		if err := json.Unmarshal([]byte(MessagesJSON(lang)), &table); err != nil {
			t.Fatalf("%s is not valid JSON in the page: %v", lang, err)
		}
		if len(table) != len(messages[lang]) {
			t.Fatalf("%s reaches the script with %d lines of %d", lang, len(table), len(messages[lang]))
		}
		if table["status.open"] != Translate(lang, "status.open") {
			t.Fatalf("the script's %s table disagrees with the templates'", lang)
		}
	}
}

// An unknown key is visible rather than blank, and an unknown language is English rather than
// empty: neither can happen in a build whose tests pass, and neither leaves a hole in a page.
func TestAnUnknownKeyIsVisible(t *testing.T) {
	if got := Translate(LangRU, "nothing.like.this"); got != "nothing.like.this" {
		t.Fatalf("an unknown key rendered as %q", got)
	}
	if got := Translate(Lang("de"), "status.open"); got != messages[LangEN]["status.open"] {
		t.Fatalf("an unknown language rendered as %q", got)
	}
}

func TestTheLanguageIsRememberedBesideTheData(t *testing.T) {
	paths := setupTempInstall(t)
	if got := StoredLang(paths); got != "" {
		t.Fatalf("a fresh installation has already chosen %q", got)
	}
	// Nothing chosen: the browser's own preference decides, and English is the fallback.
	if got := LangFor(paths, "ru-RU,ru;q=0.9,en;q=0.8"); got != LangRU {
		t.Fatalf("a Russian browser was answered in %q", got)
	}
	if got := LangFor(paths, "fr-FR,fr;q=0.9"); got != LangEN {
		t.Fatalf("a browser asking for neither was answered in %q", got)
	}
	if err := StoreLang(paths, LangRU); err != nil {
		t.Fatal(err)
	}
	// Chosen: the choice wins over the browser, and survives the process that made it.
	if got := LangFor(paths, "en-GB,en;q=0.9"); got != LangRU {
		t.Fatalf("the stored language lost to the browser's: %q", got)
	}
	if got := StoredLang(paths); got != "ru" {
		t.Fatalf("the language on disk reads %q", got)
	}
	if err := StoreLang(paths, LangEN); err != nil {
		t.Fatal(err)
	}
	if got := LangFor(paths, "ru-RU"); got != LangEN {
		t.Fatalf("switching back left %q in force", got)
	}
}

// A file with something unrecognisable in it is English rather than a refusal to serve a page.
func TestAnUnreadableLanguageFileIsEnglish(t *testing.T) {
	paths := setupTempInstall(t)
	if err := StoreLang(paths, Lang("klingon")); err != nil {
		t.Fatal(err)
	}
	if got := LangFor(paths, "ru"); got != LangEN {
		t.Fatalf("an unrecognisable language file gave %q", got)
	}
}
