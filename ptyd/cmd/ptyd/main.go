// Command ptyd is the terminal daemon: it owns pseudo-terminals, keeps their output and screens, and
// serves them to the Daedalus host over an authenticated local socket.
package main

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/internal/emulator/basic"
	"github.com/ascorblack/daedalus/ptyd/internal/events"
	"github.com/ascorblack/daedalus/ptyd/internal/logx"
	"github.com/ascorblack/daedalus/ptyd/internal/rpc"
	"github.com/ascorblack/daedalus/ptyd/internal/server"
	"github.com/ascorblack/daedalus/ptyd/internal/term"
	"github.com/ascorblack/daedalus/ptyd/internal/version"
)

const usage = `usage:
  ptyd serve --env <name> --run-dir <dir> --state-dir <dir> [flags]
  ptyd version
  ptyd hook-post <name> [--wait-ms N] < body   (inside a launch: post a hook, print the reply)

serve flags:
  --listen unix|tcp:127.0.0.1:<port>   where to listen (default unix: <run-dir>/ptyd.sock)
  --home <dir>                         home directory of spawned shells (default $HOME)
  --shell <path>                       login shell (default $SHELL, then the passwd entry)
  --hooks-listen <addr>                loopback address of the hook listener
  --config <file>                      JSON file with limits
  --log-file <file>, --log-level <lv>  the daemon's own log (default stderr, info)
`

func main() {
	// Called through the hook command's link, the daemon is that command.
	if filepath.Base(os.Args[0]) == "hook-post" {
		os.Exit(hookPost(os.Args[1:]))
	}
	if len(os.Args) < 2 {
		fmt.Fprint(os.Stderr, usage)
		os.Exit(2)
	}
	switch os.Args[1] {
	case "version", "--version":
		fmt.Printf("ptyd %s (protocol %d)\n", version.Version, version.Protocol)
	case "serve":
		if err := serve(os.Args[2:]); err != nil {
			fmt.Fprintln(os.Stderr, "ptyd:", err)
			os.Exit(1)
		}
	case "hook-post":
		os.Exit(hookPost(os.Args[2:]))
	case "help", "-h", "--help":
		fmt.Print(usage)
	default:
		fmt.Fprintf(os.Stderr, "ptyd: unknown command %q\n\n%s", os.Args[1], usage)
		os.Exit(2)
	}
}

func serve(args []string) error {
	cfg, err := config.Parse(args)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(cfg.StateDir, 0o700); err != nil {
		return err
	}
	log, logCloser, err := logx.New(cfg.LogFile, cfg.LogLevel)
	if err != nil {
		return err
	}
	defer logCloser.Close()
	journalFile, err := logx.OpenRotating(filepath.Join(cfg.StateDir, "agent-writes.jsonl"),
		config.AgentJournalBytes, config.AgentJournalFiles)
	if err != nil {
		return err
	}
	defer journalFile.Close()

	environ := os.Environ()

	// A terminal's program exiting must never take the daemon with it. SIGHUP reaches a daemon that
	// was started from a terminal when that terminal closes; SIGPIPE is handled per write.
	signal.Ignore(syscall.SIGHUP, syscall.SIGPIPE)

	ep, err := server.Prepare(cfg.RunDir, cfg.Listen)
	if err != nil {
		if errors.Is(err, server.ErrHeld) {
			return err
		}
		return fmt.Errorf("listening in %s: %w", cfg.RunDir, err)
	}
	defer ep.Release()

	instance := make([]byte, 8)
	_, _ = rand.Read(instance)
	evlog := events.NewLog(config.EventRingSize)
	deb := events.NewDebouncer(evlog, events.Policies)
	registry := term.NewRegistry(term.Deps{
		Emulator:  basic.Factory,
		Events:    deb,
		Journal:   logx.NewJournal(journalFile),
		Clock:     term.RealClock{},
		Log:       log,
		KillGrace: cfg.Limits.KillGrace,
	}, cfg.Limits.MaxTerminals)
	daemon := &rpc.Daemon{
		Config: cfg, Instance: hex.EncodeToString(instance), StartedAt: time.Now().UTC(), Registry: registry,
		Events: evlog, Log: log, EmulatorName: basic.Name, Environ: environ,
	}
	side, closeSide, err := startSide(cfg, evlog, environ, log)
	if err != nil {
		return fmt.Errorf("side channels: %w", err)
	}
	defer closeSide()
	daemon.Side = side
	srv := server.New(ep.Token, log, daemon.Hello)
	daemon.Register(srv)

	stop := make(chan struct{})
	go registry.RunReaper(stop)
	go daemon.RunStats(stop)

	sigs := make(chan os.Signal, 1)
	signal.Notify(sigs, syscall.SIGTERM, syscall.SIGINT)
	served := make(chan error, 1)
	go func() { served <- srv.Serve(ep.Listener) }()
	log.Info("ptyd serving", "env", cfg.Env, "version", version.Version, "protocol", version.Protocol,
		"instance", daemon.Instance, "run_dir", cfg.RunDir, "emulator", basic.Name)

	var serveErr error
	select {
	case s := <-sigs:
		log.Info("ptyd stopping", "signal", s.String())
	case serveErr = <-served:
		log.Error("ptyd listener failed", "error", fmt.Sprint(serveErr))
	}
	// Every terminal gets its hangup and grace in parallel, so stopping takes one grace, not one per
	// terminal. The endpoint is removed first, so the host stops connecting to a daemon on its way out.
	close(stop)
	ep.Release()
	_ = ep.Listener.Close()
	registry.Shutdown(cfg.Limits.KillGrace)
	srv.Close()
	log.Info("ptyd stopped")
	return serveErr
}
