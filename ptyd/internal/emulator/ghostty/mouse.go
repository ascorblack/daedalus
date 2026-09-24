//go:build cgo

package ghostty

import (
	"bytes"
	"strconv"

	gh "go.mitchellh.com/libghostty"
)

// mouse recovers the mouse tracking mode and report format the terminal really uses, as DEC mode
// numbers (0 for none and for the default encoding).
//
// The mode bits cannot be read for this. They keep 1000 and 1002 set after a program moved on to
// 1003 and then turned that off, so reading bits, as the library's own formatter does, would bring
// back mouse reporting the program had ended, and every click after a reattach would type escape
// sequences into it. The library's mouse encoder is configured from the real state, so asking it to
// encode synthetic events tells the truth.
func (e *Emulator) mouse() (mode, encoding int) {
	if tracking, err := e.t.MouseTracking(); err != nil || !tracking {
		return 0, 0
	}
	enc, err := gh.NewMouseEncoder()
	if err != nil {
		return 0, 0
	}
	defer enc.Close()
	ev, err := gh.NewMouseEvent()
	if err != nil {
		return 0, 0
	}
	defer ev.Close()
	ev.SetPosition(gh.MousePosition{X: 20, Y: 20})
	try := func(action gh.MouseAction, button, pressed bool) []byte {
		enc.Reset()
		enc.SetOptFromTerminal(e.t)
		enc.SetOptSize(gh.MouseEncoderSize{ScreenWidth: 8 * 200, ScreenHeight: 16 * 100, CellWidth: 8, CellHeight: 16})
		enc.SetOptAnyButtonPressed(pressed)
		ev.SetAction(action)
		if button {
			ev.SetButton(gh.MouseButtonLeft)
		} else {
			ev.ClearButton()
		}
		out, _ := enc.Encode(ev)
		return out
	}
	press := try(gh.MouseActionPress, true, false)
	switch {
	case len(press) == 0:
		return 0, 0
	case len(try(gh.MouseActionMotion, false, false)) > 0:
		mode = 1003
	case len(try(gh.MouseActionMotion, true, true)) > 0:
		mode = 1002
	case len(try(gh.MouseActionRelease, true, false)) > 0:
		mode = 1000
	default:
		mode = 9
	}
	switch {
	case bytes.HasPrefix(press, []byte("\x1b[<")):
		// SGR reports cells; SGR-pixels reports the pixel position, 20 or 21 for a cell at 20.
		encoding = 1006
		if bytes.Contains(press, []byte(";20;20")) || bytes.Contains(press, []byte(";21;21")) {
			encoding = 1016
		}
	case bytes.HasPrefix(press, []byte("\x1b[M")):
		if len(press) > 3 && press[3] >= 0x80 {
			encoding = 1005
		}
	default:
		encoding = 1015
	}
	return mode, encoding
}

// mouseRestore turns off every mouse mode and turns on the ones in effect, replacing the stale bits
// the formatter emits.
func mouseRestore(mode, encoding int) []byte {
	var b bytes.Buffer
	b.WriteString("\x1b[?9l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1005l\x1b[?1006l\x1b[?1015l\x1b[?1016l")
	switch mode {
	case 9, 1000, 1002, 1003:
		b.WriteString("\x1b[?" + strconv.Itoa(mode) + "h")
	}
	switch encoding {
	case 1005, 1006, 1015, 1016:
		b.WriteString("\x1b[?" + strconv.Itoa(encoding) + "h")
	}
	return b.Bytes()
}
