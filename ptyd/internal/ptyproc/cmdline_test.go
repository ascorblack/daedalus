package ptyproc

import (
	"slices"
	"testing"
	"unicode/utf16"
)

// parseCommandLine splits a command line as the C runtime of a Windows program does
// (CommandLineToArgvW, for the arguments after the first): 2n backslashes and a quote are n
// backslashes and a quote that opens or closes; 2n+1 backslashes and a quote are n backslashes and
// a literal quote; backslashes before anything else are literal.
func parseCommandLine(s string) []string {
	var args []string
	var cur []byte
	inArg, quoted := false, false
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c == '\\':
			n := 0
			for i < len(s) && s[i] == '\\' {
				n++
				i++
			}
			if i < len(s) && s[i] == '"' {
				for j := 0; j < n/2; j++ {
					cur = append(cur, '\\')
				}
				if n%2 == 1 {
					cur = append(cur, '"')
				} else {
					quoted = !quoted
				}
			} else {
				for j := 0; j < n; j++ {
					cur = append(cur, '\\')
				}
				i--
			}
			inArg = true
		case c == '"':
			quoted = !quoted
			inArg = true
		case (c == ' ' || c == '\t') && !quoted:
			if inArg {
				args = append(args, string(cur))
				cur, inArg = nil, false
			}
		default:
			cur = append(cur, c)
			inArg = true
		}
	}
	if inArg {
		args = append(args, string(cur))
	}
	return args
}

func TestACommandLineSplitsBackIntoTheSameWords(t *testing.T) {
	cases := [][]string{
		{`C:\Program Files\PowerShell\7\pwsh.exe`, "-NoLogo", "-NoExit", "-Command", `try { . ([scriptblock]::Create([IO.File]::ReadAllText('C:\data\it''s\init.ps1'))) } catch { }`},
		{"claude", "--settings", `C:\state\launches\a b\settings.json`, ""},
		{"x", `trailing\`, `trailing space\ `, `a"quote`, `back\"slash`, `\\server\share\`, `two\\"quotes"`},
		{"x", "tab\there", `"`, `\`, `\\`},
	}
	for _, argv := range cases {
		line := CommandLine(argv)
		if got := parseCommandLine(line); !slices.Equal(got, argv) {
			t.Errorf("%q\n  line %s\n  back %q", argv, line, got)
		}
	}
	if got := CommandLine([]string{"plain", "words"}); got != "plain words" {
		t.Errorf("plain words were quoted: %s", got)
	}
}

func TestAnEnvironmentBlockEndsEachEntryAndItself(t *testing.T) {
	block, err := EnvBlock([]string{"A=1", "PATH=C:\\x;D:\\y", "NAME=Дедал"})
	if err != nil {
		t.Fatal(err)
	}
	want := utf16.Encode([]rune("A=1\x00PATH=C:\\x;D:\\y\x00NAME=Дедал\x00\x00"))
	if !slices.Equal(block, want) {
		t.Errorf("block %v", block)
	}
	if empty, _ := EnvBlock(nil); !slices.Equal(empty, []uint16{0, 0}) {
		t.Errorf("empty block %v", empty)
	}
	if _, err := EnvBlock([]string{"A=1\x00B=2"}); err == nil {
		t.Error("an entry holding a NUL was accepted")
	}
}
