package page

import (
	"fmt"
	"strings"
	"unicode"
	"unicode/utf8"
)

// key is what CDP needs to press one key.
type key struct {
	Key     string
	Code    string
	KeyCode int
	Text    string
}

// named are the keys the contract names.
var named = map[string]key{
	"Enter":      {"Enter", "Enter", 13, "\r"},
	"Tab":        {"Tab", "Tab", 9, ""},
	"Escape":     {"Escape", "Escape", 27, ""},
	"Backspace":  {"Backspace", "Backspace", 8, ""},
	"Delete":     {"Delete", "Delete", 46, ""},
	"Space":      {" ", "Space", 32, " "},
	"ArrowUp":    {"ArrowUp", "ArrowUp", 38, ""},
	"ArrowDown":  {"ArrowDown", "ArrowDown", 40, ""},
	"ArrowLeft":  {"ArrowLeft", "ArrowLeft", 37, ""},
	"ArrowRight": {"ArrowRight", "ArrowRight", 39, ""},
	"Home":       {"Home", "Home", 36, ""},
	"End":        {"End", "End", 35, ""},
	"PageUp":     {"PageUp", "PageUp", 33, ""},
	"PageDown":   {"PageDown", "PageDown", 34, ""},
}

// Modifier bits, as CDP counts them, and the keys that hold them.
var modifiers = map[string]struct {
	bit int
	key key
}{
	"Alt":   {1, key{"Alt", "AltLeft", 18, ""}},
	"Ctrl":  {2, key{"Control", "ControlLeft", 17, ""}},
	"Meta":  {4, key{"Meta", "MetaLeft", 91, ""}},
	"Shift": {8, key{"Shift", "ShiftLeft", 16, ""}},
}

// chord is one press: the modifiers held and the key.
type chord struct {
	mods    []string
	modBits int
	key     key
}

// parseKeys reads "Enter", "Ctrl+A", "Shift+Tab", "a", "F5".
func parseKeys(s string) (chord, error) {
	if s == "" || len(s) > 64 {
		return chord{}, fmt.Errorf("keys must name a key, such as Enter, Tab, Ctrl+A")
	}
	parts := strings.Split(s, "+")
	// "Ctrl++" is Ctrl and the plus key.
	if strings.HasSuffix(s, "++") {
		parts = append(strings.Split(strings.TrimSuffix(s, "++"), "+"), "+")
	}
	var c chord
	for _, m := range parts[:len(parts)-1] {
		mod, ok := modifiers[m]
		if !ok {
			return chord{}, fmt.Errorf("unknown modifier %q; use Ctrl, Shift, Alt or Meta", m)
		}
		c.mods = append(c.mods, m)
		c.modBits |= mod.bit
	}
	name := parts[len(parts)-1]
	if k, ok := named[name]; ok {
		c.key = k
	} else if len(name) >= 2 && name[0] == 'F' && isNumber(name[1:]) {
		n := 0
		fmt.Sscanf(name[1:], "%d", &n)
		if n < 1 || n > 12 {
			return chord{}, fmt.Errorf("function keys are F1-F12")
		}
		c.key = key{name, name, 111 + n, ""}
	} else if utf8.RuneCountInString(name) == 1 {
		r, _ := utf8.DecodeRuneInString(name)
		c.key = charKey(r, c.modBits&8 != 0)
	} else {
		return chord{}, fmt.Errorf("unknown key %q", name)
	}
	// A chord with Ctrl, Alt or Meta types nothing: it is a shortcut.
	if c.modBits&(1|2|4) != 0 {
		c.key.Text = ""
	}
	return c, nil
}

func isNumber(s string) bool {
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return s != ""
}

func charKey(r rune, shift bool) key {
	text := string(r)
	if shift {
		text = strings.ToUpper(text)
	}
	switch {
	case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z':
		u := unicode.ToUpper(r)
		return key{text, "Key" + string(u), int(u), text}
	case r >= '0' && r <= '9':
		return key{text, "Digit" + string(r), int(r), text}
	}
	return key{text, "", 0, text}
}

// printable reports whether a chord types a character into a field.
func (c chord) printable() bool { return c.key.Text != "" && c.key.Text != "\r" }
