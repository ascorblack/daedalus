//go:build cgo

package ghostty

import (
	"fmt"
	"os"
	"strings"
	"testing"

	"github.com/ascorblack/daedalus/ptyd/internal/emulator"
)

// mixedOutput is about n bytes of what terminals mostly show: coloured build output, plain lines,
// some wide text and emoji, and a full-screen redraw now and then.
func mixedOutput(n int) []byte {
	var b strings.Builder
	for i := 0; b.Len() < n; i++ {
		if i%1000 == 999 {
			// A full-screen program comes and goes.
			fmt.Fprintf(&b, "\x1b[?1049h\x1b[H\x1b[2J\x1b[7m status %d \x1b[0m\x1b[2;1Hmenu\x1b[?1049l", i)
			continue
		}
		switch i % 10 {
		case 0, 1, 2, 3:
			fmt.Fprintf(&b, "\x1b[32mok\x1b[0m  step %d: compiled /usr/lib/x%d.so in %d ms\r\n", i, i%97, i%1000)
		case 4, 5:
			fmt.Fprintf(&b, "plain text line number %d with some words to wrap around the edge of the screen maybe\r\n", i)
		case 6:
			fmt.Fprintf(&b, "\x1b[1;38;2;%d;%d;%dm%s\x1b[0m 日本語 ✅ 👍🏻\r\n", i%256, (i*7)%256, (i*13)%256, "bold truecolour")
		case 7:
			fmt.Fprintf(&b, "\x1b[7m progress %d%% \x1b[0m\r\x1b[K", i%100)
		case 8:
			fmt.Fprintf(&b, "\x1b[A\x1b[%dG\x1b[4mcell\x1b[24m\x1b[B\r", i%60+1)
		case 9:
			b.WriteString("\r\n")
		}
	}
	return []byte(b.String())
}

func BenchmarkFeed(b *testing.B) {
	data := mixedOutput(50 << 20)
	b.SetBytes(int64(len(data)))
	for i := 0; i < b.N; i++ {
		e := New(emulator.Options{Cols: 200, Rows: 50, ScrollbackLines: 10000, ScrollbackBytes: 64 << 20, GraphemeClusters: true})
		// In reads of the size the daemon hands over.
		for off := 0; off < len(data); off += 32 << 10 {
			e.Feed(data[off:min(off+32<<10, len(data))])
		}
		e.Close()
	}
}

func BenchmarkSnapshot(b *testing.B) {
	e := New(emulator.Options{Cols: 200, Rows: 50, ScrollbackLines: 10000, ScrollbackBytes: 64 << 20, GraphemeClusters: true})
	defer e.Close()
	data := mixedOutput(8 << 20)
	for off := 0; off < len(data); off += 32 << 10 {
		e.Feed(data[off:min(off+32<<10, len(data))])
	}
	if n, _ := e.History(); n < 9000 {
		b.Fatalf("only %d lines of history", n)
	}
	var size int
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		snap, _ := e.Snapshot(emulator.SnapshotOptions{Scrollback: 10000})
		size = len(snap)
	}
	b.ReportMetric(float64(size)/(1<<20), "MB/snapshot")
}

func BenchmarkSnapshotDefault(b *testing.B) {
	e := New(emulator.Options{Cols: 200, Rows: 50, ScrollbackLines: 10000, ScrollbackBytes: 64 << 20, GraphemeClusters: true})
	defer e.Close()
	data := mixedOutput(8 << 20)
	for off := 0; off < len(data); off += 32 << 10 {
		e.Feed(data[off:min(off+32<<10, len(data))])
	}
	var size int
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		snap, _ := e.Snapshot(emulator.SnapshotOptions{Scrollback: 2000})
		size = len(snap)
	}
	b.ReportMetric(float64(size)/(1<<20), "MB/snapshot")
}

// BenchmarkFeedFile feeds the file PTYD_BENCH_FILE names, for comparing with other measurements of
// the same bytes.
func BenchmarkFeedFile(b *testing.B) {
	path := os.Getenv("PTYD_BENCH_FILE")
	if path == "" {
		b.Skip("PTYD_BENCH_FILE is not set")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		b.Fatal(err)
	}
	// 120x40, as the earlier measurements of these files were taken.
	b.SetBytes(int64(len(data)))
	for i := 0; i < b.N; i++ {
		e := New(emulator.Options{Cols: 120, Rows: 40, ScrollbackLines: 10000, ScrollbackBytes: 64 << 20, GraphemeClusters: true})
		for off := 0; off < len(data); off += 32 << 10 {
			e.Feed(data[off:min(off+32<<10, len(data))])
		}
		e.Close()
	}
}
