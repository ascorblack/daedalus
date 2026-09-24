package main

import (
	"context"
	"os"
	"os/signal"
	"syscall"

	"github.com/ascorblack/daedalus/ptyd/internal/hooks"
	"github.com/ascorblack/daedalus/ptyd/internal/teammcp"
)

// hookPost is `ptyd hook-post <name> [--wait-ms N]`, run by a CLI inside a launch.
func hookPost(args []string) int {
	return hooks.HookPost(args, os.Getenv, os.Stdin, os.Stdout, os.Stderr)
}

// hook is `ptyd hook <source> [--wait-ms N]`, a CLI's command hook: like hook-post, but it always
// exits 0, because a failing hook must never block the CLI.
func hook(args []string) int {
	return hooks.Hook(args, os.Getenv, os.Stdin, os.Stdout, os.Stderr)
}

// teamMCP is `ptyd team-mcp`, the team tools' MCP server on stdio, started by a CLI from its
// per-launch MCP configuration.
func teamMCP() int {
	// The CLI ends its MCP servers with SIGTERM once it closes their stdin; either ends the session.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	if err := teammcp.Serve(ctx, os.Getenv, os.Stdin, os.Stdout, os.Stderr); err != nil {
		os.Stderr.WriteString("ptyd team-mcp: " + err.Error() + "\n")
		return 1
	}
	return 0
}
