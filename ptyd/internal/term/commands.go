package term

import (
	"crypto/subtle"
	"strings"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/emulator"
	"github.com/ascorblack/daedalus/ptyd/internal/scan"
)

// The commands a shell reports through its marks (OSC 133 A/B/C/D and OSC 633 E): where each one's
// prompt and output are, what it was, and how it ended.

// Bounds of what is kept per terminal.
const (
	maxCommandRecords = 500     // the newest are kept
	maxCommandLine    = 4 << 10 // a longer command line is cut, at a character boundary
)

// CommandRecord is one command, as `terminal.commands` reports it. Rows are absolute (history lines
// plus the screen row), taken from the emulator at the exact point of the stream where each mark
// was, so they stay valid as the output scrolls; they are approximate across a resize, which reflows.
type CommandRecord struct {
	N          int64      `json:"n"`
	Command    string     `json:"command"`
	Cwd        string     `json:"cwd"`
	ExitCode   *int       `json:"exit_code"`
	StartedAt  time.Time  `json:"started_at"`
	FinishedAt *time.Time `json:"finished_at"`
	DurationMs *int64     `json:"duration_ms"`
	PromptRow  *int64     `json:"prompt_row"` // where the prompt before it started, when the shell said
	OutputRow  int64      `json:"output_row"` // the first row of its output
	EndRow     *int64     `json:"end_row"`    // one past the last row of its output, once it ended
	StartSeq   int64      `json:"start_seq"`  // the output offset just after its C mark
	EndSeq     *int64     `json:"end_seq"`    // the output offset just after its D mark
	// Only with `with_output`.
	Output          *string `json:"output,omitempty"`
	OutputTruncated bool    `json:"output_truncated,omitempty"`
}

// commandLog is the terminal's record of its commands. It is written on the emulator goroutine and
// read under the terminal's mutex, which every write also holds.
type commandLog struct {
	nonce       string // the launch's; a mark without it is ignored
	integration string // the shell whose integration the launch loaded, or ""
	verified    bool   // a mark with the nonce has been seen
	records     []CommandRecord
	next        int64
	open        bool   // the last record has started and not ended
	promptRow   *int64 // the last prompt's start, for the next command and for clients
	pending     string // the command line of the next C, from 633 E
}

// verifiedMark reports whether m is a shell mark this launch's shell printed: it carries the nonce.
// A terminal without a nonce believes none.
func (t *Terminal) verifiedMark(m scan.Mark) bool {
	if t.cmds.nonce == "" {
		return false
	}
	k := m.Params["k"]
	return len(k) == len(t.cmds.nonce) && subtle.ConstantTimeCompare([]byte(k), []byte(t.cmds.nonce)) == 1
}

