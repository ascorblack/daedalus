//go:build unix

package rpc_test

import (
	"encoding/json"
	"os"
	"syscall"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/config"
	"github.com/ascorblack/daedalus/browserd/internal/wire"
)

func TestOpenNavigateAndTabs(t *testing.T) {
	h := start(t, nil)
	var info map[string]any
	h.must("daemon.info", nil, &info)
	if info["protocol"] != float64(1) || info["env"] != "test" {
		t.Fatalf("info: %v", info)
	}
	o := h.open("g1", "project-a", "/still")
	if !o.Created || o.Group.ID != "g1" || o.Tab.ID == "" {
		t.Fatalf("open: %+v", o)
	}
	var nav browser.NavResult
	h.must("page.navigate", map[string]any{"tab_id": o.Tab.ID, "url": h.site.URL + "/button"}, &nav)
	if nav.Title != "Button" || nav.Error != "" {
		t.Fatalf("navigate: %+v", nav)
	}
	// Opening the same group again returns it, and does not start another browser.
	again := h.open("g1", "project-a", "")
	if again.Created || again.Tab.ID != o.Tab.ID {
		t.Fatalf("second open: %+v", again)
	}
	var tab map[string]any
	h.must("tab.new", map[string]any{"group_id": "g1", "url": h.site.URL + "/still"}, &tab)
	var list struct {
		Tabs      []map[string]any `json:"tabs"`
		ActiveTab string           `json:"active_tab"`
	}
	h.must("tab.list", map[string]any{"group_id": "g1"}, &list)
	if len(list.Tabs) != 2 || list.ActiveTab != tab["id"] {
		t.Fatalf("tabs: %+v", list)
	}
	h.must("page.back", map[string]any{"tab_id": o.Tab.ID}, &nav)
	if nav.Title != "Still" {
		t.Fatalf("back: %+v", nav)
	}
	h.must("tab.close", map[string]any{"tab_id": tab["id"]}, nil)
	h.must("tab.list", map[string]any{"group_id": "g1"}, &list)
	if len(list.Tabs) != 1 {
		t.Fatalf("after close: %+v", list)
	}
	// Schemes that reach past the network wall are refused before Chromium sees them.
	for _, u := range []string{"file:///etc/passwd", "data:text/html,hi", "javascript:alert(1)", "chrome://version"} {
		if err := h.call("page.navigate", map[string]any{"tab_id": o.Tab.ID, "url": u}, nil); code(err) != 1004 {
			t.Errorf("%s: %v", u, err)
		}
	}
	// A navigation that fails is a result, not an error.
	h.must("page.navigate", map[string]any{"tab_id": o.Tab.ID, "url": "http://127.0.0.1:1/"}, &nav)
	if nav.Error == "" {
		t.Fatalf("an unreachable page loaded: %+v", nav)
	}
	var closed map[string]any
	h.must("group.close", map[string]any{"group_id": "g1"}, &closed)
	if err := h.call("tab.list", map[string]any{"group_id": "g1"}, nil); code(err) != 1001 {
		t.Fatalf("a closed group: %v", err)
	}
}

func TestBrowserCapAndThrowawayGroups(t *testing.T) {
	h := start(t, func(l *config.Limits) { l.MaxBrowsers = 1 })
	h.open("g1", "project-a", "/still")
	// Two groups of one profile share its browser.
	h.open("g2", "project-a", "/still")
	var bl struct {
		Browsers []map[string]any `json:"browsers"`
	}
	h.must("browser.list", nil, &bl)
	if len(bl.Browsers) != 1 || bl.Browsers[0]["groups"] != float64(2) {
		t.Fatalf("browsers: %+v", bl)
	}
	err := h.call("browser.open", map[string]any{"group_id": "g3", "profile": "project-b"}, nil)
	if code(err) != 1003 {
		t.Fatalf("past the cap: %v", err)
	}
	var tl struct {
		Tabs []map[string]any `json:"tabs"`
	}
	h.must("tab.list", map[string]any{"group_id": "g2"}, &tl)
	if len(tl.Tabs) != 1 {
		t.Fatalf("each group sees its own tabs: %+v", tl)
	}
}

