package hooks

import (
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
)

// validName is what a hook's name may be (the last segment of its URL): Claude's event names,
// "team", a CLI's own name.
var validName = regexp.MustCompile(`^[A-Za-z0-9._-]{1,64}$`)

// Listen opens the hook listener on a loopback address and serves it until the returned server is
// closed. The base URL of every launch is set from the address it actually got.
func (r *Registry) Listen(addr string) (*http.Server, net.Listener, error) {
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return nil, nil, err
	}
	r.SetBase("http://" + ln.Addr().String())
	srv := &http.Server{
		Handler:           r,
		ReadHeaderTimeout: 5 * time.Second,
		// The body is read before anything waits; a held post then waits with no deadline of the
		// server's own, bounded by its hold instead.
		ReadTimeout:    30 * time.Second,
		IdleTimeout:    time.Minute,
		MaxHeaderBytes: 16 << 10,
	}
	go func() { _ = srv.Serve(ln) }()
	return srv, ln, nil
}

// ServeHTTP is `POST /hook/<launch_id>/<name>[?wait_ms=N]`.
//
// 204: the event was published (and, when held, no reply came in time). 200: the host's reply, with
// its body. 401: a wrong token. 410: no such launch, or it ended — the CLI is told its launch is
// over, and nothing is published. 413: a body over a megabyte. 429: faster than a launch may post,
// or too many posts waiting at once.
func (r *Registry) ServeHTTP(w http.ResponseWriter, req *http.Request) {
	if req.Method != http.MethodPost {
		w.Header().Set("Allow", "POST")
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	rest, ok := strings.CutPrefix(req.URL.Path, "/hook/")
	launchID, name, ok2 := strings.Cut(rest, "/")
	if !ok || !ok2 || !validID.MatchString(launchID) || !validName.MatchString(name) {
		http.Error(w, "not a hook URL", http.StatusNotFound)
		return
	}
	l, ok := r.Get(launchID)
	if !ok {
		http.Error(w, "no such launch", http.StatusGone)
		return
	}
	token, _ := strings.CutPrefix(req.Header.Get("Authorization"), "Bearer ")
	if subtle.ConstantTimeCompare([]byte(token), []byte(l.Token)) != 1 {
		http.Error(w, "wrong token", http.StatusUnauthorized)
		return
	}
	if !l.limiter.allow() {
		http.Error(w, "too many posts", http.StatusTooManyRequests)
		return
	}
	body, err := io.ReadAll(io.LimitReader(req.Body, config.HookBodyBytes+1))
	if err != nil {
		http.Error(w, "reading the body: "+err.Error(), http.StatusBadRequest)
		return
	}
	if len(body) > config.HookBodyBytes {
		http.Error(w, "the body is larger than a megabyte", http.StatusRequestEntityTooLarge)
		return
	}
	hold, err := holdOf(req, body)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	hold = min(hold, l.holdMax)

	var h *held
	if hold > 0 {
		if h, err = r.hold(l); err != nil {
			status := http.StatusTooManyRequests
			if errors.Is(err, ErrNoLaunch) {
				status = http.StatusGone
			}
			http.Error(w, err.Error(), status)
			return
		}
	}
	// Published only once the reply is registered, so a host that answers at once finds it waiting.
	data, size, truncated := eventBody(body, config.HookEventBody)
	ev := map[string]any{"launch_id": l.ID, "terminal_id": l.Primary(), "name": name, "body": data, "size": len(body)}
	if truncated {
		ev["truncated"] = true
	}
	if h != nil {
		ev["reply_id"] = h.id
		ev["hold_ms"] = hold.Milliseconds()
	}
	r.pub.PublishSized("hook", l.Primary(), ev, size+256)
	if h == nil {
		w.WriteHeader(http.StatusNoContent)
		return
	}

	timer := time.NewTimer(hold)
	defer timer.Stop()
	select {
	case reply := <-h.ch:
		if reply.Gone {
			http.Error(w, "the launch ended", http.StatusGone)
			return
		}
		if reply.ContentType != "" {
			w.Header().Set("Content-Type", reply.ContentType)
		}
		w.Header().Set("Content-Length", strconv.Itoa(len(reply.Body)))
		w.WriteHeader(reply.Status)
		_, _ = w.Write(reply.Body)
	case <-timer.C:
		r.release(h)
		// The CLI goes on as it would with no hook answering: an empty success.
		w.WriteHeader(http.StatusNoContent)
	case <-req.Context().Done():
		r.release(h)
	}
}

// holdOf is how long a post asks to be held: `wait_ms` (or `hold_ms`) in the query, or a
// `daedalus_hold_ms` field of a JSON object body, for callers that cannot change their URL per post.
func holdOf(req *http.Request, body []byte) (time.Duration, error) {
	q := req.URL.Query()
	for _, key := range []string{"wait_ms", "hold_ms"} {
		if v := q.Get(key); v != "" {
			ms, err := strconv.ParseInt(v, 10, 64)
			if err != nil || ms < 0 {
				return 0, errors.New(key + " must be a number of milliseconds")
			}
			return min(time.Duration(ms)*time.Millisecond, config.MaxHookHold), nil
		}
	}
	if len(body) > 0 && body[0] == '{' {
		var probe struct {
			HoldMs *int64 `json:"daedalus_hold_ms"`
		}
		if json.Unmarshal(body, &probe) == nil && probe.HoldMs != nil && *probe.HoldMs > 0 {
			return min(time.Duration(*probe.HoldMs)*time.Millisecond, config.MaxHookHold), nil
		}
	}
	return 0, nil
}
