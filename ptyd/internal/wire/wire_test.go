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

// sharedFrames is testdata/frames.json: the browser frames, written independently of both codecs
// and held byte for byte identical by the app (miniapp/src/terminal/testdata/frames.json) and by
// this module. A host test compares the two copies; this test holds the codec to them.
type sharedFrames struct {
	Frames []struct {
		Name      string          `json:"name"`
		Direction string          `json:"direction"`
		Hex       string          `json:"hex"`
		Value     json.RawMessage `json:"value"`
	} `json:"frames"`
	Malformed []struct {
		Name string `json:"name"`
		Hex  string `json:"hex"`
	} `json:"malformed"`
}

type frameValue struct {
	Kind    string          `json:"kind"`
	Seq     uint64          `json:"seq"`
	Cols    uint16          `json:"cols"`
	Rows    uint16          `json:"rows"`
	PxW     uint16          `json:"px_w"`
	PxH     uint16          `json:"px_h"`
	Data    string          `json:"data"`
	Event   json.RawMessage `json:"event"`
	Request json.RawMessage `json:"request"`
}

// sameJSON compares two JSON texts as values: key order and spacing are the encoder's business.
func sameJSON(t *testing.T, a, b []byte) bool {
	t.Helper()
	var x, y any
	if json.Unmarshal(a, &x) != nil || json.Unmarshal(b, &y) != nil {
		return false
	}
	xa, _ := json.Marshal(x)
	yb, _ := json.Marshal(y)
	return bytes.Equal(xa, yb)
}

func TestSharedBrowserFrames(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("testdata", "frames.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fx sharedFrames
	if err := json.Unmarshal(raw, &fx); err != nil {
		t.Fatal(err)
	}
	if len(fx.Frames) == 0 || len(fx.Malformed) == 0 {
		t.Fatal("the fixture is empty")
	}
	for _, c := range fx.Frames {
		b, err := hex.DecodeString(c.Hex)
		if err != nil {
			t.Fatalf("%s: %v", c.Name, err)
		}
		var v frameValue
		if err := json.Unmarshal(c.Value, &v); err != nil {
			t.Fatalf("%s: %v", c.Name, err)
		}
		f, err := DecodeBrowser(b)
		if err != nil {
			t.Errorf("%s: %v", c.Name, err)
			continue
		}
		// Decoding gives the recorded values, and encoding the values gives the recorded bytes.
		var again []byte
		switch v.Kind {
		case "output":
			ok := f.Type == TypeOutput && f.Seq == v.Seq && string(f.Data) == v.Data
			again = EncodeOutput(v.Seq, []byte(v.Data))
			if !ok {
				t.Errorf("%s: decoded %+v", c.Name, f)
			}
		case "snapshot":
			ok := f.Type == TypeSnapshot && f.Cols == v.Cols && f.Rows == v.Rows && f.Seq == v.Seq && string(f.Data) == v.Data
			again = EncodeSnapshot(v.Cols, v.Rows, v.Seq, []byte(v.Data))
			if !ok {
				t.Errorf("%s: decoded %+v", c.Name, f)
			}
		case "input":
			again = EncodeInput([]byte(v.Data))
			if f.Type != TypeInput || string(f.Data) != v.Data {
				t.Errorf("%s: decoded %+v", c.Name, f)
			}
		case "resize":
			again = EncodeResize(v.Cols, v.Rows, v.PxW, v.PxH)
			if f.Type != TypeResize || f.Cols != v.Cols || f.Rows != v.Rows || f.PxW != v.PxW || f.PxH != v.PxH {
				t.Errorf("%s: decoded %+v", c.Name, f)
			}
		case "ack":
			again = EncodeAck(v.Seq)
			if f.Type != TypeAck || f.Seq != v.Seq {
				t.Errorf("%s: decoded %+v", c.Name, f)
			}
		case "event":
			if f.Type != TypeEvent || !sameJSON(t, f.Data, v.Event) {
				t.Errorf("%s: decoded %s", c.Name, f.Data)
			}
			var ev any
			_ = json.Unmarshal(v.Event, &ev)
			enc, _ := EncodeEvent(ev)
			if enc[0] != TypeEvent || !sameJSON(t, enc[1:], v.Event) {
				t.Errorf("%s: encoded %q", c.Name, enc)
			}
			continue // JSON spacing differs between encoders; the value is what must agree
		case "attach":
			var a Attach
			if err := json.Unmarshal(v.Request, &a); err != nil {
				t.Fatal(err)
			}
			if again, err = EncodeAttach(a); err != nil {
				t.Fatal(err)
			}
			got, err := DecodeAttach(f)
			if err != nil || got.LastSeq != a.LastSeq || got.HaveState != a.HaveState || got.ReadOnly != a.ReadOnly {
				t.Errorf("%s: decoded %+v %v", c.Name, got, err)
			}
		default:
			t.Fatalf("%s: unknown kind %q", c.Name, v.Kind)
		}
		if !bytes.Equal(again, b) {
			t.Errorf("%s: encoded\n got %x\nwant %x", c.Name, again, b)
		}
	}
	for _, c := range fx.Malformed {
		b, _ := hex.DecodeString(c.Hex)
		if _, err := DecodeBrowser(b); err == nil {
			t.Errorf("malformed %s was accepted", c.Name)
		}
	}
}

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
		{Name: "attachment frame carrying an OUTPUT", Channel: 0x01020304, Payload: hex.EncodeToString(EncodeOutput(16, []byte("hi")))},
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

func TestBrowserRejects(t *testing.T) {
	bad := [][]byte{
		{},
		{0x7f},
		{TypeOutput, 1, 2},
		{TypeAck, 0, 0, 0, 0, 0, 0, 0, 1, 9},
		{TypeResize, 0, 80, 0, 24},
		append([]byte{TypeAttach}, "{nope"...),
		append([]byte{TypeEvent}, `{"cols":1}`...),
		append([]byte{TypeEvent}, `[1]`...),
		{TypeOutput, 0x00, 0x20, 0, 0, 0, 0, 0, 0},
		append([]byte{TypeInput}, make([]byte, MaxInput+1)...),
	}
	for _, b := range bad {
		if _, err := DecodeBrowser(b); err == nil {
			t.Errorf("accepted % x", b[:min(len(b), 12)])
		}
	}
}

func FuzzFrame(f *testing.F) {
	f.Add(AppendFrame(nil, 0, []byte(`{"jsonrpc":"2.0"}`)))
	f.Add(AppendFrame(nil, 3, nil))
	f.Add([]byte{0, 0, 0, 4, 0, 0, 0, 1})
	f.Add([]byte{0xff, 0xff, 0xff, 0xff})
	f.Add(EncodeResize(80, 24, 0, 0))
	f.Fuzz(func(t *testing.T, data []byte) {
		r := bytes.NewReader(data)
		for {
			fr, err := ReadFrame(r)
			if err != nil {
				break
			}
			// Whatever decodes must encode back to the same bytes.
			again, err := ReadFrame(bytes.NewReader(AppendFrame(nil, fr.Channel, fr.Payload)))
			if err != nil || again.Channel != fr.Channel || !bytes.Equal(again.Payload, fr.Payload) {
				t.Fatalf("round trip failed: %v", err)
			}
			_, _ = DecodeBrowser(fr.Payload)
		}
		_, _ = DecodeBrowser(data)
	})
}
