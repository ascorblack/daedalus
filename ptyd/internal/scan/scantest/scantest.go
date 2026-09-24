// Package scantest checks, independently of the scanner's own code, that a stream obeys the
// clamps. It is shared by the scanner's tests and the end-to-end tests of hostile output.
package scantest

import "fmt"

// The limits, written out again rather than imported: a check that reads the scanner's own
// constants would agree with the scanner whatever they said.
const (
	maxParam  = 10000
	maxFields = 32
	maxString = 64 << 10
)

// Verify is an independent reading of a stream: no numeric CSI parameter above maxParam, no CSI
// with more than maxFields fields, and no string payload longer than maxString. It follows how
// parsers read a stream (controls inside a CSI are executed and the digits continue).
func Verify(out []byte) error {
	const (
		g = iota
		e
		c
		s
	)
	st, val, fields, strLen := g, 0, 0, 0
	osc := false
	for i := 0; i < len(out); i++ {
		b := out[i]
		if b == 0xc2 && i+1 < len(out) && out[i+1] >= 0x80 && out[i+1] <= 0x9f {
			i++
			switch out[i] {
			case 0x9b:
				st, val, fields = c, 0, 1
			case 0x9d, 0x90, 0x98, 0x9e, 0x9f:
				st, strLen, osc = s, 0, out[i] == 0x9d
			case 0x9c:
				st = g
			}
			continue
		}
		switch st {
		case g:
			if b == 0x1b {
				st = e
			}
		case e:
			switch {
			case b == '[':
				st, val, fields = c, 0, 1
			case b == ']' || b == 'P' || b == 'X' || b == '^' || b == '_':
				st, strLen, osc = s, 0, b == ']'
			case b == 0x1b:
			default:
				st = g
			}
		case c:
			switch {
			case b >= '0' && b <= '9':
				val = min(val*10+int(b-'0'), 1<<30)
				if val > maxParam {
					return fmt.Errorf("parameter %d at byte %d", val, i)
				}
			case b == ';' || b == ':':
				fields++
				val = 0
				if fields > maxFields {
					return fmt.Errorf("%d fields at byte %d", fields, i)
				}
			case b == 0x1b:
				st = e
			case b == 0x18 || b == 0x1a:
				st = g
			case b >= 0x40 && b <= 0x7e:
				st = g
			case b < 0x20:
			default:
				val = 0
			}
		case s:
			switch {
			case b == 0x1b || b == 0x18 || b == 0x1a || b == 0x07 && osc:
				st = g
				if b == 0x1b {
					st = e
				}
			default:
				strLen++
				if strLen > maxString {
					return fmt.Errorf("string of %d bytes at byte %d", strLen, i)
				}
			}
		}
	}
	return nil
}
