package term

import (
	"context"
	"errors"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/internal/emulator"
	"github.com/ascorblack/daedalus/ptyd/internal/wire"
)

// Sink carries one client's browser frames to it: an attachment channel of the host's connection.
// Send fails once the channel or the connection is gone; Close closes the channel from this side.
type Sink interface {
	Send(frame []byte) error
	Close()
}

// ClientOptions describe a client as the host attaches it.
type ClientOptions struct {
	Kind     string // "human" or "viewer"; a viewer is always read-only
	Label    string
	Via      string
	ReadOnly bool
}

// Client kinds.
const (
	KindHuman  = "human"
	KindViewer = "viewer"
)

// ErrTooManyClients is returned by Attach past config.MaxClients.
var ErrTooManyClients = errors.New("too many clients attached to this terminal")

// inboxFrames bounds the frames waiting for a client's goroutine. When it is full the connection's
// reader waits, which pushes back on the browser's typing instead of queueing it here.
const inboxFrames = 64

// maxQueuedEvents bounds the events waiting for one client. Events that describe state (size,
// keyboard, modes, the title) replace their own pending copy, so the bound only ever drops the
// transient kinds, and only for a client that stopped reading.
const maxQueuedEvents = 64

// maxBurstFrames is how many OUTPUT frames one pass sends before the goroutine looks at its inbox
// again, so a client's keystrokes are not held up behind its own output.
const maxBurstFrames = 16

var clientSeq atomic.Int64

// Client is one attachment: a browser (through the host) or a viewer reading a terminal.
//
// Every frame to the client is sent by one goroutine, which owns the stream position; the reader of
// the connection only decodes and hands over. The position and the acknowledgement are the flow
// control: the goroutine stops sending once WindowBytes are unacknowledged, and never makes the
// terminal wait.
type Client struct {
	ID         string
	Kind       string
	Label      string
	Via        string
	AttachedAt time.Time

	t    *Terminal
	sink Sink

	readOnly atomic.Bool
	sent     atomic.Int64 // offset one past the last OUTPUT byte sent
	acked    atomic.Int64 // the highest offset the client has acknowledged, never past sent

	inbox      chan []byte
	poke       chan struct{}
	stop       chan struct{}
	stopOnce   sync.Once
	peerClosed atomic.Bool
	done       chan struct{}

	evMu   sync.Mutex
	events []queuedEvent

	// Under t.att.mu: what other goroutines read when they pick a size owner or answer a query.
	desired    *Size
	lastActive time.Time
	theme      *wire.Theme
	pending    []pendingReply
	attached   bool

	// Only the client's goroutine touches these.
	scrollback int
	stalled    bool
	lastSend   time.Time
	exitSent   bool
}

// Size is a terminal size in cells, with the pixel size the client measured (0 when unknown).
type Size struct {
	Cols, Rows int
	PxW, PxH   int
}

type queuedEvent struct {
	key string // events with a key replace a pending one with the same key
	v   any
}

// attachments is the terminal's side of its clients.
type attachments struct {
	mu         sync.Mutex
	clients    []*Client // in order of attachment
	owner      *Client   // the client that owns the size, nil when the host does or nobody does
	lastDetach time.Time
	answers    []answer // recent answers to queries, by output offset
	closed     bool     // the terminal was forgotten

	count atomic.Int32 // len(clients), read without the lock on the output path

	wakeMu sync.Mutex
	wake   chan struct{}

	typingActor string
	typingTimer *time.Timer
	kbTimer     *time.Timer
}

// outputWait returns a channel closed at the next notify. Taken before looking at the ring, so
// output that arrives in between is not missed.
func (a *attachments) outputWait() <-chan struct{} {
	a.wakeMu.Lock()
	defer a.wakeMu.Unlock()
	if a.wake == nil {
		a.wake = make(chan struct{})
	}
	return a.wake
}