// onShellMark applies a verified mark. It runs on the emulator goroutine, after the emulator was fed
// every byte before the mark, so the cursor is where the mark was printed.
func (t *Terminal) onShellMark(e emulator.Emulator, m scan.Mark, seq int64) {
	now := t.deps.Clock.Now().UTC()
	cur := e.Cursor()
	t.mu.Lock()
	c := &t.cmds
	c.verified = true
	var started, ended *CommandRecord
	var promptRow *int64
	switch m.Letter {
	case 'A':
		// A prompt while a command is open means its end was never reported (the shell was replaced,
		// or a D was lost): it ends here, with no status.
		if c.open {
			ended = t.endLocked(e, cur, now, seq, nil)
		}
		row := cur.AbsRow
		c.promptRow = &row
		promptRow = &row
	case 'E':
		c.pending = cutCommand(m.Text)
	case 'C':
		if c.open {
			ended = t.endLocked(e, cur, now, seq, nil)
		}
		c.next++
		rec := CommandRecord{N: c.next, Command: c.pending, Cwd: t.cwd, StartedAt: now, PromptRow: c.promptRow,
			OutputRow: cur.AbsRow, StartSeq: seq}
		c.pending = ""
		c.records = append(c.records, rec)
		if len(c.records) > maxCommandRecords {
			c.records = append(c.records[:0], c.records[len(c.records)-maxCommandRecords:]...)
		}
		c.open = true
		r := rec
		started = &r
	case 'D':
		// A D closes only a command that was seen to start. Shells report D before every prompt,
		// also after an empty line, when nothing ran.
		if c.open {
			var code *int
			if m.HasExit {
				v := m.Exit
				code = &v
			}
			ended = t.endLocked(e, cur, now, seq, code)
		}
		c.pending = ""
	}
	t.mu.Unlock()

	// Every event carries the output offset of its mark. The client's queue flushes events ahead of
	// output still batched for it, so an event can arrive before the bytes it marks; the offset lets a
	// client hold it until those bytes are drawn, and place the mark on the right row.
	if promptRow != nil {
		row := *promptRow
		t.broadcast("", func(*Client) any {
			return map[string]any{"type": "command", "phase": "prompt", "abs_row": row, "seq": seq, "at": now}
		})
	}
	if ended != nil {
		t.commandEnded(*ended)
	}
	if started != nil {
		s := *started
		t.broadcast("", func(*Client) any {
			return map[string]any{"type": "command", "phase": "start", "n": s.N, "command": s.Command,
				"abs_row": s.OutputRow, "prompt_row": s.PromptRow, "seq": s.StartSeq, "at": s.StartedAt}
		})
	}
}

// endLocked ends the open command at the cursor and returns a copy of it. The output ends at the
// row of the D mark, or one row further when the output left the cursor mid-line (no final newline),
// so that last line is part of it.
func (t *Terminal) endLocked(e emulator.Emulator, cur emulator.Cursor, now time.Time, seq int64, code *int) *CommandRecord {
	c := &t.cmds
	c.open = false
	rec := &c.records[len(c.records)-1]
	end := cur.AbsRow
	if cur.X > 0 {
		end++
	}
	if end < rec.OutputRow {
		end = rec.OutputRow
	}
	finished := now
	ms := now.Sub(rec.StartedAt).Milliseconds()
	s := seq
	rec.ExitCode, rec.FinishedAt, rec.DurationMs, rec.EndRow, rec.EndSeq = code, &finished, &ms, &end, &s
	t.lastCommand = &Command{Command: rec.Command, ExitCode: code, At: now}
	out := *rec
	return &out
}

// commandEnded tells the host and the clients that a command ended. The clients were once told only
// of starts, so a command's mark could never show how it ended until the next snapshot.
func (t *Terminal) commandEnded(r CommandRecord) {
	t.deps.Events.Publish("terminal.command", t.ID, map[string]any{
		"phase": "end", "n": r.N, "exit_code": r.ExitCode, "command": r.Command, "cwd": r.Cwd,
		"abs_row": r.OutputRow, "end_row": r.EndRow, "started_at": r.StartedAt, "duration_ms": r.DurationMs,
		"seq": *r.EndSeq,
	})
	t.broadcast("", func(*Client) any {
		return map[string]any{"type": "command", "phase": "end", "n": r.N, "exit_code": r.ExitCode,
			"command": r.Command, "abs_row": r.OutputRow, "prompt_row": r.PromptRow, "end_row": r.EndRow,
			"duration_ms": r.DurationMs, "seq": *r.EndSeq, "at": *r.FinishedAt}
	})
}

// cutCommand bounds a command line, at a character boundary.
func cutCommand(s string) string {
	if len(s) <= maxCommandLine {
		return s
	}
	cut := maxCommandLine
	for cut > 0 && s[cut]&0xc0 == 0x80 {
		cut--
	}
	return s[:cut]
}

// Integration names the shell integration the terminal's launch loaded, or "".
func (t *Terminal) Integration() string { return t.cmds.integration }

// CommandsKnown reports whether the terminal can report commands: its shell was launched with
// integration, or a program in it printed a mark with its nonce.
func (t *Terminal) CommandsKnown() bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.cmds.integration != "" || t.cmds.verified
}

