package wire

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"io"
	"os"
	"path/filepath"
	"testing"
)

var update = flag.Bool("update", false, "rewrite testdata/socket_frames.json from the cases below")

// The socket framing between the host and the daemon, in testdata/socket_frames.json for the
// host's client to test against.
type socketGolden struct {
	Name    string `json:"name"`
	Hex     string `json:"hex"`
	Channel uint32 `json:"channel"`
	Payload string `json:"payload_hex"`
}

func socketCases() []socketGolden {
	cases := []socketGolden{
		{Name: "control frame", Channel: 0, Payload: hex.EncodeToString([]byte(`{"jsonrpc":"2.0","id":1,"method":"daemon.info"}`))},
		{Name: "close of channel 7", Channel: 7, Payload: ""},
		// The payload is a terminal OUTPUT frame, [0x01][u64 16]["hi"]; this package only frames it.
		{Name: "attachment frame carrying an OUTPUT", Channel: 0x01020304, Payload: "0100000000000000106869"},
	}
	for i := range cases {
		payload, _ := hex.DecodeString(cases[i].Payload)
		cases[i].Hex = hex.EncodeToString(AppendFrame(nil, cases[i].Channel, payload))
	}
	return cases
}

func TestSocketFrames(t *testing.T) {
	path := filepath.Join("testdata", "socket_frames.json")
	cases := socketCases()
	if *update {
		data, _ := json.MarshalIndent(cases, "", "  ")
		if err := os.WriteFile(path, append(data, '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var stored []socketGolden
	if err := json.Unmarshal(data, &stored); err != nil {
		t.Fatal(err)
	}
	if len(stored) != len(cases) {
		t.Fatalf("fixture has %d cases, the code %d; run with -update after a deliberate change", len(stored), len(cases))
	}
	for i, g := range stored {
		if g.Hex != cases[i].Hex {
			t.Errorf("%s: encoding changed", g.Name)
		}
		raw, _ := hex.DecodeString(g.Hex)
		f, err := ReadFrame(bytes.NewReader(raw))
		if err != nil || f.Channel != g.Channel || hex.EncodeToString(f.Payload) != g.Payload {
			t.Errorf("%s: decoded %d/%x %v", g.Name, f.Channel, f.Payload, err)
		}
	}
}

func TestFrameRoundTrip(t *testing.T) {
	var buf bytes.Buffer
	payloads := [][]byte{nil, []byte("x"), bytes.Repeat([]byte{0xab}, MaxPayload)}
	for i, pl := range payloads {
		if err := WriteFrame(&buf, uint32(i), pl); err != nil {
			t.Fatal(err)
		}
	}
	for i, pl := range payloads {
		f, err := ReadFrame(&buf)
		if err != nil {
			t.Fatal(err)
		}
		if f.Channel != uint32(i) || !bytes.Equal(f.Payload, pl) {
			t.Fatalf("frame %d did not survive", i)
		}
	}
	if _, err := ReadFrame(&buf); err != io.EOF {
		t.Fatalf("after the last frame: %v, want EOF", err)
	}
}

func TestFrameLimits(t *testing.T) {
	if err := WriteFrame(io.Discard, 0, make([]byte, MaxPayload+1)); !errors.Is(err, ErrFrameTooLarge) {
		t.Fatalf("oversized write: %v", err)
	}
	// A hostile length is refused before anything is allocated for it.
	if _, err := ReadFrame(bytes.NewReader([]byte{0xff, 0xff, 0xff, 0xff, 0, 0, 0, 0})); !errors.Is(err, ErrFrameTooLarge) {
		t.Fatalf("oversized read: %v", err)
	}
	if _, err := ReadFrame(bytes.NewReader([]byte{0, 0, 0, 3, 0, 0, 0})); err == nil {
		t.Fatal("a length shorter than the channel id was accepted")
	}
	if _, err := ReadFrame(bytes.NewReader([]byte{0, 0, 0, 9, 0, 0, 0, 1, 'a'})); !errors.Is(err, io.ErrUnexpectedEOF) {
		t.Fatalf("truncated frame: %v", err)
	}
}