// notify wakes every client: output arrived. It costs nothing without clients, which is the
// reader's usual case.
func (a *attachments) notify() {
	if a.count.Load() == 0 {
		return
	}
	a.wakeMu.Lock()
	if a.wake != nil {
		close(a.wake)
		a.wake = nil
	}
	a.wakeMu.Unlock()
}

// Attach adds a client. Its goroutine runs until the client detaches, the other side closes the
// channel, the terminal is forgotten, or ctx ends. Nothing is sent before the client's ATTACH frame.
// An exited terminal can still be attached, to see its last screen; typing into it goes nowhere.
func (t *Terminal) Attach(ctx context.Context, o ClientOptions, sink Sink) (*Client, error) {
	switch o.Kind {
	case "":
		o.Kind = KindHuman
	case KindHuman, KindViewer:
	default:
		return nil, errors.New("client kind must be human or viewer")
	}
	c := &Client{
		ID: "c" + strconv.FormatInt(clientSeq.Add(1), 10), Kind: o.Kind, Label: o.Label, Via: o.Via,
		AttachedAt: t.deps.Clock.Now().UTC(), t: t, sink: sink,
		inbox: make(chan []byte, inboxFrames), poke: make(chan struct{}, 1), stop: make(chan struct{}),
		done: make(chan struct{}), scrollback: config.DefaultSnapshotScrollback,
	}
	c.readOnly.Store(o.ReadOnly || o.Kind == KindViewer)
	a := &t.att
	a.mu.Lock()
	if a.closed {
		a.mu.Unlock()
		return nil, ErrForgotten
	}
	if len(a.clients) >= config.MaxClients {
		a.mu.Unlock()
		return nil, ErrTooManyClients
	}
	a.clients = append(a.clients, c)
	a.count.Store(int32(len(a.clients)))
	a.mu.Unlock()
	go c.run(ctx)
	t.broadcastClients()
	return c, nil
}

// Clients lists the attached clients.
func (t *Terminal) Clients() []ClientInfo {
	a := &t.att
	a.mu.Lock()
	defer a.mu.Unlock()
	out := make([]ClientInfo, 0, len(a.clients))
	for _, c := range a.clients {
		out = append(out, c.info())
	}
	return out
}

func (c *Client) info() ClientInfo {
	return ClientInfo{ID: c.ID, Kind: c.Kind, Label: c.Label, Via: c.Via, ReadOnly: c.readOnly.Load(),
		AttachedAt: c.AttachedAt}
}

// Frame takes one browser frame from the client. It runs on the connection's reader: an ACK is
// applied at once, so acknowledgements never wait behind typing; anything else is handed to the
// client's goroutine, waiting while its inbox is full.
func (c *Client) Frame(payload []byte) {
	if len(payload) == 9 && payload[0] == wire.TypeAck {
		if f, err := wire.DecodeBrowser(payload); err == nil {
			c.ack(int64(f.Seq))
		}
		return
	}
	select {
	case c.inbox <- payload:
	case <-c.stop:
	}
}

// ack raises the acknowledged offset. An acknowledgement of bytes never sent is taken as one of
// everything sent: a client cannot open the window wider by lying about what it parsed.
func (c *Client) ack(seq int64) {
	seq = min(seq, c.sent.Load())
	for {
		cur := c.acked.Load()
		if seq <= cur || c.acked.CompareAndSwap(cur, seq) {
			break
		}
	}
	c.wakeUp()
}

func (c *Client) wakeUp() {
	select {
	case c.poke <- struct{}{}:
	default:
	}
}

// Closed is the other side closing the channel. It runs on the connection's reader and does not
// wait for the goroutine.
func (c *Client) Closed() {
	c.peerClosed.Store(true)
	c.halt()
}

// Detach ends the client from this side, closes its channel, and waits until it is gone.
func (c *Client) Detach() {
	c.halt()
	<-c.done
}

// Done is closed once the client is gone.
func (c *Client) Done() <-chan struct{} { return c.done }

func (c *Client) halt() { c.stopOnce.Do(func() { close(c.stop) }) }

