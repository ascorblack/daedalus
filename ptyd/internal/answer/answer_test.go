//go:build cgo

package answer

import (
	"encoding/json"
	"os"
	"testing"

	"github.com/ascorblack/daedalus/ptyd/internal/emulator"
	"github.com/ascorblack/daedalus/ptyd/internal/emulator/ghostty"
	"github.com/ascorblack/daedalus/ptyd/internal/scan"
)

type fixtureCase struct {
	Name  string `json:"name"`
	Setup string `json:"setup"`
	Query string `json:"query"`
	Cols  int    `json:"cols"`
	Rows  int    `json:"rows"`
	PxW   int    `json:"px_w"`
	PxH   int    `json:"px_h"`
	Reply string `json:"reply"`
	// Ptyd is the daemon's reply where it deliberately differs from xterm.js; Why says why.
	Ptyd *string `json:"ptyd"`
	Why  string  `json:"why"`
}

func loadFixture(t *testing.T) []fixtureCase {
	t.Helper()
	raw, err := os.ReadFile("testdata/xterm-replies.json")
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Cases []fixtureCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	if len(doc.Cases) < 100 {
		t.Fatalf("fixture has %d cases", len(doc.Cases))
	}
	return doc.Cases
}

// answerAll feeds the stream the way a terminal does: through the scanner, into the emulator up to
// each query, answering it at that point.
func answerAll(e emulator.Emulator, stream string, o Owner) string {
	sc := scan.New()
	out, marks := sc.Scan([]byte(stream))
	var replies []byte
	at := 0
	for _, m := range marks {
		if m.Offset > at {
			e.Feed(out[at:m.Offset])
			at = m.Offset
		}
		replies = append(replies, Reply(m, e, o)...)
	}
	e.Feed(out[at:])
	return string(replies)
}

// The daemon answers every query the way the browser's xterm.js would, with the recorded exceptions.
func TestRepliesMatchXtermJS(t *testing.T) {
	for _, c := range loadFixture(t) {
		t.Run(c.Name, func(t *testing.T) {
			e := ghostty.New(emulator.Options{Cols: c.Cols, Rows: c.Rows, ScrollbackLines: 100, GraphemeClusters: true})
			defer e.Close()
			answerAll(e, c.Setup, Owner{})
			got := answerAll(e, c.Query, Owner{PxW: c.PxW, PxH: c.PxH})
			want := c.Reply
			if c.Ptyd != nil {
				if c.Why == "" {
					t.Fatal("a deliberate difference must say why")
				}
				want = *c.Ptyd
			}
			if got != want {
				t.Fatalf("setup %q query %q: got %q, want %q", c.Setup, c.Query, got, want)
			}
		})
	}
}

// Replies follow the viewer's theme, and a colour the program set wins over it.
func TestColoursFollowTheTheme(t *testing.T) {
	e := ghostty.New(emulator.Options{Cols: 80, Rows: 24, ScrollbackLines: 100})
	defer e.Close()
	light := emulator.Theme{Foreground: emulator.RGB{R: 0x10, G: 0x10, B: 0x10}, Background: emulator.RGB{R: 0xfa, G: 0xfa, B: 0xfa},
		Cursor: emulator.RGB{R: 1, G: 2, B: 3}, Palette: []emulator.RGB{{R: 9, G: 9, B: 9}}}
	e.(emulator.Querier).SetTheme(light)
	if got := answerAll(e, "\x1b[?996n\x1b]11;?\x07\x1b]4;0;?;1;?\x07", Owner{}); got !=
		"\x1b[?997;2n\x1b]11;rgb:fafa/fafa/fafa\x1b\\\x1b]4;0;rgb:0909/0909/0909\x1b\\\x1b]4;1;rgb:f0f0/6262/5d5d\x1b\\" {
		t.Fatalf("light theme: %q", got)
	}
	if got := answerAll(e, "\x1b]10;#ffffff\x07\x1b]11;#000000\x07\x1b[?996n\x1b]10;?\x07", Owner{}); got !=
		"\x1b[?997;1n\x1b]10;rgb:ffff/ffff/ffff\x1b\\" {
		t.Fatalf("after the program set its colours: %q", got)
	}
}

// A mouse mode a program turned off stays off in the reports, even though the mode bits of the
// modes it passed through on the way are still set.
func TestMouseReportsAreTheEffectiveMode(t *testing.T) {
	e := ghostty.New(emulator.Options{Cols: 80, Rows: 24})
	defer e.Close()
	got := answerAll(e, "\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1003l\x1b[?1000$p\x1b[?1002$p\x1b[?1003$p", Owner{})
	if got != "\x1b[?1000;2$y\x1b[?1002;2$y\x1b[?1003;2$y" {
		t.Fatalf("%q", got)
	}
}

func TestParseColor(t *testing.T) {
	for in, want := range map[string]emulator.RGB{
		"#09090b": {R: 9, G: 9, B: 11}, "#FFF": {R: 255, G: 255, B: 255}, "#10203040": {R: 16, G: 32, B: 48},
		"rgb(1, 2, 3)": {R: 1, G: 2, B: 3}, "rgba(10,20,30,0.5)": {R: 10, G: 20, B: 30}, "rgb(100% 0% 50%)": {R: 255, B: 128},
	} {
		if got, ok := ParseColor(in); !ok || got != want {
			t.Errorf("%q: %v %v, want %v", in, got, ok, want)
		}
	}
	for _, bad := range []string{"", "red", "#12", "rgb(1,2)", "var(--fg)", "color-mix(in srgb, red, blue)"} {
		if _, ok := ParseColor(bad); ok {
			t.Errorf("%q parsed", bad)
		}
	}
	if th := Theme("#fff", "junk", ""); th.Foreground != (emulator.RGB{R: 255, G: 255, B: 255}) ||
		th.Background != emulator.DefaultTheme.Background {
		t.Errorf("theme %+v", th)
	}
}
