package page

import (
	"context"
	"encoding/json"
	"math"
	"strings"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// The bounds of what one read returns.
const (
	DefaultMaxChars = 40000
	MaxChars        = 200000
)

func maxChars(n int) (int, error) {
	if n == 0 {
		return DefaultMaxChars, nil
	}
	if n < 100 || n > MaxChars {
		return 0, wire.Errorf(wire.CodeInvalidParams, "max_chars must be 100-%d", MaxChars)
	}
	return n, nil
}

// Snapshot is page.snapshot's result.
type Snapshot struct {
	URL       string  `json:"url"`
	Title     string  `json:"title"`
	Text      string  `json:"text"`
	Refs      int     `json:"refs"`
	Truncated bool    `json:"truncated"`
	Frames    []Frame `json:"frames"`
}

// Frame is a frame the snapshot met.
type Frame struct {
	Ref         string `json:"ref"`
	URL         string `json:"url"`
	CrossOrigin bool   `json:"cross_origin"`
}

// Snapshot is the page's outline with refs, of one element's subtree when scope is set.
func (p *Model) Snapshot(ctx context.Context, t *browser.Tab, scope string, max int) (*Snapshot, error) {
	n, err := maxChars(max)
	if err != nil {
		return nil, err
	}
	raw, err := p.call(ctx, t, "snapshot", scope, n)
	if err != nil {
		return nil, err
	}
	if err := checkScript(raw); err != nil {
		return nil, err
	}
	var s Snapshot
	if err := json.Unmarshal(raw, &s); err != nil {
		return nil, err
	}
	if s.Frames == nil {
		s.Frames = []Frame{}
	}
	return &s, nil
}

// Text is page.text's result.
type Text struct {
	URL       string `json:"url"`
	Title     string `json:"title"`
	Text      string `json:"text"`
	Truncated bool   `json:"truncated"`
}

// Text is the page's readable text, or one element's.
func (p *Model) Text(ctx context.Context, t *browser.Tab, ref string, max int) (*Text, error) {
	n, err := maxChars(max)
	if err != nil {
		return nil, err
	}
	raw, err := p.call(ctx, t, "readable", ref, n)
	if err != nil {
		return nil, err
	}
	if err := checkScript(raw); err != nil {
		return nil, err
	}
	var out Text
	return &out, json.Unmarshal(raw, &out)
}

// ScreenshotParams are page.screenshot's.
type ScreenshotParams struct {
	TabID    string          `json:"tab_id"`
	Ref      string          `json:"ref,omitempty"`
	FullPage bool            `json:"full_page,omitempty"`
	MaxWidth int             `json:"max_width,omitempty"`
	Format   string          `json:"format,omitempty"`
	Quality  int             `json:"quality,omitempty"`
	Origin   *browser.Origin `json:"origin,omitempty"`
}

// Screenshot is page.screenshot's result.
type Screenshot struct {
	Format string   `json:"format"`
	Width  int      `json:"width"`
	Height int      `json:"height"`
	Data   string   `json:"data_b64"`
	Masked []string `json:"masked"`
}

// Screenshot captures the viewport, the whole page, or one element, with every secret field hidden
// for the capture.
func (p *Model) Screenshot(ctx context.Context, t *browser.Tab, sp ScreenshotParams) (*Screenshot, error) {
	format := sp.Format
	if format == "" {
		format = "jpeg"
	}
	if format != "jpeg" && format != "png" {
		return nil, wire.Errorf(wire.CodeInvalidParams, "format must be jpeg or png")
	}
	quality := sp.Quality
	if quality == 0 {
		quality = 80
	}
	if quality < 30 || quality > 100 {
		return nil, wire.Errorf(wire.CodeInvalidParams, "quality must be 30-100")
	}
	maxW := sp.MaxWidth
	if maxW == 0 {
		maxW = 1280
	}
	if maxW < 64 || maxW > 2560 {
		return nil, wire.Errorf(wire.CodeInvalidParams, "max_width must be 64-2560")
	}
	var x, y, w, h float64
	beyond := false
	switch {
	case sp.Ref != "":
		raw, err := p.call(ctx, t, "prepare", sp.Ref)
		if err != nil {
			return nil, err
		}
		if err := checkScript(raw); err != nil {
			return nil, err
		}
		var pr prepared
		if err := json.Unmarshal(raw, &pr); err != nil {
			return nil, err
		}
		x, y, w, h = pr.Box.X, pr.Box.Y, pr.Box.W, pr.Box.H
	case sp.FullPage:
		var lm struct {
			CSSContentSize struct {
				Width  float64 `json:"width"`
				Height float64 `json:"height"`
			} `json:"cssContentSize"`
		}
		if err := t.Call(ctx, "Page.getLayoutMetrics", nil, &lm); err != nil {
			return nil, err
		}
		w, h = lm.CSSContentSize.Width, math.Min(lm.CSSContentSize.Height, 16000)
		beyond = true
	default:
		w, h = float64(t.Group.Viewport.W), float64(t.Group.Viewport.H)
	}
	if w < 1 || h < 1 {
		return nil, wire.Errorf(wire.CodeForbidden, "the element has no size on the page")
	}
	scale := math.Min(1, float64(maxW)/w)
	raw, err := p.call(ctx, t, "mask", true)
	if err != nil {
		return nil, err
	}
	var m struct {
		Masked []string `json:"masked"`
	}
	_ = json.Unmarshal(raw, &m)
	defer func() {
		// The masks come off whatever happened to the capture.
		uctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_, _ = p.call(uctx, t, "mask", false)
	}()
	params := map[string]any{"format": format, "clip": map[string]any{"x": x, "y": y, "width": w, "height": h, "scale": scale},
		"captureBeyondViewport": beyond}
	if format == "jpeg" {
		params["quality"] = quality
	}
	var shot struct {
		Data string `json:"data"`
	}
	if err := t.Call(ctx, "Page.captureScreenshot", params, &shot); err != nil {
		return nil, err
	}
	if m.Masked == nil {
		m.Masked = []string{}
	}
	return &Screenshot{Format: format, Width: int(math.Round(w * scale)), Height: int(math.Round(h * scale)), Data: shot.Data,
		Masked: m.Masked}, nil
}

// Wait waits for a condition on the page, at most timeout.
func (p *Model) Wait(ctx context.Context, t *browser.Tab, what, value string, timeout time.Duration) (map[string]any, error) {
	wctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	result := func(matched string) map[string]any {
		return map[string]any{"matched": matched, "url": t.URL()}
	}
	switch what {
	case "load", "idle":
		name := "load"
		if what == "idle" {
			name = "networkIdle"
		}
		if err := t.WaitLifecycle(wctx, "", name); err != nil {
			if wctx.Err() != nil && ctx.Err() == nil {
				return result("timeout"), nil
			}
			return nil, err
		}
		return result(what), nil
	case "text", "gone", "url":
		if value == "" {
			return nil, wire.Errorf(wire.CodeInvalidParams, "waiting for %s needs a value", what)
		}
	default:
		return nil, wire.Errorf(wire.CodeInvalidParams, "for must be load, idle, text, gone or url")
	}
	tick := time.NewTicker(200 * time.Millisecond)
	defer tick.Stop()
	for {
		ok, err := p.check(wctx, t, what, value)
		if err != nil && wctx.Err() == nil {
			var we *wire.Error
			// A document replaced under the check is only a moment to check again.
			if !asWire(err, &we) || we.Code == browser.CodeDialogOpen || we.Code == browser.CodeNoSuchTab {
				return nil, err
			}
		}
		if ok {
			return result(what), nil
		}
		select {
		case <-tick.C:
		case <-wctx.Done():
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			return result("timeout"), nil
		}
	}
}

func (p *Model) check(ctx context.Context, t *browser.Tab, what, value string) (bool, error) {
	switch what {
	case "url":
		raw, err := p.call(ctx, t, "href")
		if err != nil {
			return false, err
		}
		var href string
		_ = json.Unmarshal(raw, &href)
		return strings.Contains(href, value), nil
	case "text":
		raw, err := p.call(ctx, t, "hasText", value)
		if err != nil {
			return false, err
		}
		return string(raw) == "true", nil
	case "gone":
		// A ref, or else a text, that is no longer on the page.
		if isRef(value) {
			raw, err := p.call(ctx, t, "exists", value)
			if err != nil {
				return false, err
			}
			return string(raw) == "false", nil
		}
		raw, err := p.call(ctx, t, "hasText", value)
		if err != nil {
			return false, err
		}
		return string(raw) == "false", nil
	}
	return false, nil
}

func isRef(s string) bool {
	if s == "" {
		return false
	}
	rest := s
	if rest[0] == 'f' {
		i := 1
		for i < len(rest) && rest[i] >= '0' && rest[i] <= '9' {
			i++
		}
		if i == 1 {
			return false
		}
		rest = rest[i:]
	}
	if len(rest) < 2 || rest[0] != 'e' {
		return false
	}
	for _, c := range rest[1:] {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}

// AnswerDialog accepts or dismisses the page's dialog.
func (p *Model) AnswerDialog(ctx context.Context, t *browser.Tab, accept bool, text string) error {
	if t.Dialog() == nil {
		return wire.Errorf(wire.CodeNotFound, "no dialog is open on this page")
	}
	params := map[string]any{"accept": accept}
	if text != "" {
		params["promptText"] = text
	}
	if err := t.Call(ctx, "Page.handleJavaScriptDialog", params, nil); err != nil {
		return err
	}
	// The reply means the dialog is gone: wait for the event that says so.
	deadline := time.After(5 * time.Second)
	for t.Dialog() != nil {
		select {
		case <-t.Wake():
		case <-deadline:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	return nil
}
