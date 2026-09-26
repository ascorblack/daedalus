package wire

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

// golden is testdata/frames.json: the view frames, written independently of every codec and held
// byte for byte identical by the app (miniapp/src/browser/testdata/frames.json). A host test
// compares the two copies; this test holds the daemon's codec to its copy.
type golden struct {
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

type goldenValue struct {
	Kind     string          `json:"kind"`
	FrameNo  uint32          `json:"frame_no"`
	Meta     Meta            `json:"meta"`
	ImageHex string          `json:"image_hex"`
	Event    json.RawMessage `json:"event"`
	Attach   json.RawMessage `json:"attach"`
	View     json.RawMessage `json:"view"`
	Input    json.RawMessage `json:"input"`
}

func loadGolden(t *testing.T) golden {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("testdata", "frames.json"))
	if err != nil {
		t.Fatal(err)
	}
	var g golden
	if err := json.Unmarshal(data, &g); err != nil {
		t.Fatal(err)
	}
	if len(g.Frames) == 0 || len(g.Malformed) == 0 {
		t.Fatal("the golden file holds no frames")
	}
	return g
}

// sameJSON compares two JSON texts by value.
func sameJSON(t *testing.T, name string, got, want []byte) {
	t.Helper()
	var a, b any
	if err := json.Unmarshal(got, &a); err != nil {
		t.Fatalf("%s: %v", name, err)
	}
	if err := json.Unmarshal(want, &b); err != nil {
		t.Fatalf("%s: %v", name, err)
	}
	if !reflect.DeepEqual(a, b) {
		t.Errorf("%s:\n got %s\nwant %s", name, got, want)
	}
}

func TestGoldenFrames(t *testing.T) {
	g := loadGolden(t)
	for _, c := range g.Frames {
		raw, err := hex.DecodeString(c.Hex)
		if err != nil {
			t.Fatalf("%s: %v", c.Name, err)
		}
		var v goldenValue
		if err := json.Unmarshal(c.Value, &v); err != nil {
			t.Fatalf("%s: %v", c.Name, err)
		}
		f, err := Decode(raw)
		if err != nil {
			t.Errorf("%s: %v", c.Name, err)
			continue
		}
		// What the daemon sends it must also write byte for byte; what the app sends it must read.
		var again []byte
		switch v.Kind {
		case "frame":
			if f.Type != TypeFrame || f.FrameNo != v.FrameNo || !reflect.DeepEqual(f.Meta, v.Meta) || hex.EncodeToString(f.Image) != v.ImageHex {
				t.Errorf("%s: decoded %+v", c.Name, f)
			}
			img, _ := hex.DecodeString(v.ImageHex)
			again, err = EncodeFrame(v.FrameNo, v.Meta, img)
		case "event":
			sameJSON(t, c.Name, f.JSON, v.Event)
			var compact bytes.Buffer
			if err := json.Compact(&compact, v.Event); err != nil {
				t.Fatal(err)
			}
			again, err = EncodeEvent(json.RawMessage(compact.Bytes()))
		case "ack":
			if f.Type != TypeAck || f.FrameNo != v.FrameNo {
				t.Errorf("%s: decoded %+v", c.Name, f)
			}
			again = EncodeAck(v.FrameNo)
		case "attach":
			a, err := DecodeAttach(f)
			if err != nil {
				t.Fatalf("%s: %v", c.Name, err)
			}
			b, _ := json.Marshal(a)
			sameJSON(t, c.Name, b, v.Attach)
			again, err = EncodeJSON(TypeAttach, a)
			if err != nil {
				t.Fatal(err)
			}
		case "view":
			vw, err := DecodeView(f)
			if err != nil {
				t.Fatalf("%s: %v", c.Name, err)
			}
			b, _ := json.Marshal(vw)
			sameJSON(t, c.Name, b, v.View)
			again, err = EncodeJSON(TypeView, vw)
			if err != nil {
				t.Fatal(err)
			}
		case "input":
			in, err := DecodeInput(f)
			if err != nil {
				t.Fatalf("%s: %v", c.Name, err)
			}
			// Zero values are left out when the daemon writes an INPUT (it never does outside
			// tests), so the check is by value, with the golden's zeros dropped.
			var want map[string]any
			_ = json.Unmarshal(v.Input, &want)
			for k, x := range want {
				if x == float64(0) || x == "" {
					delete(want, k)
				}
			}
			b, _ := json.Marshal(in)
			wantB, _ := json.Marshal(want)
			sameJSON(t, c.Name, b, wantB)
			continue
		default:
			t.Fatalf("%s: unknown kind %q", c.Name, v.Kind)
		}
		if err != nil {
			t.Fatalf("%s: %v", c.Name, err)
		}
		if !bytes.Equal(again, raw) {
			t.Errorf("%s: encoded\n got %x\nwant %x", c.Name, again, raw)
		}
	}
	for _, c := range g.Malformed {
		raw, _ := hex.DecodeString(c.Hex)
		if _, err := Decode(raw); err == nil {
			t.Errorf("malformed %s was accepted", c.Name)
		}
	}
}

func TestStrictClientJSON(t *testing.T) {
	f, err := Decode(append([]byte{TypeAttach}, `{"tier":"live","max_w":10,"max_h":10,"colour":1}`...))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := DecodeAttach(f); err == nil {
		t.Fatal("an unknown ATTACH field was accepted")
	}
	long := append([]byte{TypeInput}, `{"t":"text","text":"`...)
	long = append(long, bytes.Repeat([]byte("x"), MaxInput)...)
	long = append(long, `"}`...)
	if _, err := Decode(long); err == nil {
		t.Fatal("an INPUT over 4 KiB was accepted")
	}
}

func TestEventsAreNotHTMLEscaped(t *testing.T) {
	b, err := EncodeEvent(map[string]string{"type": "tab", "title": "Q&A <shop>"})
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(b, []byte(`"Q&A <shop>"`)) {
		t.Fatalf("escaped: %s", b)
	}
}

func FuzzDecode(f *testing.F) {
	g := golden{}
	if data, err := os.ReadFile(filepath.Join("testdata", "frames.json")); err == nil {
		_ = json.Unmarshal(data, &g)
	}
	for _, c := range g.Frames {
		raw, _ := hex.DecodeString(c.Hex)
		f.Add(raw)
	}
	f.Fuzz(func(t *testing.T, data []byte) {
		fr, err := Decode(data)
		if err != nil || fr.Type != TypeFrame {
			return
		}
		// A frame that decodes encodes back to a frame that decodes to the same thing.
		again, err := EncodeFrame(fr.FrameNo, fr.Meta, fr.Image)
		if err != nil {
			return
		}
		fr2, err := Decode(again)
		if err != nil || fr2.FrameNo != fr.FrameNo || !bytes.Equal(fr2.Image, fr.Image) {
			t.Fatalf("round trip failed: %v", err)
		}
	})
}
