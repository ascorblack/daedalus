package page

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/sensitive"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// ActParams are page.act's.
type ActParams struct {
	TabID     string          `json:"tab_id"`
	Action    string          `json:"action"`
	Ref       string          `json:"ref,omitempty"`
	ToRef     string          `json:"to_ref,omitempty"`
	Element   string          `json:"element"`
	Text      *string         `json:"text,omitempty"`
	Keys      string          `json:"keys,omitempty"`
	Option    string          `json:"option,omitempty"`
	Submit    bool            `json:"submit,omitempty"`
	Direction string          `json:"direction,omitempty"`
	UploadIDs []string        `json:"upload_ids,omitempty"`
	DryRun    bool            `json:"dry_run,omitempty"`
	Origin    *browser.Origin `json:"origin,omitempty"`
}

// MaxTypeText bounds what one type action inserts.
const MaxTypeText = 10000

// needsRef lists the actions that act on an element.
var needsRef = map[string]bool{"click": true, "double_click": true, "right_click": true, "hover": true, "type": true,
	"select": true, "check": true, "uncheck": true, "drag": true, "upload": true}

type box struct {
	X float64 `json:"x"`
	Y float64 `json:"y"`
	W float64 `json:"w"`
	H float64 `json:"h"`
}

type point struct {
	X float64 `json:"x"`
	Y float64 `json:"y"`
}

type described struct {
	Role         string `json:"role"`
	Name         string `json:"name"`
	Tag          string `json:"tag"`
	Type         string `json:"type,omitempty"`
	Autocomplete string `json:"autocomplete,omitempty"`
	Href         string `json:"href,omitempty"`
	FormAction   string `json:"form_action,omitempty"`
	Secret       bool   `json:"secret"`
	SecretKind   string `json:"secret_kind"`
	Disabled     bool   `json:"disabled"`
	Checked      bool   `json:"checked"`
	File         bool   `json:"file"`
	Select       bool   `json:"select"`
}

type prepared struct {
	Box      box       `json:"box"`
	Element  described `json:"element"`
	Viewport struct {
		W float64 `json:"w"`
		H float64 `json:"h"`
	} `json:"viewport"`
}

// ActResult is page.act's reply.
type ActResult struct {
	ActionID  string            `json:"action_id"`
	OK        bool              `json:"ok"`
	Effects   map[string]any    `json:"effects"`
	Point     *point            `json:"point,omitempty"`
	Box       *box              `json:"box,omitempty"`
	Diff      string            `json:"diff,omitempty"`
	Sensitive *sensitive.Result `json:"sensitive,omitempty"`
	Element   *described        `json:"element,omitempty"`
}

func asWire(err error, we **wire.Error) bool { return errors.As(err, we) }

func (p *Model) prepare(ctx context.Context, t *browser.Tab, ref string) (*prepared, error) {
	raw, err := p.call(ctx, t, "prepare", ref)
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
	return &pr, nil
}

// aim is the point inside a box an action presses: the centre, moved by an offset the action id
// decides, so repeated clicks do not land on the one pixel a page might watch, and never outside.
func aim(b box, actionID string) point {
	h := sha256.Sum256([]byte(actionID))
	u := float64(binary.BigEndian.Uint16(h[0:2]))/65535 - 0.5
	v := float64(binary.BigEndian.Uint16(h[2:4]))/65535 - 0.5
	dx := u * minf(b.W*0.3, 8)
	dy := v * minf(b.H*0.3, 4)
	return point{X: b.X + b.W/2 + dx, Y: b.Y + b.H/2 + dy}
}

func minf(a, b float64) float64 {
	if a < b {
		return a
	}
	return b
}

func fieldForbidden(ref, kind string) *wire.Error {
	if kind == "" {
		kind = "password"
	}
	e := wire.Errorf(browser.CodeFieldForbidden,
		"This is a password, code or payment field. Call BrowserHandoff(reason='login') and let the operator type it.")
	e.Data = map[string]any{"ref": ref, "field": kind}
	return e
}

