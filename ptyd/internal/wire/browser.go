package wire

import (
	"encoding/binary"
	"encoding/json"
	"fmt"
)

// The browser frames. The host relays them unchanged between a WebSocket and an attachment channel,
// so this one codec is the format on both hops. The first byte is the type; integers are big-endian.
const (
	TypeOutput   byte = 0x01 // server to client: [u64 seq][bytes]
	TypeSnapshot byte = 0x02 // server to client: [u16 cols][u16 rows][u64 seq][vt bytes]
	TypeEvent    byte = 0x03 // server to client: [json]
	TypeInput    byte = 0x10 // client to server: [bytes]
	TypeResize   byte = 0x11 // client to server: [u16 cols][u16 rows][u16 px_w][u16 px_h]
	TypeAck      byte = 0x12 // client to server: [u64 seq]
	TypeAttach   byte = 0x13 // client to server: [json]
)

// MaxSeq is the largest stream offset a frame may carry: 2^53 - 1, the largest integer a browser
// holds exactly. An offset past it would be rounded on the other side, and a rounded offset is a
// resynchronisation that goes wrong silently. No terminal writes 9 PB, so the limit costs nothing.
const MaxSeq = 1<<53 - 1

// MaxInput bounds one INPUT frame. A paste larger than this arrives as several frames, which lets
// the daemon interleave other clients' keystrokes and keeps one frame from monopolising the PTY.
const MaxInput = 32 << 10

// BrowserFrame is one decoded browser frame. Only the fields of its Type are meaningful.
type BrowserFrame struct {
	Type       byte
	Seq        uint64 // OUTPUT, SNAPSHOT, ACK
	Cols, Rows uint16 // SNAPSHOT, RESIZE
	PxW, PxH   uint16 // RESIZE
	Data       []byte // OUTPUT, SNAPSHOT, INPUT bytes; EVENT and ATTACH JSON
}

// Attach is the JSON of an ATTACH frame.
type Attach struct {
	LastSeq    uint64 `json:"lastSeq"`
	HaveState  bool   `json:"haveState"`
	ReadOnly   bool   `json:"readOnly"`
	Scrollback *int   `json:"scrollback,omitempty"`
	Theme      *Theme `json:"theme,omitempty"`
}

// Theme is the size owner's colours, which the daemon uses to answer OSC 10/11/12 queries.
type Theme struct {
	FG     string `json:"fg,omitempty"`
	BG     string `json:"bg,omitempty"`
	Cursor string `json:"cursor,omitempty"`
}

// EncodeOutput builds an OUTPUT frame.
func EncodeOutput(seq uint64, data []byte) []byte {
	b := make([]byte, 9, 9+len(data))
	b[0] = TypeOutput
	binary.BigEndian.PutUint64(b[1:9], seq)
	return append(b, data...)
}

// EncodeSnapshot builds a SNAPSHOT frame.
func EncodeSnapshot(cols, rows uint16, seq uint64, vt []byte) []byte {
	b := make([]byte, 13, 13+len(vt))
	b[0] = TypeSnapshot
	binary.BigEndian.PutUint16(b[1:3], cols)
	binary.BigEndian.PutUint16(b[3:5], rows)
	binary.BigEndian.PutUint64(b[5:13], seq)
	return append(b, vt...)
}

// EncodeEvent builds an EVENT frame from any JSON-encodable value.
func EncodeEvent(v any) ([]byte, error) {
	j, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	return append([]byte{TypeEvent}, j...), nil
}

// EncodeInput builds an INPUT frame.
func EncodeInput(data []byte) []byte { return append([]byte{TypeInput}, data...) }

// EncodeResize builds a RESIZE frame.
func EncodeResize(cols, rows, pxW, pxH uint16) []byte {
	b := make([]byte, 9)
	b[0] = TypeResize
	binary.BigEndian.PutUint16(b[1:3], cols)
	binary.BigEndian.PutUint16(b[3:5], rows)
	binary.BigEndian.PutUint16(b[5:7], pxW)
	binary.BigEndian.PutUint16(b[7:9], pxH)
	return b
}