// run is the client's goroutine.
func (c *Client) run(ctx context.Context) {
	defer c.finish()
	ping := time.NewTicker(config.PingInterval)
	defer ping.Stop()
	var batch *time.Timer
	defer func() {
		if batch != nil {
			batch.Stop()
		}
	}()
	// The terminal's end is watched directly: its exit event is published a moment before the
	// terminal reports itself exited, so a wake-up at the event could find it still running.
	exited := c.t.Done()
	for {
		wake := c.t.att.outputWait()
		var retry time.Time
		if c.isAttached() {
			var err error
			if retry, err = c.pump(); err != nil {
				return
			}
		}
		var batchC <-chan time.Time
		if !retry.IsZero() {
			d := time.Until(retry)
			if batch == nil {
				batch = time.NewTimer(d)
			} else {
				batch.Reset(d)
			}
			batchC = batch.C
		}
		select {
		case <-ctx.Done():
			return
		case <-c.stop:
			return
		case p := <-c.inbox:
			if err := c.handle(p); err != nil {
				return
			}
			// Take whatever else is waiting before sending, so a burst of typing is one pass.
			for i := 0; i < inboxFrames; i++ {
				select {
				case p := <-c.inbox:
					if err := c.handle(p); err != nil {
						return
					}
					continue
				default:
				}
				break
			}
		case <-c.poke:
		case <-exited:
			exited = nil
		case <-wake:
		case <-batchC:
		case <-ping.C:
			if c.isAttached() {
				if err := c.event(map[string]any{"type": "ping", "at": c.t.deps.Clock.Now().UnixMilli()}); err != nil {
					return
				}
			}
		}
		if batchC != nil && !batch.Stop() {
			select {
			case <-batch.C:
			default:
			}
		}
	}
}

func (c *Client) isAttached() bool {
	c.t.att.mu.Lock()
	defer c.t.att.mu.Unlock()
	return c.attached
}

// finish removes the client, closes its channel unless the other side did, and hands the size to
// the most recently active client left.
func (c *Client) finish() {
	// A reader handing over a frame must never wait for a goroutine that is gone.
	c.halt()
	t := c.t
	a := &t.att
	a.mu.Lock()
	for i, x := range a.clients {
		if x == c {
			a.clients = append(a.clients[:i], a.clients[i+1:]...)
			break
		}
	}
	a.count.Store(int32(len(a.clients)))
	a.lastDetach = t.deps.Clock.Now().UTC()
	wasOwner := a.owner == c
	var next *Client
	if wasOwner {
		a.owner = nil
		next = a.successorLocked()
	}
	a.mu.Unlock()
	if !c.peerClosed.Load() {
		c.sink.Close()
	}
	close(c.done)
	if wasOwner {
		t.ownerLeft(next)
	}
	t.broadcastClients()
}

// handle processes one frame from the client other than an ACK.
func (c *Client) handle(p []byte) error {
	f, err := wire.DecodeBrowser(p)
	if err != nil {
		return c.errorEvent("bad_frame", err.Error())
	}
	switch f.Type {
	case wire.TypeAttach:
		return c.attach(f)
	case wire.TypeInput:
		if !c.isAttached() {
			return nil
		}
		c.input(f.Data)
		return nil
	case wire.TypeResize:
		if !c.isAttached() {
			return nil
		}
		return c.resize(Size{Cols: int(f.Cols), Rows: int(f.Rows), PxW: int(f.PxW), PxH: int(f.PxH)})
	case wire.TypeAck:
		c.ack(int64(f.Seq))
		return nil
	}
	return c.errorEvent("bad_frame", "a client sends INPUT, RESIZE, ACK and ATTACH frames only")
}