// Act performs one action: it finds the element, refuses a secret field, classifies what the action
// would do, and, unless it is a dry run, dispatches it as real input and reports what changed.
func (p *Model) Act(ctx context.Context, t *browser.Tab, ap ActParams) (*ActResult, error) {
	if err := p.checkAct(ap); err != nil {
		return nil, err
	}
	p.mu.Lock()
	p.actions++
	id := "a" + p.run + "-" + strconv.Itoa(p.actions)
	p.mu.Unlock()
	res := &ActResult{ActionID: id, Effects: map[string]any{}}

	// The element, brought into view.
	var pr *prepared
	ref := ap.Ref
	if ap.Action == "press" && ref == "" {
		raw, err := p.call(ctx, t, "focusedRef")
		if err != nil {
			return nil, err
		}
		_ = json.Unmarshal(raw, &ref)
	}
	if ref != "" {
		var err error
		if pr, err = p.prepare(ctx, t, ref); err != nil {
			return nil, err
		}
		b := pr.Box
		res.Box, res.Element = &b, &pr.Element
		if ap.Action != "scroll" && ap.Action != "press" && (b.W < 1 || b.H < 1) {
			return nil, wire.Errorf(wire.CodeForbidden, "%s has no size on the page; it cannot be acted on", ref)
		}
		pt := aim(b, id)
		res.Point = &pt
	}

	// Secret fields are the operator's.
	if pr != nil && pr.Element.Secret {
		chordPrintable := false
		if ap.Action == "press" {
			if c, err := parseKeys(ap.Keys); err == nil {
				chordPrintable = c.printable()
			}
		}
		if ap.Action == "type" || ap.Action == "select" || chordPrintable {
			p.needsYou(t, "field_forbidden", "The agent reached a "+strings.ReplaceAll(orDefault(pr.Element.SecretKind, "password"), "_", " ")+
				" field ("+orDefault(pr.Element.Name, ref)+") and needs you to fill it in.", t.URL(), "")
			return nil, fieldForbidden(ref, pr.Element.SecretKind)
		}
	}

	// What the action would do, for the host's policy.
	if ref != "" {
		raw, err := p.call(ctx, t, "evidence", ref, ap.Action, ap.Submit)
		if err != nil {
			return nil, err
		}
		if err := checkScript(raw); err != nil {
			return nil, err
		}
		var ev sensitive.Evidence
		_ = json.Unmarshal(raw, &ev)
		kind := ap.Action
		if ap.Action == "press" {
			if c, err := parseKeys(ap.Keys); err == nil && c.key.Key == "Enter" && c.modBits == 0 {
				kind = "press"
			} else {
				kind = "keys"
			}
		}
		s := sensitive.Classify(kind, ev, ap.Submit)
		res.Sensitive = &s
	} else {
		res.Sensitive = &sensitive.Result{Kinds: []string{}, Evidence: map[string]any{}}
	}
	if ap.DryRun {
		res.OK = true
		return res, nil
	}

	// What the page looked like near the element, and what the group held, before.
	var before []string
	if ref != "" {
		if raw, err := p.call(ctx, t, "region", ref); err == nil {
			_ = json.Unmarshal(raw, &before)
		}
	}
	loaderBefore, urlBefore := t.Loader(), t.URL()
	tabsBefore := map[string]bool{}
	for _, x := range t.Group.Tabs() {
		tabsBefore[x.ID] = true
	}
	downloadsBefore := p.downloadCount(t.Group.ID)
	actor := browser.ActorAgent
	if !ap.Origin.IsAgent() {
		actor = browser.ActorOperator
	}
	event := map[string]any{"action_id": id, "group_id": t.Group.ID, "tab_id": t.ID, "actor": actor, "kind": ap.Action,
		"name": "", "element": ap.Element, "at": time.Now().UnixMilli()}
	if res.Point != nil {
		event["point"], event["box"] = res.Point, res.Box
		event["name"] = pr.Element.Name
	}
	if ap.Text != nil {
		event["text_len"] = len([]rune(*ap.Text))
	}
	if ap.Keys != "" {
		event["keys"] = ap.Keys
	}
	p.m.Publish("action", event)

	err := p.dispatch(ctx, t, ap, ref, pr, res, id)
	done := map[string]any{"action_id": id, "group_id": t.Group.ID, "tab_id": t.ID, "ok": err == nil}
	if err != nil {
		done["error"] = err.Error()
		done["effects"] = map[string]any{}
		p.m.Publish("action_done", done)
		return nil, err
	}

	p.settle(ctx, t, loaderBefore)
	if t.Loader() != loaderBefore || t.URL() != urlBefore {
		res.Effects["navigated"] = true
		res.Effects["url"] = t.URL()
	}
	for _, x := range t.Group.Tabs() {
		if !tabsBefore[x.ID] {
			res.Effects["new_tab"] = x.ID
		}
	}
	if d := t.Dialog(); d != nil {
		res.Effects["dialog"] = d
	}
	if d := p.latestDownload(t.Group.ID, downloadsBefore); d != nil {
		res.Effects["download"] = map[string]any{"id": d.ID, "name": d.Name, "state": d.State}
	}
	if ref != "" && res.Effects["navigated"] == nil && t.Dialog() == nil {
		var after []string
		if raw, err := p.call(ctx, t, "region", ref); err == nil {
			_ = json.Unmarshal(raw, &after)
		}
		res.Diff = diff(before, after, 2000)
	}
	res.OK = true
	done["effects"] = res.Effects
	p.m.Publish("action_done", done)
	if p.AfterAction != nil {
		p.AfterAction(t, id)
	}
	return res, nil
}

