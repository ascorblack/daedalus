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
  --data DIR  where the checkouts, the keys and the environment live (default: ./data)
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
// then the stack, then the browser. The launcher keeps serving its page afterwards so the buttons
// work; closing it leaves the containers running.
func startCommand(ctx context.Context, app *App, opts options) error {
	server := NewServer(app, opts.port)
	if err := server.Start(); err != nil {
		// A page that cannot listen is not a reason to refuse to start the stack.
		fmt.Fprintln(os.Stderr, err.Error())
		server = nil
	}
	if server != nil {
		defer server.Stop(context.Background())
	}
	if !app.paths.Configured() || opts.setup {
		if server == nil {
			return errors.New("the setup page needs a free port: pass --port")
		}
		fmt.Println("Set Daedalus up at", server.URL())
		_ = OpenBrowser(ctx, server.URL())
		if err := server.WaitForSetup(ctx); err != nil {
			return err
		}
	}
	if err := app.Start(ctx); err != nil {
		return err
	}
	url := app.OpenURL(ctx)
	fmt.Println("opening", url)
	_ = OpenBrowser(ctx, url)
	if server == nil {
		return nil
	}
	fmt.Printf("The launcher is at %s — leave it running for the buttons, or close it with Ctrl+C: the stack keeps running.\n", server.URL())
	<-ctx.Done()
	return nil
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