// attach answers an ATTACH frame: hello, then either the bytes the client is missing or a fresh
// screen. A second ATTACH on the same channel starts over the same way.
func (c *Client) attach(f wire.BrowserFrame) error {
	req, err := wire.DecodeAttach(f)
	if err != nil {
		return c.errorEvent("bad_frame", "ATTACH: "+err.Error())
	}
	t := c.t
	if req.ReadOnly {
		c.readOnly.Store(true)
	}
	c.scrollback = config.DefaultSnapshotScrollback
	if req.Scrollback != nil {
		c.scrollback = max(0, min(*req.Scrollback, config.MaxSnapshotScrollback))
	}
	t.att.mu.Lock()
	c.attached = true
	if req.Theme != nil {
		th := *req.Theme
		c.theme = &th
	}
	t.att.mu.Unlock()
	c.exitSent = false
	c.stalled = false
	if err := c.event(t.helloFor(c)); err != nil {
		return err
	}
	if err := c.event(t.clientsFor(c)); err != nil {
		return err
	}
	lastSeq := int64(req.LastSeq)
	reason := t.replayable(req.HaveState, lastSeq)
	if reason != "" {
		return c.snapshot(reason)
	}
	c.sent.Store(lastSeq)
	c.acked.Store(lastSeq)
	return nil
}

// replayable decides whether a client that holds the stream up to lastSeq can be brought up to date
// with the bytes after it. It says why not, or "" when it can. Bytes drawn for another size are
// garbage on the client's screen, so a resize after lastSeq means a fresh screen; so does a backlog
// larger than a screen is worth.
func (t *Terminal) replayable(haveState bool, lastSeq int64) string {
	head, start := t.ring.Head(), t.ring.Start()
	t.mu.Lock()
	resized := t.lastResizeSeq
	t.mu.Unlock()
	switch {
	case !haveState:
		return "attach"
	case lastSeq < start || lastSeq > head:
		return "ring"
	case resized > lastSeq:
		return "resized"
	case head-lastSeq > config.ResyncBacklogBytes:
		return "backlog"
	}
	return ""
}

// pump sends what the client is due: pending events, then output or a snapshot, then the exit. It
// returns when to look again: at once when there is more to send than one pass sends, later for a
// batch not yet worth a frame, never (zero) when the client is up to date or its window is full.
func (c *Client) pump() (retry time.Time, err error) {
	if err := c.flushEvents(); err != nil {
		return time.Time{}, err
	}
	t := c.t
	running := t.Running()
	for i := 0; ; i++ {
		head := t.ring.Head()
		sent := c.sent.Load()
		if sent >= head {
			break
		}
		if i == maxBurstFrames {
			return time.Now(), nil
		}
		acked := c.acked.Load()
		room := config.DefaultWindowBytes - (sent - acked)
		if room <= 0 {
			c.stalled = true
			return time.Time{}, nil
		}
		start := t.ring.Start()
		lagged := sent < start
		if c.stalled {
			// Sending resumes after the window was full. The client fell behind; when what it missed
			// has left the ring or is more than a screen is worth, it gets the screen instead.
			c.stalled = false
			lagged = lagged || acked < start || head-acked > config.ResyncBacklogBytes
		}
		if lagged {
			if err := c.snapshot("lagged"); err != nil {
				return time.Time{}, err
			}
			return time.Now(), nil
		}
		avail := head - sent
		now := time.Now()
		if avail < config.OutputBatchBytes && now.Sub(c.lastSend) < config.OutputBatchDelay {
			return c.lastSend.Add(config.OutputBatchDelay), nil
		}
		n := min(avail, int64(config.OutputBatchBytes), room)
		data, from, gap := t.ring.ReadFrom(sent, int(n))
		if gap {
			// The ring moved on between the look at its start and the read.
			continue
		}
		if len(data) == 0 {
			break
		}
		to := from + int64(len(data))
		c.sent.Store(to)
		if err := c.sink.Send(wire.EncodeOutput(uint64(from), data)); err != nil {
			return time.Time{}, err
		}
		c.lastSend = now
		t.forwarded(c, from, to)
	}
	if !running && !c.exitSent && c.sent.Load() >= t.ring.Head() {
		c.exitSent = true
		exit, _ := t.Exit()
		var sig any
		if exit.Signal != "" {
			sig = exit.Signal
		}
		if err := c.event(map[string]any{"type": "exit", "code": exit.Code, "signal": sig}); err != nil {
			return time.Time{}, err
		}
	}
	return time.Time{}, nil
}

