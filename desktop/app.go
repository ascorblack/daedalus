package main

import (
	"context"
	"fmt"
	"os"
	"strings"
	"sync"
	"time"
)

// App is the launcher's state: the folder it works in, what it is doing, and the last lines it
// printed. The same methods serve the command line and the buttons on the local page, so both
// paths do exactly the same thing to the stack.
type App struct {
	paths Paths

	mu      sync.Mutex
	lines   []string
	busy    string
	failure string
}

// logLimit is how much of the running commentary the page keeps. It is a progress view, not a log
// file: the real logs come from docker.
const logLimit = 200

func NewApp(p Paths) *App { return &App{paths: p} }

// log records a line and echoes it to the terminal, so the operator sees the same progress whether
// they are watching the window the launcher runs in or the page in the browser.
func (a *App) log(format string, args ...any) {
	line := fmt.Sprintf(format, args...)
	fmt.Println(line)
	a.mu.Lock()
	defer a.mu.Unlock()
	a.lines = append(a.lines, time.Now().Format("15:04:05")+"  "+line)
	if len(a.lines) > logLimit {
		a.lines = a.lines[len(a.lines)-logLimit:]
	}
}

// begin claims the launcher for one action. Two starts at once — one from the terminal, one from an
// impatient click — would race over the same compose project.
func (a *App) begin(action string) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.busy != "" {
		return fmt.Errorf("%s is already running", a.busy)
	}
	a.busy = action
	a.failure = ""
	return nil
}

func (a *App) end(err error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.busy = ""
	if err != nil {
		a.failure = err.Error()
	}
}

// Telegram reports whether the Telegram containers belong in this installation.
func (a *App) Telegram() bool {
	return strings.TrimSpace(readEnv(readFile(a.paths.Env))["TELEGRAM_BOT_TOKEN"]) != ""
}

// Start brings the stack up: the checkouts, the override, the images, the containers, and then the
// wait for the app to answer. It is safe to call on a running stack — compose reconciles.
func (a *App) Start(ctx context.Context) error {
	if err := a.begin("start"); err != nil {
		return err
	}
	err := a.start(ctx)
	a.end(err)
	return err
}

func (a *App) start(ctx context.Context) error {
	if err := CheckDocker(ctx); err != nil {
		return err
	}
	if err := EnsureRepos(ctx, a.paths, a.log); err != nil {
		return err
	}
	if err := WriteOverride(a.paths); err != nil {
		return err
	}
	if err := SyncBotEnv(a.paths); err != nil {
		return err
	}
	telegram := a.Telegram()
	a.log("pulling images")
	if _, err := compose(ctx, a.paths, telegram, "pull"); err != nil {
		// No published image for this platform, or no network. Building takes several minutes the
		// first time, mostly for the headless browser the agent's tools use.
		a.log("no image to pull; building locally, which takes a few minutes the first time")
		if err := composeStream(ctx, a.paths, telegram, "up", "-d", "--build"); err != nil {
			return err
		}
	} else if err := composeStream(ctx, a.paths, telegram, "up", "-d"); err != nil {
		return err
	}
	a.log("waiting for the app to answer")
	if err := WaitReady(ctx, APIPort(a.paths), readyTimeout); err != nil {
		return err
	}
	a.log("the app is up at %s", AppURL(APIPort(a.paths)))
	return nil
}

// Stop leaves the containers in place but stopped. The restart policy is unless-stopped, so they
// stay down until the launcher is asked to start them again.
func (a *App) Stop(ctx context.Context) error {
	if err := a.begin("stop"); err != nil {
		return err
	}
	err := a.stop(ctx)
	a.end(err)
	return err
}

func (a *App) stop(ctx context.Context) error {
	if err := CheckDocker(ctx); err != nil {
		return err
	}
	if err := WriteOverride(a.paths); err != nil {
		return err
	}
	a.log("stopping the stack")
	// The Telegram profile is always on for a stop: a container started under it must be stopped
	// even when the token was removed from the configuration since.
	_, err := compose(ctx, a.paths, true, "stop")
	return err
}

// Update moves both checkouts to what is published, refreshes the images and restarts. The agent's
// own merged pull requests arrive this way.
func (a *App) Update(ctx context.Context) error {
	if err := a.begin("update"); err != nil {
		return err
	}
	err := a.update(ctx)
	a.end(err)
	return err
}

