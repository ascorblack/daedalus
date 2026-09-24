//go:build cgo

package ghostty

import (
	"strconv"
	"strings"

	gh "go.mitchellh.com/libghostty"

	"github.com/ascorblack/daedalus/ptyd/internal/emulator"
	"github.com/ascorblack/daedalus/ptyd/internal/emulator/basic"
)

// Name is reported in `daemon.info` as the emulator in use: the library and the Ghostty commit it
// was built from.
const Name = "libghostty-vt@27e8b3fa"

// Cell size handed to the library on resize. It only feeds pixel reports, which the daemon answers
// itself from what the browser measured, so any plausible value does.
const cellWidthPx, cellHeightPx = 8, 16

// Emulator is libghostty-vt behind the daemon's emulator interface. Like every emulator it belongs
// to one goroutine.
type Emulator struct {
	t  *gh.Terminal
	rs *gh.RenderState

	// Absolute rows. The library knows how many rows its history holds, not how many ever passed
	// through it, so the count is kept here: total is the number of rows that ever entered the
	// primary screen's history, and mark follows the newest of them, so that after a feed the rows
	// added since are the rows below the mark. A row keeps its absolute number (total at the time
	// it scrolled off, minus the rows after it) until the history drops it. Two things make the
	// count approximate, and both are rare: a single feed that pushes more than the whole history
	// (the mark itself is dropped, and only what is still held is counted), and reflow on resize,
	// which changes how many rows the same text takes.
	total int64
	sb    int
	mark  *gh.TrackedGridRef
}

// New returns a Ghostty emulator. Should the library fail to create a terminal (it allocates its
// first pages up front), the terminal still gets the modes-only emulator rather than none.
func New(o emulator.Options) emulator.Emulator {
	e, err := newEmulator(o)
	if err != nil {
		return basic.New(o)
	}
	return e
}

// Factory is New as an emulator.Factory.
var Factory emulator.Factory = New

func clampSize(cols, rows int) (uint16, uint16) {
	return uint16(max(1, min(cols, 65535))), uint16(max(1, min(rows, 65535)))
}

func newEmulator(o emulator.Options) (*Emulator, error) {
	cols, rows := clampSize(o.Cols, o.Rows)
	opts := []gh.TerminalOption{
		gh.WithSize(cols, rows),
		gh.WithMaxScrollbackLines(uint(max(0, o.ScrollbackLines))),
		// Explicit, always: the library's default is 10 000 bytes, which holds a page or two.
		gh.WithMaxScrollbackBytes(uint(max(1<<20, o.ScrollbackBytes))),
		// When rows are added, history comes back down into them, as xterm.js does.
		gh.WithResizePullScrollback(true),
		// A snapshot taken while a sequence is half-written carries the half, so replaying the
		// snapshot and then the stream from its offset completes it.
		gh.WithContinuationMaxBytes(64 << 10),
		gh.WithModeDefault(gh.ModeGraphemeCluster, o.GraphemeClusters),
	}
	t, err := gh.NewTerminal(opts...)
	if err != nil {
		return nil, err
	}
	rs, err := gh.NewRenderState()
	if err != nil {
		t.Close()
		return nil, err
	}
	// xterm.js's defaults: a steady block. A snapshot then only has to say something about the
	// cursor's shape when the program changed it.
	style, blink := gh.TerminalCursorStyle(gh.TerminalCursorStyleBlock), false
	_ = t.SetDefaultCursorStyle(&style)
	_ = t.SetDefaultCursorBlink(&blink)
	// No kitty graphics: the browser cannot show them, and stored images are memory no clamp bounds.
	var noImages uint64
	_ = t.SetKittyImageStorageLimit(&noImages)
	e := &Emulator{t: t, rs: rs}
	e.SetTheme(emulator.DefaultTheme)
	return e, nil
}