func TestEphemeralGroupsAreSeparate(t *testing.T) {
	h := start(t, nil)
	a := h.open("e1", browser.EphemeralProfile, "/still")
	b := h.open("e2", browser.EphemeralProfile, "/still")
	if a.Tab.ID == b.Tab.ID {
		t.Fatal("two throwaway groups share a tab")
	}
	var bl struct {
		Browsers []map[string]any `json:"browsers"`
	}
	h.must("browser.list", nil, &bl)
	if len(bl.Browsers) != 1 {
		t.Fatalf("throwaway groups share one browser: %+v", bl)
	}
}

func TestViewSendsFramesOnlyOnChangeAndPaces(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/still")
	v := h.attach("g1", wire.Attach{Tier: "live", MaxW: 1280, MaxH: 800})
	hello := v.waitEvent("hello", 10*time.Second, nil)
	if hello["client_id"] != v.id || hello["tab_id"] != o.Tab.ID {
		t.Fatalf("hello: %v", hello)
	}
	f, ok := v.frame(10 * time.Second)
	if !ok {
		t.Fatal("no first frame")
	}
	if f.FrameNo != 1 || f.Meta.Tab != o.Tab.ID || f.Meta.Tier != "live" || f.Meta.VW != 1280 || f.Meta.VH != 800 || f.Meta.W != 1280 {
		t.Fatalf("first frame: %+v", f.Meta)
	}
	v.ack(f.FrameNo)
	// A still page sends nothing more.
	if f2, ok := v.frame(2 * time.Second); ok {
		t.Fatalf("a still page sent frame %d", f2.FrameNo)
	}
	// A moving page sends, at most fps_cap a second.
	h.must("page.navigate", map[string]any{"tab_id": o.Tab.ID, "url": h.site.URL + "/anim"}, nil)
	count := 0
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		f, ok := v.frame(time.Until(deadline))
		if !ok {
			break
		}
		count++
		v.ack(f.FrameNo)
	}
	if count < 5 || count > 3*config.DefaultFPSCap+3 {
		t.Fatalf("%d frames in 3 s of animation", count)
	}
}

func TestSlowViewerGetsTheNewestFrame(t *testing.T) {
	h := start(t, nil)
	h.open("g1", "project-a", "/anim")
	v := h.attach("g1", wire.Attach{Tier: "live", MaxW: 640, MaxH: 400})
	first, ok := v.frame(10 * time.Second)
	if !ok {
		t.Fatal("no first frame")
	}
	// Unacknowledged, nothing else comes: one frame in flight.
	if f, ok := v.frame(1500 * time.Millisecond); ok {
		t.Fatalf("frame %d came before the acknowledgement", f.FrameNo)
	}
	acked := time.Now()
	v.ack(first.FrameNo)
	next, ok := v.frame(5 * time.Second)
	if !ok {
		t.Fatal("no frame after the acknowledgement")
	}
	// The frame that follows is the newest, not one that waited through the pause.
	if age := acked.Sub(time.UnixMilli(next.Meta.TS)); age > 700*time.Millisecond {
		t.Fatalf("the frame after a pause was %s old", age)
	}
	if next.FrameNo != first.FrameNo+1 {
		t.Fatalf("frame numbers %d then %d", first.FrameNo, next.FrameNo)
	}
}

