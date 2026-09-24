package rpc

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"regexp"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/internal/server"
	"github.com/ascorblack/daedalus/ptyd/internal/term"
	"github.com/ascorblack/daedalus/ptyd/internal/wire"
)

// The screen: snapshots, the screen as text or styled runs, and waiting for something to appear.

func checkScrollback(n *int, def int) error {
	if *n == 0 && def > 0 {
		*n = def
	}
	if *n < 0 || *n > config.ScrollbackLines {
		return wire.Errorf(wire.CodeInvalidParams, "scrollback must be 0..%d", config.ScrollbackLines)
	}
	return nil
}

func (d *Daemon) snapshot(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		ID         string `json:"id"`
		Scrollback *int   `json:"scrollback"`
	}
	t, err := d.terminal(params, &p, &p.ID)
	if err != nil {
		return nil, err
	}
	scrollback := config.DefaultSnapshotScrollback
	if p.Scrollback != nil {
		scrollback = *p.Scrollback
		if err := checkScrollback(&scrollback, 0); err != nil {
			return nil, err
		}
	}
	data, info, seq, err := t.Snapshot(scrollback, config.MaxSnapshotBytes)
	if err != nil {
		return nil, termError(err)
	}
	return map[string]any{"cols": info.Cols, "rows": info.Rows, "seq": seq, "first_abs_row": info.FirstAbsRow,
		"data_b64": base64.StdEncoding.EncodeToString(data)}, nil
}

func (d *Daemon) readScreen(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		ID         string `json:"id"`
		Format     string `json:"format"`
		Scrollback int    `json:"scrollback"`
		TailRows   int    `json:"tail_rows"`
	}
	t, err := d.terminal(params, &p, &p.ID)
	if err != nil {
		return nil, err
	}
	if err := checkScrollback(&p.Scrollback, 0); err != nil {
		return nil, err
	}
	if p.TailRows < 0 || p.TailRows > config.ScrollbackLines+config.MaxRows {
		return nil, wire.Errorf(wire.CodeInvalidParams, "tail_rows must be 0..%d", config.ScrollbackLines+config.MaxRows)
	}
	format := term.ScreenText
	switch p.Format {
	case "", "text":
	case "runs":
		format = term.ScreenRuns
		p.Scrollback = min(p.Scrollback, config.MaxRunsRows)
		if p.TailRows == 0 || p.TailRows > config.MaxRunsRows {
			p.TailRows = config.MaxRunsRows
		}
	case "vt":
		format = term.ScreenVT
	default:
		return nil, wire.Errorf(wire.CodeInvalidParams, "format must be text, runs or vt")
	}
	s, err := t.ReadScreen(format, p.Scrollback, p.TailRows, config.MaxSnapshotBytes)
	if err != nil {
		return nil, termError(err)
	}
	title, cwd := t.TitleCwd()
	out := map[string]any{
		"cols": s.Cols, "rows": s.Rows, "alt_screen": s.AltScreen, "title": title, "cwd": cwd, "seq": s.Seq,
		"first_abs_row": s.FirstAbsRow,
		"cursor":        map[string]any{"x": s.Cursor.X, "y": s.Cursor.Y, "visible": s.Cursor.Visible, "abs_row": s.Cursor.AbsRow},
	}
	switch format {
	case term.ScreenText:
		// A reply is one frame: the oldest lines go first when the text would not fit.
		lines, size, truncated := s.Lines, 0, false
		for i := len(lines) - 1; i >= 0; i-- {
			size += len(lines[i]) + 8
			if size > config.MaxSnapshotBytes {
				lines, truncated = lines[i+1:], true
				break
			}
		}
		if lines == nil {
			lines = []string{}
		}
		out["lines"], out["truncated"] = lines, truncated
	case term.ScreenRuns:
		out["runs"] = s.Runs
	case term.ScreenVT:
		out["data_b64"] = base64.StdEncoding.EncodeToString(s.VT)
	}
	return out, nil
}

// maxWaitRegex bounds the pattern of a wait: RE2 has no catastrophic backtracking, but a huge
// pattern is still a huge automaton.
const maxWaitRegex = 4 << 10

func (d *Daemon) waitFor(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		ID          string `json:"id"`
		Regex       string `json:"regex"`
		Scope       string `json:"scope"`
		IdleMs      int64  `json:"idle_ms"`
		CommandDone bool   `json:"command_done"`
		SinceSeq    *int64 `json:"since_seq"`
		TimeoutMs   int64  `json:"timeout_ms"`
	}
	t, err := d.terminal(params, &p, &p.ID)
	if err != nil {
		return nil, err
	}
	bad := func(format string, args ...any) error { return wire.Errorf(wire.CodeInvalidParams, format, args...) }
	if p.CommandDone {
		return nil, wire.Errorf(wire.CodeUnsupported, "command_done needs shell integration, which this build lacks")
	}
	w := term.WaitSpec{SinceSeq: -1}
	switch p.Scope {
	case "", "screen":
	case "output":
		w.Output = true
	default:
		return nil, bad("scope must be screen or output")
	}
	if p.Regex != "" {
		if len(p.Regex) > maxWaitRegex {
			return nil, bad("regex is longer than %d bytes", maxWaitRegex)
		}
		re, err := regexp.Compile(p.Regex)
		if err != nil {
			return nil, bad("regex: %v", err)
		}
		w.Regex = re
	}
	if p.IdleMs < 0 || p.IdleMs > config.MaxWaitTimeout.Milliseconds() {
		return nil, bad("idle_ms out of range")
	}
	w.Idle = time.Duration(p.IdleMs) * time.Millisecond
	if w.Regex == nil && w.Idle == 0 {
		return nil, bad("give regex, idle_ms or both")
	}
	if p.SinceSeq != nil {
		if *p.SinceSeq < 0 {
			return nil, bad("since_seq must not be negative")
		}
		w.SinceSeq = *p.SinceSeq
	}
	if p.TimeoutMs <= 0 || p.TimeoutMs > config.MaxWaitTimeout.Milliseconds() {
		return nil, bad("timeout_ms must be 1..%d", config.MaxWaitTimeout.Milliseconds())
	}
	w.Timeout = time.Duration(p.TimeoutMs) * time.Millisecond
	r, err := t.WaitFor(ctx, w)
	if err != nil {
		return nil, termError(err)
	}
	out := map[string]any{"matched": r.Matched, "seq": r.Seq}
	if r.Matched == "regex" {
		out["match"] = r.Match
	}
	return out, nil
}