// Feed consumes output. The library parses anything; the daemon has already clamped it.
func (e *Emulator) Feed(p []byte) {
	if len(p) == 0 {
		return
	}
	e.t.VTWrite(p)
	e.count()
}

// count brings the absolute row count up to date. It runs only while the primary screen is active:
// the alternate screen has no history, and the primary's does not move while it is hidden, so rows
// the primary took before a switch in the same feed are counted on the way back.
func (e *Emulator) count() {
	if !e.primary() {
		return
	}
	sb := int(must(e.t.ScrollbackRows()))
	added := sb
	if e.mark != nil && e.mark.HasValue() {
		// Screen coordinates, not history ones: rows pulled back down by a resize are below the
		// history, and count negatively, which keeps every other row's number where it was.
		if p, err := e.mark.Point(gh.PointTagScreen); err == nil {
			added = sb - 1 - int(p.Y)
		}
	}
	e.total += int64(added)
	e.total = max(e.total, int64(sb))
	e.sb = sb
	if sb == 0 {
		return
	}
	at := gh.Point{Tag: gh.PointTagScreen, Y: uint32(sb - 1)}
	if e.mark == nil {
		if m, err := e.t.TrackGridRef(at); err == nil {
			e.mark = m
		}
		return
	}
	if err := e.mark.Set(e.t, at); err != nil {
		e.mark.Close()
		e.mark = nil
	}
}

func (e *Emulator) primary() bool {
	s, err := e.t.ActiveScreen()
	return err == nil && s == gh.ScreenPrimary
}

func (e *Emulator) Resize(cols, rows int) {
	c, r := clampSize(cols, rows)
	_ = e.t.Resize(c, r, cellWidthPx, cellHeightPx)
	e.count()
}

func (e *Emulator) Size() (int, int) {
	return int(must(e.t.Cols())), int(must(e.t.Rows()))
}

func (e *Emulator) Cursor() emulator.Cursor {
	y := int(must(e.t.CursorY()))
	return emulator.Cursor{
		X:       int(must(e.t.CursorX())),
		Y:       y,
		Visible: must(e.t.CursorVisible()),
		AbsRow:  e.total + int64(y),
	}
}

func (e *Emulator) on(m gh.Mode) bool {
	v, _ := e.t.Mode(m)
	return v
}

func (e *Emulator) Modes() emulator.Modes {
	mode, encoding := e.mouse()
	kitty, _ := e.t.KittyKeyboardFlags()
	return emulator.Modes{
		AltScreen:      !e.primary(),
		BracketedPaste: e.on(gh.ModeBracketedPaste),
		AppCursor:      e.on(gh.ModeDECCKM),
		AppKeypad:      e.on(gh.ModeKeypadKeys),
		Mouse:          emulator.Mouse{Mode: mode, Encoding: encoding},
		FocusEvents:    e.on(gh.ModeFocusEvent),
		KittyFlags:     int(kitty),
		CursorStyle:    e.CursorShape(),
		Origin:         e.on(gh.ModeOrigin),
		Wrap:           e.on(gh.ModeWraparound),
		Insert:         e.on(gh.ModeInsert),
		SyncOutput:     e.on(gh.ModeSyncOutput),
	}
}

func (e *Emulator) History() (int, int64) {
	return e.sb, e.total - int64(e.sb)
}

// rows maps absolute rows [from, to) onto the active screen's screen coordinates (history, then the
// visible rows). The alternate screen has no history of its own; its rows are numbered after the
// primary's history, and the primary's history cannot be read while it is hidden.
func (e *Emulator) rows(from, to int64) (y0, y1 int, ok bool) {
	base, n := e.total-int64(e.sb), int64(e.sb)
	_, rows := e.Size()
	if !e.primary() {
		base, n = e.total, 0
	}
	n += int64(rows)
	lo, hi := max(from-base, 0), min(to-base, n)
	if lo >= hi {
		return 0, 0, false
	}
	return int(lo), int(hi), true
}

