package term

import (
	"fmt"
	"strings"

	"github.com/ascorblack/daedalus/ptyd/internal/emulator"
)

// Keys that do not depend on any mode.
var plainKeys = map[string]string{
	"Enter":     "\r",
	"Tab":       "\t",
	"S-Tab":     "\x1b[Z",
	"Esc":       "\x1b",
	"Backspace": "\x7f",
	"PgUp":      "\x1b[5~",
	"PgDn":      "\x1b[6~",
	"Delete":    "\x1b[3~",
	"Insert":    "\x1b[2~",
	"F1":        "\x1bOP",
	"F2":        "\x1bOQ",
	"F3":        "\x1bOR",
	"F4":        "\x1bOS",
	"F5":        "\x1b[15~",
	"F6":        "\x1b[17~",
	"F7":        "\x1b[18~",
	"F8":        "\x1b[19~",
	"F9":        "\x1b[20~",
	"F10":       "\x1b[21~",
	"F11":       "\x1b[23~",
	"F12":       "\x1b[24~",
}

// Keys whose form follows DECCKM: CSI x normally, SS3 x when the application asked for cursor-key
// mode. Sending the wrong one makes vim, less and readline see garbage for an arrow.
var cursorKeys = map[string]byte{"Up": 'A', "Down": 'B', "Right": 'C', "Left": 'D', "Home": 'H', "End": 'F'}

// EncodeKeys turns named keys into bytes, as xterm.js would send them for the given modes.
func EncodeKeys(keys []string, m emulator.Modes) ([]byte, error) {
	var out []byte
	for _, k := range keys {
		b, err := encodeKey(k, m)
		if err != nil {
			return nil, err
		}
		out = append(out, b...)
	}
	return out, nil
}

func encodeKey(k string, m emulator.Modes) ([]byte, error) {
	if s, ok := plainKeys[k]; ok {
		return []byte(s), nil
	}
	if c, ok := cursorKeys[k]; ok {
		if m.AppCursor {
			return []byte{0x1b, 'O', c}, nil
		}
		return []byte{0x1b, '[', c}, nil
	}
	if rest, ok := strings.CutPrefix(k, "C-"); ok && len(rest) == 1 {
		c := rest[0]
		switch {
		case c >= 'a' && c <= 'z':
			return []byte{c - 'a' + 1}, nil
		case c >= '@' && c <= '_': // C-@ C-A … C-Z C-[ C-\ C-] C-^ C-_
			return []byte{c - '@'}, nil
		case c == ' ':
			return []byte{0}, nil
		case c == '?':
			return []byte{0x7f}, nil
		}
	}
	if rest, ok := strings.CutPrefix(k, "M-"); ok && rest != "" {
		// Meta sends ESC before the key, which may itself be named: M-Enter, M-b.
		if inner, err := encodeKey(rest, m); err == nil {
			return append([]byte{0x1b}, inner...), nil
		}
		if len(rest) == 1 {
			return []byte{0x1b, rest[0]}, nil
		}
	}
	return nil, fmt.Errorf("unknown key %q", k)
}

// Bracketed-paste markers. Both are removed from pasted text: an end marker inside a paste would
// end it early and let the rest run as typed commands, and a start marker has no business there.
const (
	pasteStart = "\x1b[200~"
	pasteEnd   = "\x1b[201~"
)

// EncodePaste prepares text for pasting. With bracketed paste on, it is wrapped in the markers so
// the application takes it as one paste; without, line feeds become carriage returns, which is what
// the Enter key sends.
func EncodePaste(text string, m emulator.Modes) []byte {
	if m.BracketedPaste {
		for strings.Contains(text, pasteEnd) || strings.Contains(text, pasteStart) {
			text = strings.ReplaceAll(text, pasteEnd, "")
			text = strings.ReplaceAll(text, pasteStart, "")
		}
		return []byte(pasteStart + text + pasteEnd)
	}
	text = strings.ReplaceAll(text, "\r\n", "\r")
	return []byte(strings.ReplaceAll(text, "\n", "\r"))
}
