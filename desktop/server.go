package main

import (
	"context"
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

	http *http.Server

	once  sync.Once
	saved chan struct{} // closed when the setup form has been written
}

const defaultPort = 8770

func NewServer(app *App, port int) *Server {
	return &Server{app: app, port: port, saved: make(chan struct{})}
}

// URL is where the operator reaches the launcher page.
func (s *Server) URL() string { return fmt.Sprintf("http://127.0.0.1:%d/", s.port) }

// Start begins serving. The listener is opened before returning, so a caller that opens a browser
// straight afterwards finds the page there.
func (s *Server) Start() error {
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
	s.http = &http.Server{Handler: mux, ReadHeaderTimeout: 10 * time.Second}
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

type pageData struct {
	Setup         Setup
	Status        Status
	DockerMissing string
	LauncherURL   string
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
		setup := Setup{
			DeepseekKey:   r.PostFormValue("deepseek"),
			OpenrouterKey: r.PostFormValue("openrouter"),
			OpencodeKey:   r.PostFormValue("opencode"),
			BotToken:      r.PostFormValue("bot_token"),
			OwnerID:       r.PostFormValue("owner_id"),
			APIID:         r.PostFormValue("api_id"),
			APIHash:       r.PostFormValue("api_hash"),
			USDPerDay:     r.PostFormValue("usd_per_day"),
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

func (s *Server) render(w http.ResponseWriter, r *http.Request, name string) {
	status := s.app.Status(r.Context())
	data := pageData{Setup: CurrentSetup(s.app.paths), Status: status, LauncherURL: s.URL()}
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
