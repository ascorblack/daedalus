//go:build cgo

package ghostty

import (
	"bytes"
	"fmt"

	gh "go.mitchellh.com/libghostty"

	"github.com/ascorblack/daedalus/ptyd/internal/emulator"
)

// The snapshot layer. The library's VT formatter writes the screen, the pen, the modes, the scroll
// region, the tab stops and the character sets; what it gets wrong or leaves out is put right here,
// each one found by replaying snapshots of real programs into a fresh xterm.js and comparing the
// screens cell by cell:
//
//   - It drops trailing blank rows. Without them the fresh terminal scrolls fewer lines than the
//     original did, and the split between history and screen, and with it the cursor row, shifts.
//     The rows are counted and written back as line feeds (padRows).
//   - It formats only the active screen. Under an alternate screen the primary one, with its
//     history, would be lost; it is formatted from a copy of the terminal that has left the
//     alternate screen, then the alternate screen is formatted on top (cloneToPrimary).
//   - It does not write the cursor's shape (DECSCUSR) or the title.
//   - It writes the mouse mode bits, which go stale (see mouse); the modes in effect replace them.
//   - It does not write the colours a program set with OSC 4/10/11/12; the ones that differ from the
//     theme are written as the same OSC sequences.
//
// Three more defects are in the formatter itself and are fixed by the patch the library is built
// with (libghostty/patches): background-coloured rows, blanks after a styled cell, and links.
//
// Soft-wrapped rows are written unwrapped, as one long line: the receiving terminal wraps it again,
// and reflows it on its own later resizes. Written as separate lines, a wrap would become a hard line
// break forever.

// Snapshot returns VT bytes that rebuild the terminal in a freshly reset one of the same size.
func (e *Emulator) Snapshot(o emulator.SnapshotOptions) ([]byte, emulator.SnapshotInfo) {
	cols, rows := e.Size()
	var out bytes.Buffer
	var included int
	if e.primary() {
		var f formatted
		f, included = formatScreen(e.t, o.Scrollback)
		// Content first, then the rows the formatter dropped, then everything else: a scroll region
		// set before the line feeds would keep them from scrolling, and autowrap turned off before
		// the content would cut every unwrapped line at the margin.
		out.Write(f.content)
		out.Write(bytes.Repeat([]byte("\r\n"), f.pad))
		out.Write(f.pre)
		out.Write(f.post)
		out.Write(e.originCursor(f.screen))
	} else {
		if c, err := cloneToPrimary(e.t); err == nil {
			var f formatted
			f, included = formatScreen(c, o.Scrollback)
			out.Write(f.content)
			out.Write(bytes.Repeat([]byte("\r\n"), f.pad))
			// The alternate screen's switch saves the primary cursor where the primary left it.
			fmt.Fprintf(&out, "\x1b[%d;%dH", must(c.CursorY())+1, must(c.CursorX())+1)
			c.Close()
		}
		// The modes switch to the alternate screen, so here they come first. It has no history to
		// scroll, and the cursor is placed explicitly, so its dropped rows do not matter.
		f, _ := formatScreen(e.t, 0)
		out.Write(f.pre)
		out.Write(f.content)
		out.Write(f.post)
		out.Write(e.originCursor(f.screen))
	}
	if shape := e.CursorShape(); shape != 2 {
		fmt.Fprintf(&out, "\x1b[%d q", shape)
	}
	out.Write(mouseRestore(e.mouse()))
	if title, _ := e.t.Title(); title != "" {
		fmt.Fprintf(&out, "\x1b]2;%s\x07", title)
	}
	e.writeColors(&out)
	if cont, err := e.t.Continuation(); err == nil {
		out.Write(cont)
	}
	return out.Bytes(), emulator.SnapshotInfo{Cols: cols, Rows: rows, FirstAbsRow: e.total - int64(included)}
}

// formatted is one screen as the formatter writes it, cut into its parts: the modes and tab stops it
// writes before the content, the content, the scroll region, keyboard mode and directory after it,
// and the screen state (cursor, pen, link, protection, charsets) last; and the rows it dropped.
type formatted struct {
	pre, content, post, screen []byte
	pad                        int
}