func (a *App) update(ctx context.Context) error {
	if err := CheckDocker(ctx); err != nil {
		return err
	}
	if err := EnsureRepos(ctx, a.paths, a.log); err != nil {
		return err
	}
	if err := UpdateRepos(ctx, a.paths, a.log); err != nil {
		return err
	}
	return a.start(ctx)
}

// Uninstall removes the containers and networks. The volumes — the database, the workspaces, the
// agent's memory — go with them unless the operator asked to keep the data.
func (a *App) Uninstall(ctx context.Context, keepData bool) error {
	if err := a.begin("uninstall"); err != nil {
		return err
	}
	err := a.uninstall(ctx, keepData)
	a.end(err)
	return err
}

func (a *App) uninstall(ctx context.Context, keepData bool) error {
	if err := CheckDocker(ctx); err != nil {
		return err
	}
	if err := WriteOverride(a.paths); err != nil {
		return err
	}
	args := []string{"down", "--remove-orphans"}
	if !keepData {
		args = append(args, "--volumes")
	}
	a.log("removing the containers")
	if _, err := compose(ctx, a.paths, true, args...); err != nil {
		return err
	}
	if keepData {
		a.log("the volumes and %s are kept; run the launcher again to start over from them", a.paths.Data)
		return nil
	}
	a.log("the volumes are gone; %s still holds the checkouts and your keys — delete it by hand when you are done with it", a.paths.Data)
	return nil
}

// Open points the browser at the running app, at the pairing link when the server offers one.
func (a *App) Open(ctx context.Context) (string, error) {
	url := a.OpenURL(ctx)
	return url, OpenBrowser(ctx, url)
}

// OpenURL is the address to open: the link that signs the operator in, or the plain app address
// when the server does not offer one.
func (a *App) OpenURL(ctx context.Context) string {
	if url := PairingURL(ctx, a.paths, a.Telegram()); url != "" {
		return url
	}
	return AppURL(APIPort(a.paths))
}

// Status is what the status command prints and what the page renders.
type Status struct {
	Data       string   `json:"data"`
	Configured bool     `json:"configured"`
	Repos      bool     `json:"repos"`
	Docker     string   `json:"docker"`
	Running    int      `json:"running"`
	AppURL     string   `json:"app_url"`
	Telegram   bool     `json:"telegram"`
	Busy       string   `json:"busy"`
	Failure    string   `json:"failure"`
	Log        []string `json:"log"`
}

func (a *App) Status(ctx context.Context) Status {
	a.mu.Lock()
	status := Status{
		Data:       a.paths.Data,
		Configured: a.paths.Configured(),
		Repos:      exists(a.paths.Bot) && exists(a.paths.Core),
		AppURL:     AppURL(APIPort(a.paths)),
		Telegram:   a.Telegram(),
		Busy:       a.busy,
		Failure:    a.failure,
		Log:        append([]string(nil), a.lines...),
	}
	a.mu.Unlock()
	status.Docker = DockerVersion(ctx)
	if status.Docker != "" && status.Configured {
		status.Running = a.running(ctx)
	}
	return status
}

// running counts the containers of this project that are up. A compose error here means the
// project is not created yet, which reads as zero.
func (a *App) running(ctx context.Context) int {
	if err := WriteOverride(a.paths); err != nil {
		return 0
	}
	out, err := compose(ctx, a.paths, true, "ps", "--quiet", "--status", "running")
	if err != nil {
		return 0
	}
	count := 0
	for _, line := range strings.Split(strings.TrimSpace(out), "\n") {
		if strings.TrimSpace(line) != "" {
			count++
		}
	}
	return count
}

// PrintStatus writes the status as the terminal wants it.
func (a *App) PrintStatus(ctx context.Context) {
	status := a.Status(ctx)
	docker := status.Docker
	if docker == "" {
		docker = "not available"
	}
	setup := "ready"
	if !status.Configured {
		setup = "not set up (run `daedalus-desktop setup`)"
	}
	repos := "cloned"
	if !status.Repos {
		repos = "missing (cloned on the first start)"
	}
	telegram := "off — the browser app only"
	if status.Telegram {
		telegram = "on"
	}
	fmt.Printf("data         %s\n", status.Data)
	fmt.Printf("setup        %s\n", setup)
	fmt.Printf("checkouts    %s\n", repos)
	fmt.Printf("docker       %s\n", docker)
	fmt.Printf("containers   %d running\n", status.Running)
	fmt.Printf("telegram     %s\n", telegram)
	fmt.Printf("app          %s\n", status.AppURL)
	if status.Failure != "" {
		fmt.Fprintf(os.Stderr, "last error   %s\n", status.Failure)
	}
}