func orDefault(s, d string) string {
	if s == "" {
		return d
	}
	return s
}

func (p *Model) checkAct(ap ActParams) error {
	if strings.TrimSpace(ap.Element) == "" || len(ap.Element) > 500 {
		return wire.Errorf(wire.CodeInvalidParams, "element must describe what is acted on, in at most 500 bytes")
	}
	switch ap.Action {
	case "click", "double_click", "right_click", "hover", "check", "uncheck":
	case "type":
		if ap.Text == nil || len([]rune(*ap.Text)) > MaxTypeText {
			return wire.Errorf(wire.CodeInvalidParams, "type needs text, at most %d characters", MaxTypeText)
		}
	case "press":
		if _, err := parseKeys(ap.Keys); err != nil {
			return wire.Errorf(wire.CodeInvalidParams, "%v", err)
		}
	case "select":
		if ap.Option == "" {
			return wire.Errorf(wire.CodeInvalidParams, "select needs the option's label")
		}
	case "scroll":
		if ap.Ref == "" && ap.Direction != "up" && ap.Direction != "down" {
			return wire.Errorf(wire.CodeInvalidParams, "scroll needs a ref, or direction up or down")
		}
	case "drag":
		if ap.ToRef == "" {
			return wire.Errorf(wire.CodeInvalidParams, "drag needs to_ref")
		}
	case "upload":
		if len(ap.UploadIDs) == 0 || len(ap.UploadIDs) > 20 {
			return wire.Errorf(wire.CodeInvalidParams, "upload needs 1-20 upload_ids from upload.put")
		}
	default:
		return wire.Errorf(wire.CodeInvalidParams, "unknown action %q", ap.Action)
	}
	if needsRef[ap.Action] && ap.Ref == "" {
		return wire.Errorf(wire.CodeInvalidParams, "%s needs a ref from a snapshot", ap.Action)
	}
	return nil
}

func (p *Model) input(ctx context.Context, t *browser.Tab, method string, params any) error {
	return t.Input(ctx, method, params)
}

func (p *Model) mouse(ctx context.Context, t *browser.Tab, typ string, pt point, button string, clicks, mods int) error {
	params := map[string]any{"type": typ, "x": pt.X, "y": pt.Y, "modifiers": mods}
	if typ != "mouseMoved" {
		params["button"] = button
		params["clickCount"] = clicks
		if typ == "mousePressed" {
			params["buttons"] = map[string]int{"left": 1, "right": 2, "middle": 4}[button]
		}
	}
	return p.input(ctx, t, "Input.dispatchMouseEvent", params)
}

func (p *Model) click(ctx context.Context, t *browser.Tab, pt point, button string, count int) error {
	if err := p.mouse(ctx, t, "mouseMoved", pt, "", 0, 0); err != nil {
		return err
	}
	for i := 1; i <= count && t.Dialog() == nil; i++ {
		if err := p.mouse(ctx, t, "mousePressed", pt, button, i, 0); err != nil {
			return err
		}
		if err := p.mouse(ctx, t, "mouseReleased", pt, button, i, 0); err != nil {
			return err
		}
	}
	return nil
}

// covered refuses a click whose point another element covers: clicking it would act on something
// the snapshot did not name, which is how a page steers an agent's click.
func (p *Model) covered(ctx context.Context, t *browser.Tab, ref string, pt point) error {
	raw, err := p.call(ctx, t, "hit", ref, pt.X, pt.Y)
	if err != nil {
		return err
	}
	if err := checkScript(raw); err != nil {
		return err
	}
	var h struct {
		OK        bool   `json:"ok"`
		CoveredBy string `json:"covered_by"`
	}
	_ = json.Unmarshal(raw, &h)
	if h.OK {
		return nil
	}
	e := wire.Errorf(wire.CodeForbidden, "%s is covered by %s at the point it would be clicked; deal with that first", ref, orDefault(h.CoveredBy, "something else"))
	e.Data = map[string]any{"ref": ref, "covered_by": h.CoveredBy}
	return e
}