// formatScreen formats t's active screen with at most scrollback lines of its history, and returns
// the parts and the number of history lines included. The formatter cannot be asked for the parts
// separately, so the terminal is formatted three times (content alone, with the modal extras, with
// everything) and the extras are cut out around the content.
func formatScreen(t *gh.Terminal, scrollback int) (formatted, int) {
	cols, rows := int(must(t.Cols())), int(must(t.Rows()))
	sb := int(must(t.ScrollbackRows()))
	included := min(max(scrollback, 0), sb)
	var sel *gh.Selection
	if included < sb {
		start, err1 := t.GridRef(gh.Point{Tag: gh.PointTagScreen, Y: uint32(sb - included)})
		end, err2 := t.GridRef(gh.Point{Tag: gh.PointTagScreen, X: uint16(cols - 1), Y: uint32(sb + rows - 1)})
		if err1 == nil && err2 == nil {
			sel = &gh.Selection{Start: *start, End: *end}
		} else {
			included = sb
		}
	}
	f := formatted{pad: padRows(t, sb-included, sb+rows), content: vtFormat(t, sel, false, false)}
	modal := vtFormat(t, sel, true, false)
	full := vtFormat(t, sel, true, true)
	// The content starts right after the tab stops, which always end by homing the cursor.
	at := -1
	if p := bytes.Index(modal, []byte("\x1b[3g")); p >= 0 {
		if h := bytes.Index(modal[p:], []byte("\x1b[H")); h >= 0 {
			at = p + h + len("\x1b[H")
		}
	}
	if at < 0 || !bytes.HasPrefix(modal[at:], f.content) || !bytes.HasPrefix(full, modal) {
		// Never seen; were it to happen, the dropped rows are better lost than the modes, the
		// cursor and the pen.
		return formatted{content: full}, included
	}
	f.pre = modal[:at]
	f.post = modal[at+len(f.content):]
	f.screen = full[len(modal):]
	return f, included
}

// originCursor corrects the cursor position the formatter writes when origin mode is on: it writes
// the position from the top of the screen, and under origin mode the receiving terminal reads it
// from the top of the scroll region.
func (e *Emulator) originCursor(screen []byte) []byte {
	if !e.on(gh.ModeOrigin) || !bytes.HasPrefix(screen, []byte("\x1b[")) {
		return screen
	}
	end := bytes.IndexByte(screen, 'H')
	if end < 0 {
		return screen
	}
	var y, x int
	if n, err := fmt.Sscanf(string(screen[2:end]), "%d;%d", &y, &x); err != nil || n != 2 {
		return screen
	}
	top, _ := e.ScrollRegion()
	fixed := fmt.Sprintf("\x1b[%d;%dH", max(1, y-top+1), x)
	return append([]byte(fixed), screen[end+1:]...)
}

// vtFormat runs the library's VT formatter over t (or the selection of it).
func vtFormat(t *gh.Terminal, sel *gh.Selection, extras, screenExtras bool) []byte {
	opts := []gh.FormatterOption{
		gh.WithFormatterFormat(gh.FormatterFormatVT),
		gh.WithFormatterUnwrap(true),
	}
	if sel != nil {
		opts = append(opts, gh.WithFormatterSelection(sel))
	}
	if extras {
		opts = append(opts,
			// The palette is the viewer's theme, not the program's; what the program changed is
			// written by writeColors.
			gh.WithFormatterExtraPalette(false),
			gh.WithFormatterExtraModes(true),
			gh.WithFormatterExtraScrollingRegion(true),
			gh.WithFormatterExtraTabstops(true),
			gh.WithFormatterExtraPwd(true),
			gh.WithFormatterExtraKeyboard(true))
	}
	if screenExtras {
		opts = append(opts,
			gh.WithFormatterExtraCursor(true),
			gh.WithFormatterExtraStyle(true),
			gh.WithFormatterExtraHyperlink(true),
			gh.WithFormatterExtraProtection(true),
			gh.WithFormatterExtraKittyKeyboard(true),
			gh.WithFormatterExtraCharsets(true))
	}
	f, err := gh.NewFormatter(t, opts...)
	if err != nil {
		return nil
	}
	defer f.Close()
	b, err := f.Format()
	if err != nil {
		return nil
	}
	return b
}