// EncodeAck builds an ACK frame.
func EncodeAck(seq uint64) []byte {
	b := make([]byte, 9)
	b[0] = TypeAck
	binary.BigEndian.PutUint64(b[1:9], seq)
	return b
}

// EncodeAttach builds an ATTACH frame.
func EncodeAttach(a Attach) ([]byte, error) {
	j, err := json.Marshal(a)
	if err != nil {
		return nil, err
	}
	return append([]byte{TypeAttach}, j...), nil
}

// DecodeBrowser parses one browser frame. Data aliases the input.
func DecodeBrowser(b []byte) (BrowserFrame, error) {
	if len(b) == 0 {
		return BrowserFrame{}, fmt.Errorf("empty browser frame")
	}
	f := BrowserFrame{Type: b[0]}
	body := b[1:]
	switch f.Type {
	case TypeOutput, TypeAck:
		if len(body) < 8 {
			return f, fmt.Errorf("frame 0x%02x: %d bytes, want at least 8", f.Type, len(body))
		}
		f.Seq = binary.BigEndian.Uint64(body[:8])
		if f.Seq > MaxSeq {
			return f, fmt.Errorf("frame 0x%02x: sequence number %d out of range", f.Type, f.Seq)
		}
		f.Data = body[8:]
		if f.Type == TypeAck && len(f.Data) != 0 {
			return f, fmt.Errorf("ACK frame: %d trailing bytes", len(f.Data))
		}
	case TypeSnapshot:
		if len(body) < 12 {
			return f, fmt.Errorf("SNAPSHOT frame: %d bytes, want at least 12", len(body))
		}
		f.Cols = binary.BigEndian.Uint16(body[0:2])
		f.Rows = binary.BigEndian.Uint16(body[2:4])
		f.Seq = binary.BigEndian.Uint64(body[4:12])
		if f.Seq > MaxSeq {
			return f, fmt.Errorf("SNAPSHOT frame: sequence number %d out of range", f.Seq)
		}
		f.Data = body[12:]
	case TypeResize:
		if len(body) != 8 {
			return f, fmt.Errorf("RESIZE frame: %d bytes, want 8", len(body))
		}
		f.Cols = binary.BigEndian.Uint16(body[0:2])
		f.Rows = binary.BigEndian.Uint16(body[2:4])
		f.PxW = binary.BigEndian.Uint16(body[4:6])
		f.PxH = binary.BigEndian.Uint16(body[6:8])
	case TypeInput:
		if len(body) > MaxInput {
			return f, fmt.Errorf("INPUT frame: %d bytes, the limit is %d", len(body), MaxInput)
		}
		f.Data = body
	case TypeEvent:
		// An event is an object with a string type; the app refuses anything else, and so does this.
		var probe struct {
			Type *string `json:"type"`
		}
		if err := json.Unmarshal(body, &probe); err != nil {
			return f, fmt.Errorf("EVENT frame is not a JSON object")
		}
		if probe.Type == nil {
			return f, fmt.Errorf("EVENT frame has no type")
		}
		f.Data = body
	case TypeAttach:
		var probe map[string]any
		if err := json.Unmarshal(body, &probe); err != nil {
			return f, fmt.Errorf("ATTACH frame is not a JSON object")
		}
		f.Data = body
	default:
		return f, fmt.Errorf("unknown browser frame type 0x%02x", f.Type)
	}
	return f, nil
}

// DecodeAttach parses the JSON of an ATTACH frame.
func DecodeAttach(f BrowserFrame) (Attach, error) {
	var a Attach
	if f.Type != TypeAttach {
		return a, fmt.Errorf("not an ATTACH frame")
	}
	err := json.Unmarshal(f.Data, &a)
	return a, err
}
