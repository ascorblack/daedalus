// Command daedalus-desktop turns a folder into a running Daedalus. It clones the two repositories
// with Docker (git is not expected on the host), asks for the few values the stack needs on a local
// page, writes the same env files a server install uses, and runs docker compose against the
// repository's own compose file. Docker is the only thing it expects to find installed, and it is
// never installed for the operator.
package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/signal"
	"strconv"
	"strings"
)

// version is stamped at build time; a plain `go build` leaves it as it is.
var version = "dev"

const usage = `daedalus-desktop — run Daedalus on this machine with Docker

usage: daedalus-desktop [flags] [command]

commands:
  (none)      set up if needed, start the stack and open the app
  setup       ask the setup questions again and rewrite the configuration
  start       start the stack and open the app
  stop        stop the containers (they stay until started again)
  status      what is configured, what is running
  logs [-f]   the stack's logs
  update      move both checkouts to what is published, refresh the images, restart
  open        open the app in the browser
  pair        print a fresh pairing link for signing in to the app
  uninstall   remove the containers, networks and volumes

flags:
  --data DIR  where the checkouts, the keys and the environment live
              (default: ./data, or data/ beside the app when run from Daedalus.app)
  --port N    the port the launcher's own page listens on (default: 8770)
  --setup     ask the setup questions even though the configuration exists
  --keep-data uninstall: keep the volumes and the data folder
  -f          logs: follow
  --version   print the version and exit
`

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, err.Error())
		os.Exit(1)
	}
}

type options struct {
	command  string
	data     string
	port     int
	setup    bool
	keepData bool
	follow   bool
	help     bool
	version  bool
}

func run(argv []string) error {
	opts, err := parseArgs(argv)
	if err != nil {
		return err
	}
	if opts.help {
		fmt.Print(usage)
		return nil
	}
	if opts.version {
		fmt.Println(version)
		return nil
	}
	paths, err := NewPaths(opts.data)
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	app := NewApp(paths)

	switch opts.command {
	case "", "start":
		return startCommand(ctx, app, opts)
	case "setup":
		opts.setup = true
		return setupCommand(ctx, app, opts)
	case "stop":
		return app.Stop(ctx)
	case "status":
		app.PrintStatus(ctx)
		return nil
	case "logs":
		if err := CheckDocker(ctx); err != nil {
			return err
		}
		if err := WriteOverride(paths); err != nil {
			return err
		}
		args := []string{"logs", "--tail", "200"}
		if opts.follow {
			args = append(args, "--follow")
		}
		return composeStream(ctx, paths, true, args...)
	case "update":
		return app.Update(ctx)
	case "open":
		url, err := app.Open(ctx)
		if err != nil {
			fmt.Println("open this yourself:", url)
			return nil
		}
		fmt.Println("opened", url)
		return nil
	case "pair":
		url, err := app.Pair(ctx)
		if err != nil {
			return err
		}
		fmt.Println(url)
		return nil
	case "uninstall":
		return app.Uninstall(ctx, opts.keepData)
	default:
		return fmt.Errorf("no such command: %s\n\n%s", opts.command, usage)
	}
}

// startCommand is the whole first run: the page for the questions when there are questions to ask,
// then the stack, then the app. What shows it is decided once, by what the machine can do — the
// launcher's own window, a browser in application mode, or a tab — and everything after that is the
// same whichever it turned out to be. Closing it leaves the containers running.
func startCommand(ctx context.Context, app *App, opts options) error {
	if err := app.paths.EnsureDirs(); err != nil {
		return err
	}
	server := NewServer(app, opts.port)
	if err := server.Start(); err != nil {
		// A page that cannot listen is not a reason to refuse to start the stack.
		fmt.Fprintln(os.Stderr, err.Error())
		server = nil
	}
	if server == nil {
		if !app.paths.Configured() || opts.setup {
			return errors.New("the setup page needs a free port: pass --port")
		}
		// No page to show and nothing to keep open: the stack is started, the app is opened, and
		// the terminal is the only place a failure can be read, so it is returned.
		if err := app.Start(ctx); err != nil {
			return err
		}
		url := app.OpenURL(ctx)
		fmt.Println("opening", url)
		_ = OpenBrowser(ctx, url)
		return nil
	}
	defer server.Stop(context.Background())
	// The surface is made after the page is listening, because the first thing it shows is that
	// page: the status, the buttons, and on a first start the questions.
	surface := OpenSurface(app.paths, server.URL())
	server.SetWindowed(surface.Windowed())
	go bringUp(ctx, app, server, surface, opts)
	surface.Run(ctx)
	return nil
}