func TestThumbnailTier(t *testing.T) {
	h := start(t, nil)
	h.open("g1", "project-a", "/anim")
	v := h.attach("g1", wire.Attach{Tier: "thumb", MaxW: 320, MaxH: 200})
	var got []wire.Frame
	deadline := time.Now().Add(4 * time.Second)
	for time.Now().Before(deadline) {
		f, ok := v.frame(time.Until(deadline))
		if !ok {
			break
		}
		got = append(got, f)
		v.ack(f.FrameNo)
	}
	if len(got) < 2 || len(got) > 6 {
		t.Fatalf("%d thumbnails in 4 s", len(got))
	}
	for _, f := range got {
		if f.Meta.Tier != "thumb" || f.Meta.W > 320 || f.Meta.H > 200 {
			t.Fatalf("thumbnail meta %+v", f.Meta)
		}
	}
	// A live viewer joining makes the screencast bigger; the thumbnail stays small.
	live := h.attach("g1", wire.Attach{Tier: "live", MaxW: 1280, MaxH: 800})
	// Its first frame may be the thumbnail-sized picture the tab already had; a full one follows.
	var lf wire.Frame
	for deadline := time.Now().Add(10 * time.Second); time.Now().Before(deadline); {
		f, ok := live.frame(time.Until(deadline))
		if !ok {
			break
		}
		live.ack(f.FrameNo)
		if f.Meta.W > 320 {
			lf = f
			break
		}
	}
	if lf.Meta.W <= 320 {
		t.Fatalf("no full live frame beside a thumbnail: %+v", lf.Meta)
	}
	v.waitEvent("viewers", 5*time.Second, func(e map[string]any) bool { return e["count"] == float64(2) })
	for i := 0; i < 3; i++ {
		f, ok := v.frame(3 * time.Second)
		if !ok {
			break
		}
		v.ack(f.FrameNo)
		if f.Meta.W > 320 {
			t.Fatalf("a thumbnail of %dx%d", f.Meta.W, f.Meta.H)
		}
	}
}

func TestHumanControlBlocksTheAgentAndTakesInput(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/button")
	v := h.attach("g1", wire.Attach{Tier: "live", MaxW: 1280, MaxH: 800})
	v.waitEvent("hello", 10*time.Second, nil)
	other := h.attach("g1", wire.Attach{Tier: "thumb", MaxW: 320, MaxH: 200})
	other.waitEvent("hello", 10*time.Second, nil)

	// Input before anyone holds control is refused.
	v.input(map[string]any{"t": "mouse", "type": "move", "x": 10, "y": 10})
	v.waitEvent("error", 5*time.Second, func(e map[string]any) bool { return e["code"] == "not_holder" })

	var ctl map[string]any
	h.must("control.set", map[string]any{"group_id": "g1", "owner": "human", "client_id": v.id}, &ctl)
	if ctl["owner"] != "human" || ctl["holder"] != v.id {
		t.Fatalf("control: %v", ctl)
	}
	v.waitEvent("control", 5*time.Second, func(e map[string]any) bool { return e["holder"] == "you" })
	other.waitEvent("control", 5*time.Second, func(e map[string]any) bool { return e["holder"] == "other" })

	// The agent waits, then is told a person drives; its reads are refused the same way.
	wait := int64(300)
	err := h.call("page.navigate", map[string]any{"tab_id": o.Tab.ID, "url": h.site.URL + "/still",
		"origin": map[string]any{"actor": "agent", "wait_ms": wait}}, nil)
	if code(err) != 1101 {
		t.Fatalf("agent while a person drives: %v", err)
	}
	// The person clicks the button: a trusted click.
	for _, typ := range []string{"move", "down", "up"} {
		v.input(map[string]any{"t": "mouse", "type": typ, "x": 200, "y": 130, "button": "left", "clicks": 1})
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		var list struct {
			Tabs []map[string]any `json:"tabs"`
		}
		h.must("tab.list", map[string]any{"group_id": "g1"}, &list)
		if list.Tabs[0]["title"] == "clicked true" {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the click did not land: %v", list.Tabs[0]["title"])
		}
		time.Sleep(100 * time.Millisecond)
	}
	// Another client's input is still refused.
	other.input(map[string]any{"t": "text", "text": "x"})
	other.waitEvent("error", 5*time.Second, func(e map[string]any) bool { return e["code"] == "not_holder" })
	// The operator's own calls pass.
	h.must("page.reload", map[string]any{"tab_id": o.Tab.ID, "origin": map[string]any{"actor": "operator"}}, nil)

	h.must("control.set", map[string]any{"group_id": "g1", "owner": "paused", "reason": "checking"}, nil)
	if err := h.call("page.reload", map[string]any{"tab_id": o.Tab.ID}, nil); code(err) != 1106 {
		t.Fatalf("agent while paused: %v", err)
	}
	h.must("control.set", map[string]any{"group_id": "g1", "owner": "agent"}, nil)
	h.must("page.reload", map[string]any{"tab_id": o.Tab.ID}, nil)
}