// Text is the plain text of absolute rows [from, to): soft-wrapped rows joined into one line,
// trailing spaces and trailing blank rows dropped.
func (e *Emulator) Text(from, to int64) []string {
	y0, y1, ok := e.rows(from, to)
	if !ok {
		return nil
	}
	cols, _ := e.Size()
	start, err := e.t.GridRef(gh.Point{Tag: gh.PointTagScreen, Y: uint32(y0)})
	if err != nil {
		return nil
	}
	end, err := e.t.GridRef(gh.Point{Tag: gh.PointTagScreen, X: uint16(cols - 1), Y: uint32(y1 - 1)})
	if err != nil {
		return nil
	}
	f, err := gh.NewFormatter(e.t,
		gh.WithFormatterFormat(gh.FormatterFormatPlain),
		gh.WithFormatterUnwrap(true),
		gh.WithFormatterTrim(true),
		gh.WithFormatterSelection(&gh.Selection{Start: *start, End: *end}))
	if err != nil {
		return nil
	}
	defer f.Close()
	s, err := f.FormatString()
	if err != nil {
		return nil
	}
	lines := strings.Split(s, "\n")
	for i := range lines {
		lines[i] = strings.TrimRight(lines[i], " ")
	}
	for len(lines) > 0 && lines[len(lines)-1] == "" {
		lines = lines[:len(lines)-1]
	}
	return lines
}

// Runs is absolute rows [from, to) as styled runs, one slice per row, trailing blanks dropped.
func (e *Emulator) Runs(from, to int64) [][]emulator.Run {
	y0, y1, ok := e.rows(from, to)
	if !ok {
		return nil
	}
	cols, _ := e.Size()
	out := make([][]emulator.Run, 0, y1-y0)
	for y := y0; y < y1; y++ {
		out = append(out, e.rowRuns(y, cols))
	}
	return out
}

func (e *Emulator) rowRuns(y, cols int) []emulator.Run {
	runs := []emulator.Run{}
	var text strings.Builder
	var cur emulator.Run
	flush := func() {
		if text.Len() > 0 {
			cur.T = text.String()
			runs = append(runs, cur)
			text.Reset()
		}
	}
	for x := 0; x < cols; x++ {
		ref, err := e.t.GridRef(gh.Point{Tag: gh.PointTagScreen, X: uint16(x), Y: uint32(y)})
		if err != nil {
			break
		}
		cell, err := ref.Cell()
		if err != nil {
			break
		}
		if w, _ := cell.Wide(); w == gh.CellWideSpacerTail || w == gh.CellWideSpacerHead {
			continue
		}
		ch := " "
		tag, _ := cell.ContentTag()
		if cps, _ := ref.Graphemes(); len(cps) > 0 {
			var sb strings.Builder
			for _, cp := range cps {
				sb.WriteRune(rune(cp))
			}
			ch = sb.String()
		} else if cp, _ := cell.Codepoint(); cp != 0 && (tag == gh.CellContentCodepoint || tag == gh.CellContentCodepointGrapheme) {
			ch = string(rune(cp))
		}
		r := emulator.Run{}
		if st, err := ref.Style(); err == nil {
			r.FG, r.BG = runColor(st.FgColor()), runColor(st.BgColor())
			r.B, r.I, r.D, r.Inv = st.Bold(), st.Italic(), st.Faint(), st.Inverse()
			r.U = st.Underline() != gh.UnderlineNone
		}
		switch tag {
		case gh.CellContentBgColorPalette:
			p, _ := cell.ColorPalette()
			r.BG = int(p) + 1
		case gh.CellContentBgColorRGB:
			c, _ := cell.ColorRGB()
			r.BG = rgbRun(c)
		}
		if r != cur {
			flush()
			cur = r
		}
		text.WriteString(ch)
	}
	flush()
	// Trailing blanks in the default style are not content.
	for len(runs) > 0 {
		last := &runs[len(runs)-1]
		if last.BG != 0 || last.Inv || last.U {
			break
		}
		last.T = strings.TrimRight(last.T, " ")
		if last.T != "" {
			break
		}
		runs = runs[:len(runs)-1]
	}
	return runs
}

