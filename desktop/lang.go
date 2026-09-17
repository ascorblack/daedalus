package main

// The launcher speaks two languages. Which one it speaks is a property of the installation rather
// than of a page: it is asked on the first screen, written next to the data the way the mode is,
// and every page the launcher serves after that is in it. A page half in one language and half in
// the other is worse than either, so nothing is translated at the point of use — the tables in
// i18n.go hold every line of every page in both, and a missing line fails a test rather than
// showing an English sentence in a Russian page.

import (
	"os"
	"strings"
)

// Lang is one of the two. There is no "unset": a page has to be written in something, and English
// is what an installation that has never said falls back to.
type Lang string

const (
	LangEN Lang = "en"
	LangRU Lang = "ru"
)

// ParseLang reads a language the operator or a browser named. Anything unrecognised is English:
// this decides which words a page uses, and there is nothing to refuse to start over.
func ParseLang(value string) Lang {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "ru":
		return LangRU
	default:
		return LangEN
	}
}

// StoredLang is what a previous run wrote, or the empty string when this installation has never
// chosen. The empty string is meaningful here and ParseLang's default is not: it is what lets the
// first run take the browser's own preference instead of insisting on English.
func StoredLang(p Paths) string {
	return strings.ToLower(strings.TrimSpace(readFile(p.Lang)))
}

// StoreLang records the choice, so the next start opens in the same language.
func StoreLang(p Paths, lang Lang) error {
	if err := p.EnsureDirs(); err != nil {
		return err
	}
	return os.WriteFile(p.Lang, []byte(string(lang)+"\n"), 0o644)
}

// LangFor is the language a page is rendered in: what this installation chose, else what the
// browser asks for, else English. The browser's header is only a first guess — the switch in the
// corner of every page is what settles it, and that writes the file.
func LangFor(p Paths, acceptLanguage string) Lang {
	if stored := StoredLang(p); stored != "" {
		return ParseLang(stored)
	}
	return preferredLang(acceptLanguage)
}

// preferredLang picks a language out of an Accept-Language header. Only the tags are read and only
// in the order they are written: a header that names Russian before anything the launcher speaks
// asks for Russian, and everything else is English.
func preferredLang(header string) Lang {
	for _, part := range strings.Split(header, ",") {
		tag, _, _ := strings.Cut(strings.TrimSpace(part), ";")
		tag = strings.ToLower(strings.TrimSpace(tag))
		switch {
		case tag == "ru" || strings.HasPrefix(tag, "ru-"):
			return LangRU
		case tag == "en" || strings.HasPrefix(tag, "en-"):
			return LangEN
		}
	}
	return LangEN
}