// snapshot sends a fresh screen: `resync`, then SNAPSHOT at the emulator's size, and the stream
// continues from the offset the screen was taken at. A screen whose scrollback does not fit in one
// frame is taken again with half of it.
func (c *Client) snapshot(reason string) error {
	t := c.t
	lines := c.scrollback
	var (
		vt   []byte
		info emulator.SnapshotInfo
		seq  int64
	)
	tooLarge := false
	for {
		err := t.WithEmulator(func(e emulator.Emulator) {
			vt, info = e.Snapshot(emulator.SnapshotOptions{Scrollback: lines})
			seq = t.fed.Load()
			if info.Cols == 0 || info.Rows == 0 {
				info.Cols, info.Rows = e.Size()
			}
		})
		if err != nil {
			return err
		}
		if 13+len(vt) <= wire.MaxPayload {
			break
		}
		if lines == 0 {
			// Even the bare screen does not fit (a huge grid of styled cells). An empty screen at the
			// right offset keeps the stream consistent; the program's next redraw fills it in.
			vt, tooLarge = nil, true
			break
		}
		lines /= 2
	}
	if err := c.event(map[string]any{"type": "resync", "reason": reason, "first_abs_row": info.FirstAbsRow}); err != nil {
		return err
	}
	c.sent.Store(seq)
	// The window starts again from the screen: the client acknowledges it as soon as it is parsed.
	c.acked.Store(seq)
	c.stalled = false
	if err := c.sink.Send(wire.EncodeSnapshot(uint16(info.Cols), uint16(info.Rows), uint64(seq), vt)); err != nil {
		return err
	}
	c.lastSend = time.Now()
	if tooLarge {
		return c.errorEvent("snapshot_too_large", "the screen does not fit in one frame; it was sent empty")
	}
	return nil
}

// event sends one EVENT frame now, from the client's goroutine.
func (c *Client) event(v any) error {
	b, err := wire.EncodeEvent(v)
	if err != nil {
		return nil
	}
	return c.sink.Send(b)
}

func (c *Client) errorEvent(code, message string) error {
	return c.event(map[string]any{"type": "error", "code": code, "message": message})
}

// queue hands an event to the client's goroutine. key, when set, replaces a pending event with the
// same key: only the latest size, keyboard or title matters.
func (c *Client) queue(key string, v any) {
	c.evMu.Lock()
	replaced := false
	if key != "" {
		for i := range c.events {
			if c.events[i].key == key {
				c.events[i].v = v
				replaced = true
				break
			}
		}
	}
	if !replaced && len(c.events) < maxQueuedEvents {
		c.events = append(c.events, queuedEvent{key: key, v: v})
	}
	c.evMu.Unlock()
	c.wakeUp()
}

func (c *Client) flushEvents() error {
	c.evMu.Lock()
	evs := c.events
	c.events = nil
	c.evMu.Unlock()
	for _, e := range evs {
		v := e.v
		if f, ok := v.(func() any); ok {
			v = f()
		}
		if err := c.event(v); err != nil {
			return err
		}
	}
	return nil
}

// broadcast queues an event for every attached client. build makes the client's own copy (the size
// owner is "you" to one and "other" to the rest).
func (t *Terminal) broadcast(key string, build func(c *Client) any) {
	a := &t.att
	a.mu.Lock()
	clients := make([]*Client, 0, len(a.clients))
	for _, c := range a.clients {
		if c.attached {
			clients = append(clients, c)
		}
	}
	a.mu.Unlock()
	for _, c := range clients {
		c.queue(key, build(c))
	}
}

// broadcastClients tells every client who else is attached.
func (t *Terminal) broadcastClients() {
	t.broadcast("clients", func(c *Client) any { return func() any { return t.clientsFor(c) } })
}

// clientsFor is the `clients` event as c sees it: how many are attached, and the others.
func (t *Terminal) clientsFor(c *Client) map[string]any {
	others := []ClientInfo{}
	for _, x := range t.Clients() {
		if x.ID != c.ID {
			others = append(others, x)
		}
	}
	return map[string]any{"type": "clients", "count": len(others) + 1, "others": others}
}