func runColor(c gh.StyleColor) int {
	switch c.Tag {
	case gh.StyleColorPalette:
		return int(c.Palette) + 1
	case gh.StyleColorRGB:
		return rgbRun(c.RGB)
	}
	return 0
}

func rgbRun(c gh.ColorRGB) int {
	return 0x1000000 | int(c.R)<<16 | int(c.G)<<8 | int(c.B)
}

func (e *Emulator) Close() {
	if e.mark != nil {
		e.mark.Close()
		e.mark = nil
	}
	e.rs.Close()
	e.t.Close()
}

// ModeReport answers DECRQM from the library's own mode table. The mouse tracking modes come from
// what the terminal would really report (see mouse); the report formats are one setting and their
// bits do not go stale. The three alternate-screen modes say whether the alternate screen is active,
// as xterm.js reports them.
func (e *Emulator) ModeReport(mode int, ansi bool) (set, known bool) {
	if !ansi {
		switch mode {
		case 9, 1000, 1002, 1003:
			m, _ := e.mouse()
			return m == mode, true
		case 47, 1047, 1049:
			return !e.primary(), true
		}
	}
	if mode < 0 || mode > 0x7fff {
		return false, false
	}
	v, err := e.t.Mode(gh.NewMode(uint16(mode), ansi))
	if err != nil {
		return false, false
	}
	return v, true
}

// Pen is the current SGR state as the parameters that set it from a reset.
func (e *Emulator) Pen() string {
	st, err := e.t.CursorStyle()
	if err != nil {
		return "0"
	}
	return sgrParams(st)
}

func sgrParams(st *gh.Style) string {
	p := []string{"0"}
	add := func(on bool, n string) {
		if on {
			p = append(p, n)
		}
	}
	add(st.Bold(), "1")
	add(st.Faint(), "2")
	add(st.Italic(), "3")
	switch st.Underline() {
	case gh.UnderlineNone:
	case gh.UnderlineSingle:
		p = append(p, "4")
	case gh.UnderlineDouble:
		p = append(p, "4:2")
	case gh.UnderlineCurly:
		p = append(p, "4:3")
	case gh.UnderlineDotted:
		p = append(p, "4:4")
	default:
		p = append(p, "4:5")
	}
	add(st.Blink(), "5")
	add(st.Inverse(), "7")
	add(st.Invisible(), "8")
	add(st.Strikethrough(), "9")
	add(st.Overline(), "53")
	color := func(c gh.StyleColor, base int, ext string) {
		switch c.Tag {
		case gh.StyleColorPalette:
			n := int(c.Palette)
			switch {
			case n < 8 && base != 0:
				p = append(p, strconv.Itoa(base+n))
			case n < 16 && base != 0:
				p = append(p, strconv.Itoa(base+60+n-8))
			default:
				p = append(p, ext+";5;"+strconv.Itoa(n))
			}
		case gh.StyleColorRGB:
			p = append(p, ext+";2;"+strconv.Itoa(int(c.RGB.R))+";"+strconv.Itoa(int(c.RGB.G))+";"+strconv.Itoa(int(c.RGB.B)))
		}
	}
	color(st.FgColor(), 30, "38")
	color(st.BgColor(), 40, "48")
	color(st.UnderlineColor(), 0, "58")
	return strings.Join(p, ";")
}

// ScrollRegion reads the margins from the formatter, the one place the library exposes them.
func (e *Emulator) ScrollRegion() (top, bottom int) {
	_, rows := e.Size()
	top, bottom = 1, rows
	ref, err := e.t.GridRef(gh.Point{Tag: gh.PointTagActive})
	if err != nil {
		return
	}
	f, err := gh.NewFormatter(e.t,
		gh.WithFormatterFormat(gh.FormatterFormatVT),
		gh.WithFormatterExtraScrollingRegion(true),
		gh.WithFormatterSelection(&gh.Selection{Start: *ref, End: *ref}))
	if err != nil {
		return
	}
	defer f.Close()
	out, err := f.Format()
	if err != nil {
		return
	}
	if t, b, ok := findDECSTBM(out); ok {
		return t, b
	}
	return
}