// cloneToPrimary returns a copy of t, made through the library's binary snapshot, that has left the
// alternate screen, because the formatter can only format the active screen.
func cloneToPrimary(t *gh.Terminal) (*gh.Terminal, error) {
	snap, err := t.Snapshot()
	if err != nil {
		return nil, err
	}
	dec, err := gh.NewSnapshotDecoderBytesCopy(snap)
	if err != nil {
		return nil, err
	}
	defer dec.Close()
	c, err := dec.Decode()
	if err != nil {
		return nil, err
	}
	on := func(m gh.Mode) bool { v, _ := c.Mode(m); return v }
	switch {
	case on(gh.ModeAltScreenSave):
		c.VTWrite([]byte("\x1b[?1049l"))
	case on(gh.ModeAltScreen):
		c.VTWrite([]byte("\x1b[?1047l"))
	default:
		c.VTWrite([]byte("\x1b[?47l"))
	}
	return c, nil
}

// padRows is how many line feeds must follow the formatter's content over screen rows [y0, y1): the
// formatter drops trailing blank rows, and each is a line the fresh terminal must still scroll
// through. When every row is blank the formatter writes nothing, and the first row exists before
// any line feed, so one fewer is needed.
func padRows(t *gh.Terminal, y0, y1 int) int {
	cols := int(must(t.Cols()))
	n := 0
	for y := y1 - 1; y >= y0; y-- {
		if !rowBlank(t, y, cols) {
			return n
		}
		n++
	}
	return max(0, n-1)
}

// rowBlank is the formatter's own notion of a blank row (with the patch): no text, no styling, no
// background colour.
func rowBlank(t *gh.Terminal, y, cols int) bool {
	for x := 0; x < cols; x++ {
		ref, err := t.GridRef(gh.Point{Tag: gh.PointTagScreen, X: uint16(x), Y: uint32(y)})
		if err != nil {
			continue
		}
		c, err := ref.Cell()
		if err != nil {
			continue
		}
		if txt, _ := c.HasText(); txt {
			return false
		}
		if st, _ := c.HasStyling(); st {
			return false
		}
		if tag, _ := c.ContentTag(); tag == gh.CellContentBgColorPalette || tag == gh.CellContentBgColorRGB {
			return false
		}
	}
	return true
}

// writeColors writes the colours the program set that differ from the theme.
func (e *Emulator) writeColors(out *bytes.Buffer) {
	rgb := func(c gh.ColorRGB) string {
		return fmt.Sprintf("rgb:%02x/%02x/%02x", c.R, c.G, c.B)
	}
	if cur, err := e.t.ColorPalette(); err == nil {
		if def, err := e.t.ColorPaletteDefault(); err == nil {
			for i := range cur {
				if cur[i] != def[i] {
					fmt.Fprintf(out, "\x1b]4;%d;%s\x1b\\", i, rgb(cur[i]))
				}
			}
		}
	}
	dyn := []struct {
		n        int
		cur, def func() (*gh.ColorRGB, error)
	}{
		{10, e.t.ColorForeground, e.t.ColorForegroundDefault},
		{11, e.t.ColorBackground, e.t.ColorBackgroundDefault},
		{12, e.t.ColorCursor, e.t.ColorCursorDefault},
	}
	for _, d := range dyn {
		cur, err1 := d.cur()
		def, err2 := d.def()
		if err1 == nil && err2 == nil && cur != nil && def != nil && *cur != *def {
			fmt.Fprintf(out, "\x1b]%d;%s\x1b\\", d.n, rgb(*cur))
		}
	}
}
