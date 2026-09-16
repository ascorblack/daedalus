package main

import (
	"context"
	"crypto/subtle"
	"embed"
	"encoding/json"
	"errors"
	"fmt"
	"html/template"
	"io/fs"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"
)

//go:embed ui
var uiFiles embed.FS

var uiTemplates = template.Must(template.ParseFS(uiFiles, "ui/*.html"))

// Server is the launcher's own page: the questions on a first run, and the status with the same
// buttons the command line offers afterwards. It listens on the loopback address only — it starts
// and stops containers, and nothing outside this machine has any business doing that.
type Server struct {
	app  *App
	port int

	// csrf is minted once per process and has to come back with anything that changes something.
	// Loopback is not a boundary: every page the operator opens can reach this port too.
	csrf    string
	csrfErr error

	http *http.Server

	once  sync.Once
	saved chan struct{} // closed when the setup form has been written
}

const defaultPort = 8770

// csrfHeader carries the token on an action. A custom header cannot be set on a cross-site form
// post and turns a fetch into a preflighted request, so a page that is not this one cannot send it
// at all — the token is what stops a page that somehow can.
const csrfHeader = "X-Daedalus-Desktop"

func NewServer(app *App, port int) *Server {
	token, err := randomSecret()
	return &Server{app: app, port: port, saved: make(chan struct{}), csrf: token, csrfErr: err}
}

// URL is where the operator reaches the launcher page.
func (s *Server) URL() string { return fmt.Sprintf("http://127.0.0.1:%d/", s.port) }