func (p *Model) press(ctx context.Context, t *browser.Tab, c chord) error {
	for _, m := range c.mods {
		k := modifiers[m].key
		if err := p.input(ctx, t, "Input.dispatchKeyEvent", map[string]any{"type": "rawKeyDown", "key": k.Key, "code": k.Code,
			"windowsVirtualKeyCode": k.KeyCode, "modifiers": c.modBits}); err != nil {
			return err
		}
	}
	down := map[string]any{"type": "rawKeyDown", "key": c.key.Key, "code": c.key.Code, "windowsVirtualKeyCode": c.key.KeyCode,
		"modifiers": c.modBits}
	if c.key.Text != "" {
		down["type"] = "keyDown"
		down["text"] = c.key.Text
		down["unmodifiedText"] = c.key.Text
	}
	if err := p.input(ctx, t, "Input.dispatchKeyEvent", down); err != nil {
		return err
	}
	if err := p.input(ctx, t, "Input.dispatchKeyEvent", map[string]any{"type": "keyUp", "key": c.key.Key, "code": c.key.Code,
		"windowsVirtualKeyCode": c.key.KeyCode, "modifiers": c.modBits}); err != nil {
		return err
	}
	for i := len(c.mods) - 1; i >= 0; i-- {
		k := modifiers[c.mods[i]].key
		if err := p.input(ctx, t, "Input.dispatchKeyEvent", map[string]any{"type": "keyUp", "key": k.Key, "code": k.Code,
			"windowsVirtualKeyCode": k.KeyCode}); err != nil {
			return err
		}
	}
	return nil
}

func (p *Model) dispatch(ctx context.Context, t *browser.Tab, ap ActParams, ref string, pr *prepared, res *ActResult, id string) error {
	pt := point{}
	if res.Point != nil {
		pt = *res.Point
	}
	disabled := pr != nil && pr.Element.Disabled
	switch ap.Action {
	case "click", "double_click", "right_click":
		if disabled {
			return wire.Errorf(wire.CodeForbidden, "%s is disabled", ref)
		}
		if err := p.covered(ctx, t, ref, pt); err != nil {
			return err
		}
		switch ap.Action {
		case "click":
			return p.click(ctx, t, pt, "left", 1)
		case "double_click":
			return p.click(ctx, t, pt, "left", 2)
		}
		return p.click(ctx, t, pt, "right", 1)
	case "hover":
		return p.mouse(ctx, t, "mouseMoved", pt, "", 0, 0)
	case "check", "uncheck":
		want := ap.Action == "check"
		if pr.Element.Checked == want {
			res.Effects["unchanged"] = true
			return nil
		}
		if err := p.covered(ctx, t, ref, pt); err != nil {
			return err
		}
		return p.click(ctx, t, pt, "left", 1)
	case "type":
		if disabled {
			return wire.Errorf(wire.CodeForbidden, "%s is disabled", ref)
		}
		// Focused by a real click where the field can be clicked, so the page sees what a person's
		// typing makes it see; then its content selected and replaced.
		if err := p.covered(ctx, t, ref, pt); err == nil {
			if err := p.click(ctx, t, pt, "left", 1); err != nil {
				return err
			}
		}
		if raw, err := p.call(ctx, t, "selectAll", ref); err != nil {
			return err
		} else if err := checkScript(raw); err != nil {
			return err
		}
		if *ap.Text != "" {
			if err := p.input(ctx, t, "Input.insertText", map[string]any{"text": *ap.Text}); err != nil {
				return err
			}
		} else {
			if err := p.press(ctx, t, chord{key: named["Delete"]}); err != nil {
				return err
			}
		}
		if ap.Submit {
			return p.press(ctx, t, chord{key: named["Enter"]})
		}
		return nil
	case "press":
		c, _ := parseKeys(ap.Keys)
		if ap.Ref != "" {
			if raw, err := p.call(ctx, t, "focus", ref); err != nil {
				return err
			} else if err := checkScript(raw); err != nil {
				return err
			}
		}
		return p.press(ctx, t, c)
	case "select":
		if disabled {
			return wire.Errorf(wire.CodeForbidden, "%s is disabled", ref)
		}
		raw, err := p.call(ctx, t, "selectOption", ref, ap.Option)
		if err != nil {
			return err
		}
		return checkScript(raw)
	case "scroll":
		if ref != "" {
			return nil // prepare already brought it into view
		}
		vw, vh := float64(t.Group.Viewport.W), float64(t.Group.Viewport.H)
		dy := vh * 0.8
		if ap.Direction == "up" {
			dy = -dy
		}
		c := point{X: vw / 2, Y: vh / 2}
		res.Point = &c
		if err := t.Call(ctx, "Input.dispatchMouseEvent", map[string]any{"type": "mouseWheel", "x": c.X, "y": c.Y,
			"deltaX": 0, "deltaY": dy}, nil); err != nil {
			return err
		}
		time.Sleep(150 * time.Millisecond)
		return nil
	case "drag":
		to, err := p.prepare(ctx, t, ap.ToRef)
		if err != nil {
			return err
		}
		// The source may have scrolled out while the target came in: measured again.
		from, err := p.prepare(ctx, t, ref)
		if err != nil {
			return err
		}
		a, b := aim(from.Box, id), aim(to.Box, id+"to")
		if err := p.mouse(ctx, t, "mouseMoved", a, "", 0, 0); err != nil {
			return err
		}
		if err := p.mouse(ctx, t, "mousePressed", a, "left", 1, 0); err != nil {
			return err
		}
		for i := 1; i <= 10; i++ {
			f := float64(i) / 10
			if err := t.Call(ctx, "Input.dispatchMouseEvent", map[string]any{"type": "mouseMoved", "x": a.X + (b.X-a.X)*f,
				"y": a.Y + (b.Y-a.Y)*f, "button": "left", "buttons": 1}, nil); err != nil {
				return err
			}
		}
		return p.mouse(ctx, t, "mouseReleased", b, "left", 1, 0)
	case "upload":
		if !pr.Element.File {
			return wire.Errorf(wire.CodeInvalidParams, "%s is not a file input", ref)
		}
		paths, err := p.uploadPaths(t.Group.ID, ap.UploadIDs)
		if err != nil {
			return err
		}
		obj, err := p.object(ctx, t, "element", ref)
		if err != nil {
			return err
		}
		return t.Call(ctx, "DOM.setFileInputFiles", map[string]any{"files": paths, "objectId": obj}, nil)
	}
	return fmt.Errorf("unknown action %q", ap.Action)
}

