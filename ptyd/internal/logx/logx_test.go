//go:build unix

package logx

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRotation(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "j.jsonl")
	r, err := OpenRotating(path, 100, 3)
	if err != nil {
		t.Fatal(err)
	}
	line := []byte(strings.Repeat("x", 39) + "\n")
	for i := 0; i < 20; i++ {
		if _, err := r.Write(line); err != nil {
			t.Fatal(err)
		}
	}
	r.Close()
	for _, name := range []string{"j.jsonl", "j.jsonl.1", "j.jsonl.2"} {
		st, err := os.Stat(filepath.Join(dir, name))
		if err != nil || st.Size() > 100 || st.Mode().Perm() != 0o600 {
			t.Errorf("%s: %v %v", name, st, err)
		}
	}
	if _, err := os.Stat(filepath.Join(dir, "j.jsonl.3")); !os.IsNotExist(err) {
		t.Error("more files than asked for")
	}
}

func TestJournalKeepsTheStartAndTheDigest(t *testing.T) {
	var buf bytes.Buffer
	j := NewJournal(&buf)
	data := bytes.Repeat([]byte("a"), JournalTextBytes+10)
	if err := j.Record(AgentWrite{Terminal: "t", Actor: "orchestrator", Kind: "text"}, data); err != nil {
		t.Fatal(err)
	}
	var got AgentWrite
	if err := json.Unmarshal(buf.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if !got.Truncated || len(got.Text) != JournalTextBytes || got.Bytes != len(data) || len(got.SHA256) != 64 {
		t.Fatalf("%+v", got)
	}
}