// bringUp is the work, off the thread the window needs: the questions, the stack, and then the app
// in whatever is showing it. It reports nothing back — every failure it meets is already on the
// launcher's page, which is what the operator is looking at.
func bringUp(ctx context.Context, app *App, server *Server, surface *Surface, opts options) {
	// showing records that the launcher's page is already in front of the operator, so it is not
	// opened a second time in another tab. A window is already showing it.
	showing := surface.Windowed()
	if !app.paths.Configured() || opts.setup {
		fmt.Println("Set Daedalus up at", server.URL())
		if !showing {
			_ = OpenBrowser(ctx, server.URL())
			showing = true
		}
		if err := server.WaitForSetup(ctx); err != nil {
			return
		}
	}
	if !showing && Bundled() {
		// Started from Finder the browser is the only window there is, and a first start pulls
		// several gigabytes before the app itself answers. The launcher's page is where that
		// progress is, so it is opened before the work rather than after it.
		_ = OpenBrowser(ctx, server.URL())
		showing = true
	}
	if err := app.Start(ctx); err != nil {
		// Double-clicked from Finder there is no terminal to read and no shell to try again in, and
		// the commonest failure by far — Docker not installed, or installed and not started — is one
		// the operator fixes in a minute and then wants a button for. App.Start has already recorded
		// what happened for the page to show, so the launcher stays on that page.
		fmt.Fprintln(os.Stderr, err.Error())
		if !showing {
			_ = OpenBrowser(ctx, server.URL())
		}
		fmt.Printf("The launcher is at %s — it says what went wrong, and starts the stack once that is fixed.\n", server.URL())
		return
	}
	url := app.OpenURL(ctx)
	fmt.Println("opening", url)
	surface.Show(ctx, url)
	if !surface.Windowed() {
		fmt.Printf("The launcher is at %s — leave it running for the buttons, or close it with Ctrl+C: the stack keeps running.\n", server.URL())
	}
}

// setupCommand asks the questions and stops there, for an operator who wants to change a key
// without restarting anything.
func setupCommand(ctx context.Context, app *App, opts options) error {
	server := NewServer(app, opts.port)
	if err := server.Start(); err != nil {
		return err
	}
	defer server.Stop(context.Background())
	fmt.Println("Set Daedalus up at", server.URL())
	_ = OpenBrowser(ctx, server.URL()+"setup")
	if err := server.WaitForSetup(ctx); err != nil {
		return err
	}
	fmt.Println("Written. Run `daedalus-desktop` to start the stack with it.")
	return nil
}

// parseArgs takes the command out of the argument list wherever it sits, so `logs -f` and
// `-f logs` both work. The flags are few and long-form, so this stays smaller than flag.FlagSet
// juggling one set per command.
func parseArgs(argv []string) (options, error) {
	opts := options{port: defaultPort}
	for i := 0; i < len(argv); i++ {
		arg := argv[i]
		value := ""
		if name, rest, ok := strings.Cut(arg, "="); ok {
			arg, value = name, rest
		}
		next := func() (string, error) {
			if value != "" {
				return value, nil
			}
			if i+1 >= len(argv) {
				return "", fmt.Errorf("%s needs a value", arg)
			}
			i++
			return argv[i], nil
		}
		switch arg {
		case "-h", "--help", "help":
			opts.help = true
		case "--version":
			opts.version = true
		case "--setup":
			opts.setup = true
		case "--keep-data":
			opts.keepData = true
		case "-f", "--follow":
			opts.follow = true
		case "--data":
			data, err := next()
			if err != nil {
				return opts, err
			}
			opts.data = data
		case "--port":
			port, err := next()
			if err != nil {
				return opts, err
			}
			// Atoi and not Sscanf: Sscanf reads "8770abc" as 8770 and reports no error, and a
			// mistyped port that silently becomes another one is worse than a refusal.
			number, err := strconv.Atoi(strings.TrimSpace(port))
			if err != nil || number < 0 || number > 65535 {
				return opts, fmt.Errorf("--port takes a number, not %q", port)
			}
			opts.port = number
		default:
			if strings.HasPrefix(arg, "-") {
				return opts, fmt.Errorf("no such flag: %s\n\n%s", arg, usage)
			}
			if opts.command != "" {
				return opts, fmt.Errorf("one command at a time: %s and %s", opts.command, arg)
			}
			opts.command = arg
		}
	}
	return opts, nil
}
