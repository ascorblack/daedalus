// Package answer replies to terminal queries on behalf of the terminal.
//
// A program asks its terminal questions (where is the cursor, what are you, is this mode on, what
// colour is the background) and waits for the reply on its input. The daemon answers every one of
// them, whether no browser or three are watching, and every browser swallows them: a program probing
// a detached terminal would otherwise wait forever, and one probing an attached terminal would get a
// reply per browser, late from a throttled background tab, indistinguishable from typed keys.
//
// The replies are the ones xterm.js gives, because xterm.js is what draws the terminal and encodes
// the keys and the mouse: a program must not be told about a feature the browser lacks. The values
// come from the daemon's emulator. Where xterm.js reports an option instead of the state a program
// set (the cursor shape and blink, the pen), or a column past the margin, the daemon reports the
// state; testdata/xterm-replies.json records each such case with the reason.
package answer

import (
	"fmt"
	"math"
	"strconv"
	"strings"

	"github.com/ascorblack/daedalus/ptyd/internal/emulator"
	"github.com/ascorblack/daedalus/ptyd/internal/scan"
)

// XtermVersion is the xterm.js the app draws terminals with, as XTVERSION reports it. A host test
// keeps it equal to the app's @xterm/xterm version.
const XtermVersion = "6.1.0-beta.304"

// Owner is what the answerer knows about the person whose screen sets the terminal's size.
type Owner struct {
	// PxW and PxH are the pixel size of the terminal area, as that browser measured it; 0 when no
	// browser has said.
	PxW, PxH int
}

// Reply returns the reply to a query, or nil when the terminal stays silent (as xterm.js does for
// the queries it does not answer).
func Reply(q scan.Mark, e emulator.Emulator, o Owner) []byte {
	if q.Kind != scan.KindQuery {
		return nil
	}
	qr, _ := e.(emulator.Querier)
	switch q.Query {
	case scan.QueryDA1:
		// VT100 with advanced video: what xterm.js says for TERM=xterm*.
		return []byte("\x1b[?1;2c")
	case scan.QueryDA2:
		return []byte("\x1b[>0;276;0c")
	case scan.QueryXTVERSION:
		return []byte("\x1bP>|xterm.js(" + XtermVersion + ")\x1b\\")
	case scan.QueryDSR:
		return dsr(q, e, qr)
	case scan.QueryDECRQM:
		return decrqm(q, e, qr)
	case scan.QueryKitty:
		return []byte(fmt.Sprintf("\x1b[?%du", e.Modes().KittyFlags))
	case scan.QueryDECRQSS:
		return decrqss(q.Text, e, qr)
	case scan.QueryOSCColor:
		return oscColor(q, qr)
	case scan.QueryXTWINOPS:
		return xtwinops(q.Mode, e, o)
	}
	return nil
}

func dsr(q scan.Mark, e emulator.Emulator, qr emulator.Querier) []byte {
	switch {
	case q.Mode == 5 && !q.Private:
		return []byte("\x1b[0n")
	case q.Mode == 6:
		// An emulator without a screen does not know where the cursor is, and a made-up position
		// is worse than none: programs that ask time out and carry on.
		if qr == nil {
			return nil
		}
		// Absolute, whatever DECOM says, as xterm.js reports it.
		c := e.Cursor()
		cols, _ := e.Size()
		x := min(c.X+1, max(cols, 1))
		if q.Private {
			return []byte(fmt.Sprintf("\x1b[?%d;%dR", c.Y+1, x))
		}
		return []byte(fmt.Sprintf("\x1b[%d;%dR", c.Y+1, x))
	case q.Mode == 996 && q.Private:
		// Dark (1) when the background is darker than the foreground, as xterm.js decides it.
		fg, bg := emulator.DefaultTheme.Foreground, emulator.DefaultTheme.Background
		if qr != nil {
			if c, ok := qr.Color(emulator.ColorForeground, 0); ok {
				fg = c
			}
			if c, ok := qr.Color(emulator.ColorBackground, 0); ok {
				bg = c
			}
		}
		if luminance(bg) < luminance(fg) {
			return []byte("\x1b[?997;1n")
		}
		return []byte("\x1b[?997;2n")
	}
	return nil
}

// DECRPM values.
const (
	notRecognized    = 0
	set              = 1
	reset            = 2
	permanentlySet   = 3
	permanentlyReset = 4
)

func b2v(on bool) int {
	if on {
		return set
	}
	return reset
}

// decrqm answers for exactly the modes xterm.js recognises, with the values it would give, plus
// grapheme clustering (2027), which the browser's width provider implements.
func decrqm(q scan.Mark, e emulator.Emulator, qr emulator.Querier) []byte {
	m := e.Modes()
	mode := func(n int, fallback bool) bool {
		if qr != nil {
			if v, known := qr.ModeReport(n, !q.Private); known {
				return v
			}
		}
		return fallback
	}
	v := notRecognized
	if !q.Private {
		switch q.Mode {
		case 2:
			v = permanentlyReset
		case 4:
			v = b2v(m.Insert)
		case 12:
			v = permanentlySet
		case 20:
			v = b2v(mode(20, false))
		}
		return []byte(fmt.Sprintf("\x1b[%d;%d$y", q.Mode, v))
	}
	switch q.Mode {
	case 1:
		v = b2v(m.AppCursor)
	case 6:
		v = b2v(m.Origin)
	case 7:
		v = b2v(m.Wrap)
	case 8:
		v = permanentlySet
	case 9, 1000, 1002, 1003:
		v = b2v(m.Mouse.Mode == q.Mode)
	case 12:
		v = b2v(mode(12, false))
	case 25:
		v = b2v(e.Cursor().Visible)
	case 45:
		v = b2v(mode(45, false))
	case 66:
		v = b2v(m.AppKeypad)
	case 67:
		v = permanentlyReset
	case 1004:
		v = b2v(m.FocusEvents)
	case 1005, 1015:
		v = permanentlyReset
	case 1006, 1016:
		// The report format is one setting, independent of whether tracking is on.
		v = b2v(mode(q.Mode, m.Mouse.Encoding == q.Mode))
	case 1048:
		v = set
	case 47, 1047, 1049:
		v = b2v(m.AltScreen)
	case 2004:
		v = b2v(m.BracketedPaste)
	case 2026:
		v = b2v(m.SyncOutput)
	case 2027:
		if qr != nil {
			if on, known := qr.ModeReport(2027, false); known {
				v = b2v(on)
			}
		}
	}
	return []byte(fmt.Sprintf("\x1b[?%d;%d$y", q.Mode, v))
}