func TestPopupJoinsTheOpenersGroup(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/popup")
	v := h.attach("g1", wire.Attach{Tier: "thumb", MaxW: 320, MaxH: 200})
	v.waitEvent("hello", 10*time.Second, nil)
	h.must("control.set", map[string]any{"group_id": "g1", "owner": "human", "client_id": v.id}, nil)
	for _, typ := range []string{"move", "down", "up"} {
		v.input(map[string]any{"t": "mouse", "type": typ, "x": 200, "y": 130, "button": "left", "clicks": 1})
	}
	e := v.waitEvent("tabs", 10*time.Second, func(e map[string]any) bool {
		tabs, _ := e["tabs"].([]any)
		return len(tabs) == 2
	})
	tabs := e["tabs"].([]any)
	second := tabs[1].(map[string]any)
	if second["id"] == o.Tab.ID {
		t.Fatalf("tabs: %v", tabs)
	}
	var tv map[string]any
	var list struct {
		Tabs []map[string]any `json:"tabs"`
	}
	h.must("tab.list", map[string]any{"group_id": "g1"}, &list)
	tv = list.Tabs[1]
	if tv["opener"] != o.Tab.ID {
		t.Fatalf("the popup's opener: %v", tv)
	}
}

func TestIdleCloseAndCrash(t *testing.T) {
	h := start(t, func(l *config.Limits) { l.IdleClose = time.Minute })
	h.open("g1", "project-a", "/still")
	h.open("g2", "project-b", "/still")
	if err := h.client.Call(t.Context(), "events.subscribe", map[string]any{"after_seq": 0}, nil); err != nil {
		t.Fatal(err)
	}
	var bl struct {
		Browsers []struct {
			ID      string `json:"id"`
			Profile string `json:"profile"`
			Pid     int    `json:"pid"`
		} `json:"browsers"`
	}
	h.must("browser.list", nil, &bl)
	if len(bl.Browsers) != 2 {
		t.Fatalf("browsers: %+v", bl)
	}
	// A crash: the browser's main process killed from outside.
	var crashed = bl.Browsers[0]
	if err := syscall.Kill(crashed.Pid, syscall.SIGKILL); err != nil {
		t.Fatal(err)
	}
	e, err := h.client.WaitEvent("browser.exited", "", 15*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	var data struct {
		BrowserID string   `json:"browser_id"`
		Crashed   bool     `json:"crashed"`
		Reason    string   `json:"reason"`
		Groups    []string `json:"groups"`
	}
	_ = json.Unmarshal(e.Data, &data)
	if data.BrowserID != crashed.ID || !data.Crashed || data.Reason != "crashed" || len(data.Groups) != 1 {
		t.Fatalf("exited: %s", e.Data)
	}
	if err := h.call("tab.list", map[string]any{"group_id": data.Groups[0]}, nil); code(err) != 1108 {
		t.Fatalf("a group whose browser crashed: %v", err)
	}
	// The other goes when idle, keeping its profile.
	h.manager.CloseIdle(time.Now().Add(2 * time.Minute))
	e, err = h.client.WaitEvent("browser.exited", "", 15*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	_ = json.Unmarshal(e.Data, &data)
	if data.Reason != "idle" || data.Crashed {
		t.Fatalf("idle: %s", e.Data)
	}
	var profiles struct {
		Profiles []struct {
			ID      string `json:"id"`
			Running bool   `json:"running"`
		} `json:"profiles"`
	}
	h.must("profile.list", nil, &profiles)
	if len(profiles.Profiles) != 2 {
		t.Fatalf("profiles: %+v", profiles)
	}
	// It opens again on the same profile.
	h.open("g2", "project-b", "/still")
	if err := h.call("profile.clear", map[string]any{"profile": "project-b"}, nil); code(err) != 1004 {
		t.Fatalf("clearing a profile in use: %v", err)
	}
	h.must("profile.delete", map[string]any{"profile": "project-a"}, nil)
	if _, err := os.Stat(h.manager.ProfileDir("project-a")); !os.IsNotExist(err) {
		t.Fatalf("the deleted profile is still there: %v", err)
	}
}