// busyLocked reports a command running, when marks say; ok is false when they cannot tell.
func (t *Terminal) busyLocked() (busy, ok bool) {
	if !t.cmds.verified {
		return false, false
	}
	return t.cmds.open, true
}

// CommandsOptions shape a `terminal.commands` answer.
type CommandsOptions struct {
	Last       int  // how many of the newest
	WithOutput bool // include each one's output text, while it is still in the emulator's history
	OutputMax  int  // bytes of output per command; the end is kept
	Budget     int  // bytes of the whole answer, roughly; the oldest are dropped past it
}

// Commands returns the newest records, oldest first. With output, the text is read from the emulator
// on its goroutine, so it matches the rows exactly.
func (t *Terminal) Commands(o CommandsOptions) ([]CommandRecord, error) {
	t.mu.Lock()
	recs := t.cmds.records
	if o.Last < len(recs) {
		recs = recs[len(recs)-o.Last:]
	}
	recs = append([]CommandRecord(nil), recs...)
	t.mu.Unlock()
	if o.WithOutput && len(recs) > 0 {
		err := t.WithEmulator(func(e emulator.Emulator) {
			_, first := e.History()
			cur := e.Cursor()
			for i := range recs {
				text, cut := commandOutput(e, recs[i], first, cur, o.OutputMax)
				recs[i].Output, recs[i].OutputTruncated = &text, cut
			}
		})
		if err != nil {
			return nil, err
		}
	}
	// The answer is one frame: from the newest back, until the budget is spent.
	size := 0
	for i := len(recs) - 1; i >= 0; i-- {
		size += 256 + 2*len(recs[i].Command) + len(recs[i].Cwd)
		if recs[i].Output != nil {
			size += len(*recs[i].Output) + len(*recs[i].Output)/4
		}
		if o.Budget > 0 && size > o.Budget {
			recs = recs[i+1:]
			break
		}
	}
	return recs, nil
}

// commandOutput is the text of a command's rows that the emulator still holds, trailing blank lines
// dropped and cut to max bytes from the end. cut says that some of it is gone or was left out.
func commandOutput(e emulator.Emulator, r CommandRecord, first int64, cur emulator.Cursor, max int) (string, bool) {
	from := r.OutputRow
	cut := false
	if from < first {
		from, cut = first, true
	}
	to := cur.AbsRow + 1 // a command still running has output up to the cursor
	if r.EndRow != nil {
		to = *r.EndRow
	}
	if to <= from {
		return "", cut
	}
	text := strings.TrimRight(strings.Join(e.Text(from, to), "\n"), "\n ")
	if max > 0 && len(text) > max {
		start := len(text) - max
		for start < len(text) && text[start]&0xc0 == 0x80 {
			start++
		}
		text, cut = strings.TrimLeft(text[start:], "\n"), true
	}
	return text, cut
}

// Marks is what a client needs to redraw its command marks after a snapshot: every command whose
// rows reach first (the snapshot's first row), and the current prompt. It runs on the emulator
// goroutine with the snapshot, so it describes the same point of the stream.
func (t *Terminal) marksFrom(first int64) map[string]any {
	t.mu.Lock()
	defer t.mu.Unlock()
	list := []map[string]any{}
	for _, r := range t.cmds.records {
		if r.EndRow != nil && *r.EndRow < first {
			continue
		}
		list = append(list, map[string]any{"n": r.N, "command": r.Command, "exit_code": r.ExitCode,
			"prompt_row": r.PromptRow, "output_row": r.OutputRow, "end_row": r.EndRow, "running": r.EndRow == nil})
	}
	return map[string]any{"type": "marks", "list": list, "first_abs_row": first, "prompt_row": t.cmds.promptRow}
}

// CommandDoneAfter returns the first command that ended with its D mark after the output offset
// since, if one has.
func (t *Terminal) CommandDoneAfter(since int64) (CommandRecord, bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	for _, r := range t.cmds.records {
		if r.EndSeq != nil && *r.EndSeq > since {
			return r, true
		}
	}
	return CommandRecord{}, false
}