func decrqss(pt string, e emulator.Emulator, qr emulator.Querier) []byte {
	ok := func(s string) []byte { return []byte("\x1bP1$r" + s + "\x1b\\") }
	switch pt {
	case `"q`:
		return ok(`0"q`)
	case `"p`:
		return ok(`61;1"p`)
	case "r":
		_, rows := e.Size()
		top, bottom := 1, rows
		if qr != nil {
			top, bottom = qr.ScrollRegion()
		}
		return ok(fmt.Sprintf("%d;%dr", top, bottom))
	case "m":
		pen := "0"
		if qr != nil {
			pen = qr.Pen()
		}
		return ok(pen + "m")
	case " q":
		shape := e.Modes().CursorStyle
		if qr != nil {
			shape = qr.CursorShape()
		}
		if shape <= 0 {
			// DECSCUSR 0 is the default shape: a steady block, as the browser draws it.
			shape = 2
		}
		return ok(strconv.Itoa(shape) + " q")
	}
	return []byte("\x1bP0$r\x1b\\")
}

// oscColor answers OSC 4/10/11/12 queries, one reply per slot, as xterm.js sends them.
func oscColor(q scan.Mark, qr emulator.Querier) []byte {
	color := func(slot emulator.ColorSlot, index int) (emulator.RGB, bool) {
		if qr != nil {
			return qr.Color(slot, index)
		}
		th := emulator.DefaultTheme
		switch slot {
		case emulator.ColorForeground:
			return th.Foreground, true
		case emulator.ColorBackground:
			return th.Background, true
		case emulator.ColorCursor:
			return th.Cursor, true
		}
		if index >= 0 && index < len(th.Palette) {
			return th.Palette[index], true
		}
		return emulator.RGB{}, false
	}
	var out strings.Builder
	if q.Mode == 4 {
		slots := strings.Split(q.Text, ";")
		for i := 0; i+1 < len(slots); i += 2 {
			n, err := strconv.Atoi(slots[i])
			if err != nil || n < 0 || n > 255 || slots[i+1] != "?" {
				continue
			}
			if c, ok := color(emulator.ColorPalette, n); ok {
				fmt.Fprintf(&out, "\x1b]4;%d;%s\x1b\\", n, rgbString(c))
			}
		}
		return []byte(out.String())
	}
	// OSC 10;?;? asks for 10 and then 11: each further slot is the next of foreground, background,
	// cursor.
	special := []emulator.ColorSlot{emulator.ColorForeground, emulator.ColorBackground, emulator.ColorCursor}
	for i, first := 0, q.Mode-10; i < q.Value && first+i < len(special); i++ {
		if first+i < 0 {
			continue
		}
		if c, ok := color(special[first+i], 0); ok {
			fmt.Fprintf(&out, "\x1b]%d;%s\x1b\\", 10+first+i, rgbString(c))
		}
	}
	return []byte(out.String())
}

// rgbString is xterm.js's 16-bit form: each byte written twice.
func rgbString(c emulator.RGB) string {
	return fmt.Sprintf("rgb:%02x%02x/%02x%02x/%02x%02x", c.R, c.R, c.G, c.G, c.B, c.B)
}

// luminance is the relative luminance of WCAG 2, which xterm.js compares.
func luminance(c emulator.RGB) float64 {
	ch := func(v uint8) float64 {
		s := float64(v) / 255
		if s <= 0.03928 {
			return s / 12.92
		}
		return math.Pow((s+0.055)/1.055, 2.4)
	}
	return 0.2126*ch(c.R) + 0.7152*ch(c.G) + 0.0722*ch(c.B)
}

// xtwinops answers the size reports. xterm.js answers none of them by default; a program that asks
// for the text area is better served by the truth than by silence, and the pixel sizes are known
// once a browser has measured the terminal. The title reports (20, 21) are never answered: a title is
// something the program wrote, and echoing it back as input is a classic way to type commands into
// a shell.
func xtwinops(op int, e emulator.Emulator, o Owner) []byte {
	cols, rows := e.Size()
	switch op {
	case 14, 15:
		if o.PxW > 0 && o.PxH > 0 {
			return []byte(fmt.Sprintf("\x1b[%d;%d;%dt", op-10, o.PxH, o.PxW))
		}
	case 16:
		if o.PxW > 0 && o.PxH > 0 && cols > 0 && rows > 0 {
			return []byte(fmt.Sprintf("\x1b[6;%d;%dt", (o.PxH+rows/2)/rows, (o.PxW+cols/2)/cols))
		}
	case 18, 19:
		return []byte(fmt.Sprintf("\x1b[%d;%d;%dt", op-10, rows, cols))
	}
	return nil
}
