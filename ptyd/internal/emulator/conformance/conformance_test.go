package conformance

import (
	"bytes"
	"encoding/json"
	"flag"
	"os"
	"testing"
)

var update = flag.Bool("update", false, "rewrite testdata from the code")

// The cases as data, for the browser side's cross-check, are the cases in the code.
func TestCasesData(t *testing.T) {
	data, err := json.MarshalIndent(Cases, "", " ")
	if err != nil {
		t.Fatal(err)
	}
	data = append(data, '\n')
	const path = "testdata/cases.json"
	if *update {
		if err := os.WriteFile(path, data, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	have, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(have, data) {
		t.Fatalf("%s is stale; run go test ./internal/emulator/conformance -update", path)
	}
}
