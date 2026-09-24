// Command ptyd-replay plays a terminal recording through the daemon's emulator and writes, at each
// checkpoint, the snapshot the daemon would send a browser. It is the daemon's side of the
// cross-check against xterm.js: the snapshots are replayed into a fresh xterm.js and its screen is
// compared with an xterm.js that saw the original stream.
//
//	ptyd-replay <recording.jsonl> <out.jsonl>
//
// A recording is a header line {"cols", "rows"} and then events [time, kind, data]: "o" output
// (base64), "r" a resize ("COLSxROWS"), "m" a checkpoint (its name). Each output line is
// {"cp", "grid": {"cols", "rows"}, "snaps": {"patched": base64}, "text"}; a checkpoint "end" follows
// the last event.
package main

import (
	"bufio"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"strings"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/internal/emulator"
	"github.com/ascorblack/daedalus/ptyd/internal/emulator/production"
)

func main() {
	if len(os.Args) != 3 {
		fmt.Fprintln(os.Stderr, "usage: ptyd-replay <recording.jsonl> <out.jsonl>")
		os.Exit(2)
	}
	if err := replay(os.Args[1], os.Args[2]); err != nil {
		fmt.Fprintln(os.Stderr, "ptyd-replay:", err)
		os.Exit(1)
	}
}

// clamp keeps a size within the daemon's maximum. Recordings may be smaller than the daemon's
// minimum, and are replayed at their own size so the comparison is like for like.
func clamp(cols, rows int) (int, int) {
	return max(1, min(config.MaxCols, cols)), max(1, min(config.MaxRows, rows))
}

func replay(in, outPath string) error {
	f, err := os.Open(in)
	if err != nil {
		return err
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 64<<20), 64<<20)
	if !sc.Scan() {
		return fmt.Errorf("%s: no header", in)
	}
	var hdr struct{ Cols, Rows int }
	if err := json.Unmarshal(sc.Bytes(), &hdr); err != nil {
		return err
	}
	cols, rows := clamp(hdr.Cols, hdr.Rows)
	e := production.Factory(emulator.Options{Cols: cols, Rows: rows, ScrollbackLines: config.ScrollbackLines,
		ScrollbackBytes: config.ScrollbackBytes, GraphemeClusters: true})
	defer e.Close()
	out, err := os.Create(outPath)
	if err != nil {
		return err
	}
	defer out.Close()
	w := bufio.NewWriter(out)
	defer w.Flush()
	checkpoint := func(name string) error {
		c, r := e.Size()
		snap, _ := e.Snapshot(emulator.SnapshotOptions{Scrollback: config.ScrollbackLines})
		_, first := e.History()
		cur := e.Cursor()
		text := e.Text(first, cur.AbsRow-int64(cur.Y)+int64(r))
		b, err := json.Marshal(map[string]any{
			"cp": name, "grid": map[string]int{"cols": c, "rows": r},
			"snaps": map[string]string{"patched": base64.StdEncoding.EncodeToString(snap)},
			"text":  strings.Join(text, "\n"),
		})
		if err != nil {
			return err
		}
		w.Write(b)
		return w.WriteByte('\n')
	}
	for sc.Scan() {
		var ev []any
		if err := json.Unmarshal(sc.Bytes(), &ev); err != nil || len(ev) < 3 {
			return fmt.Errorf("bad event %q", sc.Text())
		}
		kind, _ := ev[1].(string)
		data, _ := ev[2].(string)
		switch kind {
		case "o":
			b, err := base64.StdEncoding.DecodeString(data)
			if err != nil {
				return err
			}
			e.Feed(b)
		case "r":
			var c, r int
			if _, err := fmt.Sscanf(data, "%dx%d", &c, &r); err != nil {
				return err
			}
			e.Resize(clamp(c, r))
		case "m":
			if err := checkpoint(data); err != nil {
				return err
			}
		}
	}
	if err := sc.Err(); err != nil {
		return err
	}
	return checkpoint("end")
}