// Start begins serving. The listener is opened before returning, so a caller that opens a browser
// straight afterwards finds the page there.
func (s *Server) Start() error {
	if s.csrfErr != nil {
		return fmt.Errorf("the launcher page cannot tell its own requests apart: %w", s.csrfErr)
	}
	listener, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", s.port))
	if err != nil {
		return fmt.Errorf("the launcher page cannot listen on port %d: %w (another launcher may be running; --port picks another)", s.port, err)
	}
	// Port 0 asks the operating system for a free port; the page's own address has to be the one
	// it actually got.
	if addr, ok := listener.Addr().(*net.TCPAddr); ok {
		s.port = addr.Port
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/", s.handleIndex)
	mux.HandleFunc("/setup", s.handleSetup)
	mux.HandleFunc("/api/status", s.handleStatus)
	mux.HandleFunc("/api/action/", s.handleAction)
	mux.Handle("/assets/", http.StripPrefix("/assets/", http.FileServer(http.FS(mustSub()))))
	s.http = &http.Server{Handler: s.sameOrigin(mux), ReadHeaderTimeout: 10 * time.Second}
	go func() {
		if err := s.http.Serve(listener); err != nil && !errors.Is(err, http.ErrServerClosed) {
			fmt.Println("the launcher page stopped:", err)
		}
	}()
	return nil
}

func (s *Server) Stop(ctx context.Context) {
	if s.http != nil {
		_ = s.http.Shutdown(ctx)
	}
}

// WaitForSetup blocks until the form has been submitted, or the context ends.
func (s *Server) WaitForSetup(ctx context.Context) error {
	select {
	case <-s.saved:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// sameOrigin refuses anything that changes state and did not come from this page. A form post and a
// bodyless fetch both travel cross-site without a preflight, so any page the operator happens to
// open could otherwise rewrite the provider keys and the Telegram identity the whole installation
// trusts, and then restart the stack under them. Three things have to agree: the browser's own
// account of where the request came from, the Origin it names when it names one, and the address
// the request was aimed at — the last of which is what keeps a hostname resolved to 127.0.0.1 from
// standing in for the loopback address.
func (s *Server) sameOrigin(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet || r.Method == http.MethodHead {
			next.ServeHTTP(w, r)
			return
		}
		site := r.Header.Get("Sec-Fetch-Site")
		if site != "" && site != "same-origin" && site != "none" {
			refuse(w)
			return
		}
		if origin := r.Header.Get("Origin"); origin != "" && !s.ownAddress(strings.TrimPrefix(origin, "http://")) {
			refuse(w)
			return
		}
		if !s.ownAddress(r.Host) {
			refuse(w)
			return
		}
		next.ServeHTTP(w, r)
	})
}

// ownAddress reports whether a host:port names this very page. Both spellings of the loopback
// address are accepted because a browser uses whichever one the operator typed.
func (s *Server) ownAddress(hostPort string) bool {
	return hostPort == fmt.Sprintf("127.0.0.1:%d", s.port) || hostPort == fmt.Sprintf("localhost:%d", s.port)
}

// hasToken reports whether a request carries the token this process minted. The comparison is
// constant time out of habit rather than out of need: the token never leaves the machine.
func (s *Server) hasToken(value string) bool {
	return subtle.ConstantTimeCompare([]byte(value), []byte(s.csrf)) == 1
}

func refuse(w http.ResponseWriter) {
	http.Error(w, "the launcher answers its own page only", http.StatusForbidden)
}

type pageData struct {
	Setup         Setup
	Status        Status
	DockerMissing string
	LauncherURL   string
	CSRF          string
}

func (s *Server) handleIndex(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	if !s.app.paths.Configured() {
		http.Redirect(w, r, "/setup", http.StatusSeeOther)
		return
	}
	s.render(w, r, "status.html")
}

func (s *Server) handleSetup(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodPost {
		if err := r.ParseForm(); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		if !s.hasToken(r.PostFormValue("csrf")) {
			refuse(w)
			return
		}
		setup := Setup{
			DeepseekKey:   r.PostFormValue("deepseek"),
			OpenrouterKey: r.PostFormValue("openrouter"),
			OpencodeKey:   r.PostFormValue("opencode"),
			BotToken:      r.PostFormValue("bot_token"),
			OwnerID:       r.PostFormValue("owner_id"),
			APIID:         r.PostFormValue("api_id"),
			APIHash:       r.PostFormValue("api_hash"),
			USDPerDay:     r.PostFormValue("usd_per_day"),
			Clear:         clearedFields(r.PostForm["clear"]),
		}
		if err := WriteSetup(s.app.paths, setup); err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		s.app.log("configuration written to %s", s.app.paths.Env)
		s.once.Do(func() { close(s.saved) })
		http.Redirect(w, r, "/", http.StatusSeeOther)
		return
	}
	s.render(w, r, "setup.html")
}

// clearedFields turns the ticked "remove" boxes into the set WriteSetup reads. An untouched field
// means "leave what is there alone", so emptying a value has to be asked for.
func clearedFields(values []string) map[string]bool {
	out := map[string]bool{}
	for _, name := range values {
		out[name] = true
	}
	return out
}

func (s *Server) render(w http.ResponseWriter, r *http.Request, name string) {
	status := s.app.Status(r.Context())
	data := pageData{Setup: CurrentSetup(s.app.paths), Status: status, LauncherURL: s.URL(), CSRF: s.csrf}
	if status.Docker == "" {
		data.DockerMissing = dockerMissing
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	if err := uiTemplates.ExecuteTemplate(w, name, data); err != nil {
		fmt.Println("the page could not be rendered:", err)
	}
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	status := s.app.Status(r.Context())
	body := struct {
		Status
		DockerMissing string `json:"docker_missing"`
	}{Status: status}
	if status.Docker == "" {
		body.DockerMissing = dockerMissing
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(body)
}

// handleAction runs one of the launcher's actions in the background and answers immediately: the
// page polls the status for what happened, and a start that builds an image takes minutes.
func (s *Server) handleAction(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "post an action", http.StatusMethodNotAllowed)
		return
	}
	if !s.hasToken(r.Header.Get(csrfHeader)) {
		refuse(w)
		return
	}
	action := strings.TrimPrefix(r.URL.Path, "/api/action/")
	// The action outlives the request: the browser's context ends when the answer is written.
	ctx := context.Background()
	switch action {
	case "start":
		go func() { _ = s.app.Start(ctx) }()
	case "stop":
		go func() { _ = s.app.Stop(ctx) }()
	case "update":
		go func() { _ = s.app.Update(ctx) }()
	case "apply":
		go func() { _ = s.app.Apply(ctx) }()
	case "open":
		go func() { _, _ = s.app.Open(ctx) }()
	default:
		http.Error(w, "no such action", http.StatusNotFound)
		return
	}
	w.WriteHeader(http.StatusAccepted)
	_, _ = w.Write([]byte(`{"started":true}`))
}

// mustSub exposes only the stylesheet and the script, not the templates next to them.
func mustSub() fs.FS {
	sub, err := fs.Sub(uiFiles, "ui/assets")
	if err != nil {
		panic(err)
	}
	return sub
}
