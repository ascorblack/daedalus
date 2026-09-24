package term

import (
	"bytes"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/emulator/basic"
	"github.com/ascorblack/daedalus/ptyd/internal/scan/scantest"
)

// heapInUse is the live heap after a collection.
func heapInUse() uint64 {
	runtime.GC()
	var m runtime.MemStats
	runtime.ReadMemStats(&m)
	return m.HeapInuse
}

// catThrough runs `cat path` in a terminal with the production emulator and returns the output held
// in the ring, the time it took and the heap growth it left behind.
func catThrough(t *testing.T, reg *Registry, id, path string, ringBytes int) ([]byte, time.Duration, int64) {
	t.Helper()
	before := heapInUse()
	env := BuildEnv(os.Environ(), nil, nil, id)
	cat, ok := LookPath("cat", env, "/")
	if !ok {
		t.Skip("no cat")
	}
	began := time.Now()
	term, err := reg.Create(Spec{ID: id, Path: cat, Argv: []string{"cat", path}, Cwd: "/", Env: env, Cols: 120,
		Rows: 40, RingBytes: ringBytes})
	if err != nil {
		t.Fatal(err)
	}
	select {
	case <-term.Done():
	case <-time.After(60 * time.Second):
		t.Fatalf("%s: no exit within a minute", id)
	}
	took := time.Since(began)
	// Everything the terminal read is in the ring's last 64 KiB after exit; read what is left.
	data, _, _, _ := term.ReadOutput(0, 1<<20, false)
	if err := reg.Forget(id); err != nil {
		t.Fatal(err)
	}
	return data, took, int64(heapInUse()) - int64(before)
}

func adversarialRegistry(t *testing.T) *Registry {
	reg := NewRegistry(Deps{
		Emulator: basic.Factory, Events: &recorder{}, Clock: RealClock{},
		Log: slog.New(slog.NewTextHandler(io.Discard, nil)), KillGrace: 100 * time.Millisecond,
	}, 4)
	t.Cleanup(func() { reg.Shutdown(100 * time.Millisecond) })
	return reg
}

// TestHostileOutputIsBounded sends the kinds of output that made emulators allocate without limit
// through a real PTY, at sizes a test run can afford (about 12 MB in all), and checks that what the
// daemon keeps and passes on is clamped and that its heap did not grow with the input.
func TestHostileOutputIsBounded(t *testing.T) {
	dir := t.TempDir()
	rep := func(s string, n int) string { return strings.Repeat(s, n) }
	inputs := map[string]string{
		"unterminated-osc": "\x1b]0;" + rep("a", 6<<20) + "\x07END",
		"rep-and-scroll":   rep("x\x1b[1000000000b\x1b[100000000S\x1b[4000000000;4000000000H", 2000) + "END",
		"wide-sgr":         "\x1b[" + rep("1;", 200000) + "1mEND",
		"endless-dcs":      "\x1bPq" + rep("#0;2;0;0;0", 300000) + "\x1b\\END",
		"title-stack":      "\x1b]2;" + rep("t", 1<<20) + "\x07" + rep("\x1b[22;0t", 20000) + "END",
	}
	reg := adversarialRegistry(t)
	names := make([]string, 0, len(inputs))
	for name := range inputs {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		path := filepath.Join(dir, name)
		if err := os.WriteFile(path, []byte(inputs[name]), 0o600); err != nil {
			t.Fatal(err)
		}
		out, took, growth := catThrough(t, reg, name, path, 1<<20)
		if !bytes.HasSuffix(out, []byte("END")) {
			t.Errorf("%s: the text after the hostile part was lost (%d bytes kept)", name, len(out))
		}
		if err := scantest.Verify(out); err != nil {
			t.Errorf("%s: %v", name, err)
		}
		if growth > 32<<20 {
			t.Errorf("%s: the heap grew by %d MB", name, growth>>20)
		}
		t.Logf("%-18s %5d KB in, %6s, heap %+d KB", name, len(inputs[name])>>10, took.Round(time.Millisecond), growth>>10)
	}
}

// TestProbeCorpus feeds every file of an external corpus of unclamped probes (up to tens of
// megabytes each) through a terminal. It runs only when PTYD_PROBES_DIR names such a directory, and
// only under a memory cap: an unclamped probe is exactly what took a machine down once.
func TestProbeCorpus(t *testing.T) {
	dir := os.Getenv("PTYD_PROBES_DIR")
	if dir == "" {
		t.Skip("PTYD_PROBES_DIR is not set")
	}
	files, _ := filepath.Glob(filepath.Join(dir, "*.bin"))
	if len(files) == 0 {
		t.Fatalf("no probes in %s", dir)
	}
	reg := adversarialRegistry(t)
	var peak int64
	for i, path := range files {
		st, _ := os.Stat(path)
		out, took, growth := catThrough(t, reg, "probe"+strconv.Itoa(i), path, 8<<20)
		if err := scantest.Verify(out); err != nil {
			t.Errorf("%s: %v", filepath.Base(path), err)
		}
		peak = max(peak, growth)
		t.Logf("%-28s %7d KB in, %7s, %6d KB kept, heap %+d KB", filepath.Base(path), st.Size()>>10,
			took.Round(time.Millisecond), len(out)>>10, growth>>10)
	}
	var m runtime.MemStats
	runtime.ReadMemStats(&m)
	t.Logf("largest heap growth %d KB; heap system %d MB", peak>>10, m.HeapSys>>20)
}

// BenchmarkThroughput measures how fast output goes from a program through the PTY, the scanner,
// the ring and the production emulator: 100 MB of ordinary coloured lines per iteration.
func BenchmarkThroughput(b *testing.B) {
	reg := NewRegistry(Deps{
		Emulator: basic.Factory, Events: &recorder{}, Clock: RealClock{},
		Log: slog.New(slog.NewTextHandler(io.Discard, nil)), KillGrace: 100 * time.Millisecond,
	}, 4)
	defer reg.Shutdown(100 * time.Millisecond)
	const total = 100_000_000
	env := BuildEnv(os.Environ(), nil, nil, "bench")
	sh, _ := LookPath("sh", env, "/")
	b.SetBytes(total)
	for i := 0; i < b.N; i++ {
		id := "bench" + strconv.Itoa(i)
		term, err := reg.Create(Spec{ID: id, Path: sh, Argv: []string{"sh", "-c",
			`yes "$(printf '\033[32mok\033[0m  build step with a path /usr/lib/x.so and some words')" | head -c ` + strconv.Itoa(total)},
			Cwd: "/", Env: env, Cols: 120, Rows: 40, RingBytes: 8 << 20})
		if err != nil {
			b.Fatal(err)
		}
		<-term.Done()
		if got := term.OutputHead(); got < total {
			b.Fatalf("only %d bytes arrived", got)
		}
		_ = reg.Forget(id)
	}
}