// settle gives an action's consequences a moment to show: a navigation it started is waited for to
// its load (at most 10 s), anything else for a quarter of a second.
func (p *Model) settle(ctx context.Context, t *browser.Tab, loaderBefore string) {
	deadline := time.Now().Add(400 * time.Millisecond)
	for time.Now().Before(deadline) {
		if t.Loader() != loaderBefore || t.Dialog() != nil {
			break
		}
		select {
		case <-t.Wake():
		case <-time.After(time.Until(deadline)):
		case <-ctx.Done():
			return
		}
	}
	if t.Loader() != loaderBefore && t.Dialog() == nil {
		lctx, cancel := context.WithTimeout(ctx, 10*time.Second)
		defer cancel()
		_ = t.WaitLifecycle(lctx, "", "load")
	}
}

// diff is what an action changed near its element: lines that appeared (+) and went (-), at most
// limit bytes.
func diff(before, after []string, limit int) string {
	before, after = unfocused(before), unfocused(after)
	seen := map[string]int{}
	for _, l := range before {
		seen[l]++
	}
	var out []string
	for _, l := range after {
		if seen[l] > 0 {
			seen[l]--
			continue
		}
		out = append(out, "+ "+strings.TrimLeft(l, " "))
	}
	for _, l := range before {
		if seen[l] > 0 {
			seen[l]--
			out = append(out, "- "+strings.TrimLeft(l, " "))
		}
	}
	var b strings.Builder
	for _, l := range out {
		if b.Len()+len(l)+1 > limit {
			b.WriteString("… (more changed; take a snapshot)\n")
			break
		}
		b.WriteString(l + "\n")
	}
	return strings.TrimSuffix(b.String(), "\n")
}

// unfocused drops the focus marker: where the focus went is the least of what an action changed,
// and every click moves it.
func unfocused(lines []string) []string {
	out := make([]string, len(lines))
	for i, l := range lines {
		out[i] = strings.Replace(l, " [focused]", "", 1)
	}
	return out
}