// findDECSTBM finds the first `CSI t ; b r` in formatter output.
func findDECSTBM(p []byte) (top, bottom int, ok bool) {
	for i := 0; i+1 < len(p); i++ {
		if p[i] != 0x1b || p[i+1] != '[' {
			continue
		}
		j := i + 2
		for j < len(p) && (p[j] >= '0' && p[j] <= '9' || p[j] == ';') {
			j++
		}
		if j < len(p) && p[j] == 'r' {
			parts := strings.Split(string(p[i+2:j]), ";")
			if len(parts) == 2 {
				t, err1 := strconv.Atoi(parts[0])
				b, err2 := strconv.Atoi(parts[1])
				if err1 == nil && err2 == nil {
					return t, b, true
				}
			}
		}
	}
	return 0, 0, false
}

// CursorShape is the DECSCUSR value in effect: 1/2 block, 3/4 underline, 5/6 bar, odd blinking.
func (e *Emulator) CursorShape() int {
	if err := e.rs.Update(e.t); err != nil {
		return 2
	}
	vs, err := e.rs.CursorVisualStyle()
	if err != nil {
		return 2
	}
	blink, _ := e.rs.CursorBlinking()
	base := 1
	switch vs {
	case gh.CursorVisualStyleUnderline:
		base = 3
	case gh.CursorVisualStyleBar:
		base = 5
	}
	if !blink {
		base++
	}
	return base
}

func (e *Emulator) Color(slot emulator.ColorSlot, index int) (emulator.RGB, bool) {
	var c *gh.ColorRGB
	var err error
	switch slot {
	case emulator.ColorPalette:
		if index < 0 || index >= gh.PaletteSize {
			return emulator.RGB{}, false
		}
		var p *gh.Palette
		if p, err = e.t.ColorPalette(); err == nil {
			c = &p[index]
		}
	case emulator.ColorForeground:
		c, err = e.t.ColorForeground()
	case emulator.ColorBackground:
		c, err = e.t.ColorBackground()
	case emulator.ColorCursor:
		c, err = e.t.ColorCursor()
	}
	if err != nil || c == nil {
		return emulator.RGB{}, false
	}
	return emulator.RGB{R: c.R, G: c.G, B: c.B}, true
}

// SetTheme makes the theme the library's default colours, so that a query is answered with what the
// person sees unless the program set a colour of its own, and the program's own colour wins.
func (e *Emulator) SetTheme(th emulator.Theme) {
	rgb := func(c emulator.RGB) *gh.ColorRGB { return &gh.ColorRGB{R: c.R, G: c.G, B: c.B} }
	_ = e.t.SetColorForeground(rgb(th.Foreground))
	_ = e.t.SetColorBackground(rgb(th.Background))
	_ = e.t.SetColorCursor(rgb(th.Cursor))
	// The 256-colour cube and grey ramp are the standard ones; the first sixteen are the app's, then
	// whatever the theme overrides.
	p := gh.DefaultPalette()
	for _, pal := range [][]emulator.RGB{emulator.DefaultTheme.Palette, th.Palette} {
		for i, c := range pal {
			if i < len(p) {
				p[i] = *rgb(c)
			}
		}
	}
	_ = e.t.SetColorPalette(&p)
}

// must drops the error of a getter that cannot fail on a live terminal; the zero value is the
// harmless answer should it ever do so.
func must[T any](v T, _ error) T { return v }

var (
	_ emulator.Emulator = (*Emulator)(nil)
	_ emulator.Querier  = (*Emulator)(nil)
)
