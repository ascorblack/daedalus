// Package toolsmcp is `ptyd tools-mcp`: a Model Context Protocol server on stdio that gives a
// command-line staff member one set of Daedalus's tools — the team's own (`team`, also started as
// `ptyd team-mcp`), or a set the host describes in a launch file, such as the browser's.
//
// The CLI starts it from its per-launch MCP configuration, so it inherits the launch's environment
// and speaks for that launch alone. It lives in ptyd because ptyd is the one program present in
// every environment a CLI runs in: the operator's machine has no Daedalus Python. It knows nothing
// about staff, projects, browsers or orchestrators — it checks the arguments, posts them, and hands
// back what the host answered.
package toolsmcp

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/internal/version"
)

// ServerName is the name the team set's server gives itself; the tools appear to Claude Code as
// `mcp__daedalus_team__Report`, because the launch's configuration names the server the same.
const ServerName = "daedalus_team"

// TeamSet is the team's own tools, compiled in: their wire is the one `ptyd team-mcp` always spoke.
const TeamSet = "team"

// knownVersions are the protocol revisions this server answers in, newest first. The tools use
// nothing any revision changed, so a client's own is echoed whenever it is one of these; an unknown
// one is answered with the newest, as the protocol asks, and logged.
var knownVersions = []string{"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}

// Default holds, overridden per launch by DAEDALUS_ASK_HOLD_MS and DAEDALUS_REPORT_HOLD_MS. A
// question waits long enough for an orchestrator to wake and answer; a report waits only for the
// host to accept it or say why not ("commit first"), and a silent host still counts it as recorded.
const (
	DefaultAskHold    = 5 * time.Minute
	DefaultReportHold = 15 * time.Second
)

const (
	// maxLine is the longest message read from the client; a longer line is skipped whole.
	maxLine = 4 << 20
	// maxCalls bounds the calls in flight: each may hold a post at the listener, which has its own
	// ceiling per launch.
	maxCalls = 16
)

// JSON-RPC error codes.
const (
	codeParse          = -32700
	codeInvalidRequest = -32600
	codeNoMethod       = -32601
	codeInvalidParams  = -32602
)

// Serve is `ptyd team-mcp`: the team set, as ServeSet serves it.
func Serve(ctx context.Context, env func(string) string, in io.Reader, out io.Writer, logw io.Writer) error {
	return ServeSet(ctx, TeamSet, env, in, out, logw)
}

// ServeSet answers MCP messages from in on out until in ends or ctx is done, with the tools of one
// set. Log lines go to logw (the CLI shows a server's stderr in its MCP diagnostics). Calls still
// waiting when the client goes are abandoned, which releases their held posts at the listener.
func ServeSet(ctx context.Context, set string, env func(string) string, in io.Reader, out io.Writer, logw io.Writer) error {
	s := &server{out: out, logw: logw, calls: map[string]context.CancelFunc{}, callPrefix: callPrefix(env("DAEDALUS_LAUNCH_ID")), logPrefix: "ptyd team-mcp"}
	if set != TeamSet {
		s.logPrefix = "ptyd tools-mcp --set " + set
	}
	var err error
	if set == TeamSet {
		s.set = newTeamSet(holdFrom(env, "DAEDALUS_ASK_HOLD_MS", DefaultAskHold, s.logf), holdFrom(env, "DAEDALUS_REPORT_HOLD_MS", DefaultReportHold, s.logf))
	} else {
		// A set whose file cannot be read still serves, with no tools: the CLI's diagnostics show the
		// server and this log line, rather than a server that would not start.
		s.set, err = loadFileSet(set, env, s.logf)
		if err != nil {
			s.logf("%v", err)
			s.setErr = err
		}
	}
	route, err := routeFrom(env, set == TeamSet)
	if err != nil {
		// The server still starts and lists its tools, so the CLI shows why a call fails instead of a
		// server that would not start.
		s.logf("%v", err)
		s.routeErr = err
	}
	s.route = route

	ctx, cancel := context.WithCancel(ctx)
	defer func() {
		cancel()
		s.wg.Wait()
	}()
	r := bufio.NewReaderSize(in, 64<<10)
	for {
		line, err := readLine(r)
		if len(bytes.TrimSpace(line)) > 0 {
			s.handle(ctx, line)
		}
		if errors.Is(err, errTooLong) {
			s.logf("a message longer than %d bytes was skipped", maxLine)
			s.reply(nil, nil, &rpcError{Code: codeInvalidRequest, Message: "message too large"})
			continue
		}
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
		if s.writeFailed() {
			return errors.New("the client stopped reading")
		}
	}
}

type server struct {
	out       io.Writer
	logw      io.Writer
	logPrefix string
	set       toolset
	setErr    error
	route     route
	routeErr  error

	wmu     sync.Mutex
	wfailed bool

	mu    sync.Mutex
	calls map[string]context.CancelFunc
	wg    sync.WaitGroup

	// callPrefix and seq make every call's id: the launch, this process, and the call's number. The
	// host dedupes on it, so a post it sees twice — replayed after a restart of the host — makes one
	// report and one question, not two. The process part keeps ids apart when the CLI restarts this
	// server within the same launch.
	callPrefix string
	seq        atomic.Int64
}

func callPrefix(launch string) string {
	nonce := make([]byte, 4)
	_, _ = rand.Read(nonce)
	if launch == "" {
		launch = "nolaunch"
	}
	return launch + ":" + hex.EncodeToString(nonce)
}

type message struct {
	ID     json.RawMessage `json:"id"`
	Method string          `json:"method"`
	Params json.RawMessage `json:"params"`
}

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

func (s *server) handle(ctx context.Context, line []byte) {
	if line = bytes.TrimSpace(line); line[0] == '[' {
		// Batches left the protocol in 2025-06-18 and no CLI sends them.
		s.reply(nil, nil, &rpcError{Code: codeInvalidRequest, Message: "batches are not supported"})
		return
	}
	var m message
	if err := json.Unmarshal(line, &m); err != nil {
		s.reply(nil, nil, &rpcError{Code: codeParse, Message: "not JSON"})
		return
	}
	if m.Method == "" {
		return // a response to a request of ours; this server sends none
	}
	isRequest := len(m.ID) > 0 && string(m.ID) != "null"
	if !isRequest {
		if m.Method == "notifications/cancelled" {
			s.cancel(m.Params)
		}
		return // notifications/initialized and the rest need nothing
	}
	switch m.Method {
	case "initialize":
		result, client := s.initialize(m.Params)
		s.reply(m.ID, result, nil)
		s.announce(ctx, "initialize", client)
	case "ping":
		s.reply(m.ID, struct{}{}, nil)
	case "tools/list":
		s.reply(m.ID, map[string]any{"tools": s.set.list()}, nil)
		s.announce(ctx, "tools/list", nil)
	case "tools/call":
		s.startCall(ctx, m)
	default:
		s.reply(m.ID, nil, &rpcError{Code: codeNoMethod, Message: "no method " + strconv.Quote(m.Method)})
	}
}

// announce posts that the CLI reached a stage of loading the tools, in the background: the CLI's
// handshake never waits on the host.
func (s *server) announce(ctx context.Context, stage string, client map[string]any) {
	if s.route == nil {
		return
	}
	fields := map[string]any{"stage": stage}
	if client != nil {
		fields["client"] = client
	}
	source := s.set.source()
	for k, v := range s.set.hello() {
		fields[k] = v
	}
	s.wg.Add(1)
	go func() {
		defer s.wg.Done()
		s.route.hello(ctx, source, fields)
	}()
}

func (s *server) initialize(params json.RawMessage) (map[string]any, map[string]any) {
	var p struct {
		ProtocolVersion string `json:"protocolVersion"`
		ClientInfo      struct {
			Name    string `json:"name"`
			Version string `json:"version"`
		} `json:"clientInfo"`
	}
	_ = json.Unmarshal(params, &p)
	answer := knownVersions[0]
	if contains(knownVersions, p.ProtocolVersion) {
		answer = p.ProtocolVersion
	} else {
		s.logf("client %s %s proposed protocol %q; answering %s", p.ClientInfo.Name, p.ClientInfo.Version, p.ProtocolVersion, answer)
	}
	client := map[string]any{"name": p.ClientInfo.Name, "version": p.ClientInfo.Version, "protocol": p.ProtocolVersion}
	return map[string]any{
		"protocolVersion": answer,
		"capabilities":    map[string]any{"tools": map[string]any{"listChanged": false}},
		"serverInfo":      map[string]any{"name": s.set.name(), "version": version.Version},
		"instructions":    s.set.instructions(),
	}, client
}

// startCall runs a tools/call on its own, because AskOrchestrator waits minutes (and a browser's
// action waits for the operator's approval) and the client may send pings, other calls or a
// cancellation meanwhile.
func (s *server) startCall(ctx context.Context, m message) {
	var p struct {
		Name      string          `json:"name"`
		Arguments json.RawMessage `json:"arguments"`
	}
	if err := json.Unmarshal(m.Params, &p); err != nil || p.Name == "" {
		s.reply(m.ID, nil, &rpcError{Code: codeInvalidParams, Message: "tools/call needs a name"})
		return
	}
	if s.setErr != nil {
		s.reply(m.ID, toolResult("these tools are not available in this launch: "+s.setErr.Error(), true), nil)
		return
	}
	req, err := s.set.decode(p.Name, p.Arguments)
	if errors.Is(err, errUnknownTool) {
		s.reply(m.ID, nil, &rpcError{Code: codeInvalidParams, Message: "no tool " + strconv.Quote(p.Name)})
		return
	}
	if err != nil {
		s.reply(m.ID, toolResult(err.Error(), true), nil)
		return
	}
	if s.route == nil {
		s.reply(m.ID, toolResult(s.set.unreachable()+": "+s.routeErr.Error(), true), nil)
		return
	}
	req.fields["call_id"] = s.callPrefix + ":" + strconv.FormatInt(s.seq.Add(1), 10)
	key := string(m.ID)
	callCtx, cancel := context.WithCancel(ctx)
	s.mu.Lock()
	if _, dup := s.calls[key]; dup || len(s.calls) >= maxCalls {
		s.mu.Unlock()
		cancel()
		s.reply(m.ID, toolResult("too many calls at once; wait for one to finish", true), nil)
		return
	}
	s.calls[key] = cancel
	s.wg.Add(1)
	s.mu.Unlock()

	hold := req.hold
	go func() {
		defer s.wg.Done()
		defer func() {
			s.mu.Lock()
			delete(s.calls, key)
			s.mu.Unlock()
			cancel()
		}()
		text, isErr := s.route.call(callCtx, req, hold)
		// A cancelled call gets no response: the client said it no longer wants one, or it is gone.
		if callCtx.Err() != nil {
			return
		}
		s.reply(m.ID, toolResult(text, isErr), nil)
	}()
}

func (s *server) cancel(params json.RawMessage) {
	var p struct {
		RequestID json.RawMessage `json:"requestId"`
	}
	if json.Unmarshal(params, &p) != nil || len(p.RequestID) == 0 {
		return
	}
	s.mu.Lock()
	cancel := s.calls[string(bytes.TrimSpace(p.RequestID))]
	s.mu.Unlock()
	if cancel != nil {
		cancel()
	}
}

func toolResult(text string, isErr bool) map[string]any {
	return map[string]any{"content": []map[string]any{{"type": "text", "text": text}}, "isError": isErr}
}

// reply writes one response. id nil is JSON null, for a message whose id could not be read.
func (s *server) reply(id json.RawMessage, result any, e *rpcError) {
	msg := map[string]any{"jsonrpc": "2.0", "id": id}
	if id == nil {
		msg["id"] = nil
	}
	if e != nil {
		msg["error"] = e
	} else {
		msg["result"] = result
	}
	b, err := json.Marshal(msg)
	if err != nil {
		s.logf("encoding a response: %v", err)
		return
	}
	s.wmu.Lock()
	defer s.wmu.Unlock()
	if s.wfailed {
		return
	}
	if _, err := s.out.Write(append(b, '\n')); err != nil {
		s.wfailed = true
	}
}

func (s *server) writeFailed() bool {
	s.wmu.Lock()
	defer s.wmu.Unlock()
	return s.wfailed
}

func (s *server) logf(format string, args ...any) {
	if s.logw != nil {
		fmt.Fprintf(s.logw, s.logPrefix+": "+format+"\n", args...)
	}
}

// holdFrom reads a hold in milliseconds from the environment, within what the listener allows.
func holdFrom(env func(string) string, name string, def time.Duration, logf func(string, ...any)) time.Duration {
	v := env(name)
	if v == "" {
		return def
	}
	ms, err := strconv.ParseInt(v, 10, 64)
	if err != nil || ms < 0 {
		logf("%s=%q is not a number of milliseconds; using %d", name, v, def.Milliseconds())
		return def
	}
	return min(time.Duration(ms)*time.Millisecond, config.MaxHookHold)
}

var errTooLong = errors.New("line too long")

// readLine reads one newline-terminated message. A line over maxLine is consumed and reported as
// errTooLong, so one oversized message cannot grow the buffer without bound or end the session.
func readLine(r *bufio.Reader) ([]byte, error) {
	var line []byte
	for {
		chunk, err := r.ReadSlice('\n')
		if len(line)+len(chunk) > maxLine {
			for errors.Is(err, bufio.ErrBufferFull) {
				_, err = r.ReadSlice('\n')
			}
			if err != nil && err != io.EOF {
				return nil, err
			}
			return nil, errTooLong
		}
		line = append(line, chunk...)
		if errors.Is(err, bufio.ErrBufferFull) {
			continue
		}
		return line, err
	}
}
