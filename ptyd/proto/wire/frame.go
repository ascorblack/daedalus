// Package wire is the byte format between the host and a daemon: length-prefixed frames with a
// channel id, and JSON-RPC 2.0 on channel 0. ptyd and browserd both speak it; what a channel other
// than 0 carries is each daemon's own (ptyd's terminal frames are in ptyd/internal/wire).
package wire

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
)

// MaxPayload is the largest payload of one frame. A reply larger than this (a screen with a long
// scrollback, a file read) is the method's job to page, not the framing's to stream.
const MaxPayload = 1 << 20

// MaxFrame is the largest value of the length field: the channel id plus the payload.
const MaxFrame = MaxPayload + 4

// ControlChannel carries JSON-RPC.
const ControlChannel = 0

// Frame is one unit on the socket. An empty payload on a channel other than 0 means "closed from
// this side".
type Frame struct {
	Channel uint32
	Payload []byte
}

// ErrFrameTooLarge is returned for a length field beyond MaxFrame. The connection cannot be
// resynchronised after one, so the caller closes it.
var ErrFrameTooLarge = errors.New("frame too large")

// AppendFrame appends the encoding of one frame to dst.
func AppendFrame(dst []byte, channel uint32, payload []byte) []byte {
	var head [8]byte
	binary.BigEndian.PutUint32(head[0:4], uint32(4+len(payload)))
	binary.BigEndian.PutUint32(head[4:8], channel)
	dst = append(dst, head[:]...)
	return append(dst, payload...)
}

// WriteFrame writes one frame with a single Write call, so that frames written by one goroutine are
// never interleaved on the socket.
func WriteFrame(w io.Writer, channel uint32, payload []byte) error {
	if len(payload) > MaxPayload {
		return fmt.Errorf("%w: payload of %d bytes", ErrFrameTooLarge, len(payload))
	}
	_, err := w.Write(AppendFrame(make([]byte, 0, 8+len(payload)), channel, payload))
	return err
}

// ReadFrame reads one frame. It allocates the payload after checking the length, so a hostile length
// field costs nothing.
func ReadFrame(r io.Reader) (Frame, error) {
	var head [8]byte
	if _, err := io.ReadFull(r, head[:4]); err != nil {
		return Frame{}, err
	}
	n := binary.BigEndian.Uint32(head[:4])
	if n < 4 {
		return Frame{}, fmt.Errorf("frame length %d is shorter than its channel id", n)
	}
	if n > MaxFrame {
		return Frame{}, fmt.Errorf("%w: length %d", ErrFrameTooLarge, n)
	}
	if _, err := io.ReadFull(r, head[4:8]); err != nil {
		return Frame{}, unexpected(err)
	}
	f := Frame{Channel: binary.BigEndian.Uint32(head[4:8]), Payload: make([]byte, n-4)}
	if _, err := io.ReadFull(r, f.Payload); err != nil {
		return Frame{}, unexpected(err)
	}
	return f, nil
}

// unexpected turns a clean EOF in the middle of a frame into the error it is.
func unexpected(err error) error {
	if errors.Is(err, io.EOF) {
		return io.ErrUnexpectedEOF
	}
	return err
}
