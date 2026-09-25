package ptyproc

import (
	"errors"
	"strings"
	"unicode/utf16"
)

// The two pieces of starting a Windows program that are text rather than system calls, kept here
// without a build constraint so the tests of every platform check them: the daemon's Windows build
// is cross-compiled, and these are the parts of it that can be proven without a Windows machine.

// CommandLine joins argv into the one string CreateProcess takes, quoted so that the C runtime's
// parser (CommandLineToArgvW) splits it back into the same words. It is the rule of Go's own
// syscall.EscapeArg: backslashes are literal except before a quote, where they are doubled, and a
// word with a space or a tab is quoted whole.
func CommandLine(argv []string) string {
	var b []byte
	for _, arg := range argv {
		if len(b) > 0 {
			b = append(b, ' ')
		}
		b = appendQuoted(b, arg)
	}
	return string(b)
}

func appendQuoted(b []byte, s string) []byte {
	if s == "" {
		return append(b, `""`...)
	}
	special := strings.ContainsAny(s, "\"\\")
	space := strings.ContainsAny(s, " \t")
	if !special && !space {
		return append(b, s...)
	}
	if space {
		b = append(b, '"')
	}
	slashes := 0
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch c {
		case '\\':
			slashes++
		case '"':
			// The backslashes before a quote are doubled, and the quote gets one of its own.
			for ; slashes > 0; slashes-- {
				b = append(b, '\\')
			}
			b = append(b, '\\')
		default:
			slashes = 0
		}
		b = append(b, c)
	}
	if space {
		// Trailing backslashes would escape the closing quote.
		for ; slashes > 0; slashes-- {
			b = append(b, '\\')
		}
		b = append(b, '"')
	}
	return b
}

// EnvBlock is env as the block CreateProcess takes with CREATE_UNICODE_ENVIRONMENT: every
// "NAME=value" in UTF-16, each ended by a NUL, the whole ended by one more. A NUL inside an entry
// would end it early and let the rest pose as a variable of its own, so such an entry is refused.
func EnvBlock(env []string) ([]uint16, error) {
	if len(env) == 0 {
		return []uint16{0, 0}, nil
	}
	var out []uint16
	for _, kv := range env {
		if strings.IndexByte(kv, 0) >= 0 {
			return nil, errors.New("an environment entry holds a NUL")
		}
		out = append(out, utf16.Encode([]rune(kv))...)
		out = append(out, 0)
	}
	return append(out, 0), nil
}