// helloFor is the first event a client receives after its ATTACH.
func (t *Terminal) helloFor(c *Client) map[string]any {
	kb := t.in.Keyboard()
	t.mu.Lock()
	status := "running"
	if !t.Running() {
		status = "exited"
	}
	term := map[string]any{"id": t.ID, "title": t.title, "cwd": t.cwd, "status": status, "cols": t.cols, "rows": t.rows}
	size := map[string]any{"cols": t.cols, "rows": t.rows, "owner": t.ownerForLocked(c)}
	modes := t.modes
	t.mu.Unlock()
	return map[string]any{
		"type": "hello", "client_id": c.ID, "read_only": c.readOnly.Load(),
		"ack_bytes": config.DefaultAckBytes, "window_bytes": config.DefaultWindowBytes,
		"terminal": term, "size": size, "keyboard": keyboardState(kb), "modes": clientModes(modes),
	}
}

func clientModes(m ModesInfo) map[string]any {
	return map[string]any{"alt_screen": m.AltScreen, "mouse": m.Mouse, "bracketed_paste": m.BracketedPaste,
		"app_cursor": m.AppCursor}
}

// closeAttachments ends every client when the terminal is forgotten.
func (t *Terminal) closeAttachments() {
	a := &t.att
	a.mu.Lock()
	a.closed = true
	clients := append([]*Client(nil), a.clients...)
	if a.typingTimer != nil {
		a.typingTimer.Stop()
	}
	if a.kbTimer != nil {
		a.kbTimer.Stop()
	}
	a.mu.Unlock()
	for _, c := range clients {
		c.halt()
	}
}

// LastDetach is when a client last left, zero if none has.
func (t *Terminal) LastDetach() time.Time {
	t.att.mu.Lock()
	defer t.att.mu.Unlock()
	return t.att.lastDetach
}

// attachPublisher passes the terminal's events on to the daemon's publisher and to its clients.
// Clients get them without the daemon's rate limits: their own queue keeps only the latest of each
// kind, which bounds them the same way without delaying a title by a quarter of a second.
type attachPublisher struct {
	inner Publisher
	t     *Terminal
}

func (p attachPublisher) Publish(typ, terminalID string, data any) {
	p.inner.Publish(typ, terminalID, data)
	p.t.clientEvent(typ, data)
}

func (p attachPublisher) Flush(terminalID string) { p.inner.Flush(terminalID) }

// clientEvent turns a terminal event into the clients' EVENT, when they have one.
func (t *Terminal) clientEvent(typ string, data any) {
	m, _ := data.(map[string]any)
	switch typ {
	case "terminal.title":
		t.broadcast("title", func(*Client) any { return map[string]any{"type": "title", "title": m["title"]} })
	case "terminal.cwd":
		t.broadcast("cwd", func(*Client) any { return map[string]any{"type": "cwd", "cwd": m["cwd"]} })
	case "terminal.bell":
		t.broadcast("bell", func(*Client) any { return map[string]any{"type": "bell"} })
	case "terminal.notify":
		t.broadcast("", func(*Client) any {
			return map[string]any{"type": "notify", "title": m["title"], "body": m["body"]}
		})
	case "terminal.progress":
		t.broadcast("progress", func(*Client) any {
			return map[string]any{"type": "progress", "state": m["state"], "value": m["value"]}
		})
	case "terminal.mode":
		if mi, ok := data.(ModesInfo); ok {
			ev := clientModes(mi)
			ev["type"] = "mode"
			t.broadcast("mode", func(*Client) any { return ev })
		}
	}
}

// DaemonBytes is the memory the terminal holds inside the daemon beyond its emulator: the output
// ring as allocated now. The load estimate adds it to the terminal's processes, which it is not part
// of.
func (t *Terminal) DaemonBytes() int64 { return int64(t.ring.Allocated()) }
