//go:build cgo

package ghostty

import (
	"encoding/base64"
	"encoding/json"
	"os"
	"reflect"
	"strconv"
	"strings"
	"testing"

	gh "go.mitchellh.com/libghostty"

	"github.com/ascorblack/daedalus/ptyd/internal/emulator"
)

// The app draws terminals with a width provider generated from Ghostty's own tables, and is tested
// against grids recorded from the library (testdata/ghostty-widths.json, a byte copy of the app's
// fixture, which a host test keeps identical). Replaying the same writes here pins the other side:
// if the library built with the daemon, or its settings here, ever measure differently from what the
// browser was generated from, snapshots would misplace the rest of every row with an emoji in it.
func TestWidthsMatchTheBrowsersFixture(t *testing.T) {
	raw, err := os.ReadFile("testdata/ghostty-widths.json")
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Cases []struct {
			Name   string     `json:"name"`
			Cols   int        `json:"cols"`
			Rows   int        `json:"rows"`
			Writes []string   `json:"writes"`
			Grid   [][]string `json:"grid"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	if len(doc.Cases) == 0 {
		t.Fatal("no cases")
	}
	for _, c := range doc.Cases {
		t.Run(c.Name, func(t *testing.T) {
			e := New(emulator.Options{Cols: c.Cols, Rows: c.Rows, ScrollbackLines: 100, GraphemeClusters: true}).(*Emulator)
			defer e.Close()
			for _, w := range c.Writes {
				b, err := base64.StdEncoding.DecodeString(w)
				if err != nil {
					t.Fatal(err)
				}
				e.Feed(b)
			}
			got := gridRows(e)
			if len(got) != len(c.Grid) {
				t.Fatalf("%d rows, want %d", len(got), len(c.Grid))
			}
			for i := range got {
				if !reflect.DeepEqual(got[i], c.Grid[i]) {
					t.Fatalf("row %d: got %q, want %q", i, got[i], c.Grid[i])
				}
			}
		})
	}
}

// gridRows is the screen as the fixture records it: per row, the widths of its clusters, then the
// clusters; trailing blank cells and trailing empty rows dropped.
func gridRows(e *Emulator) [][]string {
	cols, rows := e.Size()
	var out [][]string
	for y := 0; y < rows; y++ {
		type cell struct {
			ch string
			w  int
		}
		var cells []cell
		for x := 0; x < cols; x++ {
			ref, err := e.t.GridRef(gh.Point{Tag: gh.PointTagActive, X: uint16(x), Y: uint32(y)})
			if err != nil {
				break
			}
			c, _ := ref.Cell()
			w := 1
			switch wd, _ := c.Wide(); wd {
			case gh.CellWideWide:
				w = 2
			case gh.CellWideSpacerTail, gh.CellWideSpacerHead:
				w = 0
			}
			ch := " "
			if cps, _ := ref.Graphemes(); len(cps) > 0 {
				var sb strings.Builder
				for _, cp := range cps {
					sb.WriteRune(rune(cp))
				}
				ch = sb.String()
			} else if cp, _ := c.Codepoint(); cp != 0 {
				ch = string(rune(cp))
			}
			cells = append(cells, cell{ch, w})
		}
		for len(cells) > 0 && cells[len(cells)-1] == (cell{" ", 1}) {
			cells = cells[:len(cells)-1]
		}
		var widths strings.Builder
		row := []string{""}
		for _, c := range cells {
			if c.w == 0 {
				continue
			}
			widths.WriteString(strconv.Itoa(c.w))
			row = append(row, c.ch)
		}
		row[0] = widths.String()
		out = append(out, row)
	}
	for len(out) > 0 && len(out[len(out)-1]) == 1 && out[len(out)-1][0] == "" {
		out = out[:len(out)-1]
	}
	return out
}
