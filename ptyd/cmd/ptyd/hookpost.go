package main

import (
	"context"
	"os"
	"os/signal"
	"strings"
	"syscall"

	"github.com/ascorblack/daedalus/ptyd/internal/hooks"
	"github.com/ascorblack/daedalus/ptyd/internal/toolsmcp"
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
// per-launch MCP configuration: `tools-mcp --set team` under the name every launch already uses.
func teamMCP() int {
	return serveTools(toolsmcp.TeamSet, "ptyd team-mcp")
}

// toolsMCP is `ptyd tools-mcp --set <name>`: one set of Daedalus's tools as an MCP server on stdio,
// the team's or one the host describes in the launch's `tools/<name>.json`.
func toolsMCP(args []string) int {
	set := ""
	for i := 0; i < len(args); i++ {
		switch {
		case args[i] == "--set" && i+1 < len(args):
			set = args[i+1]
			i++
		case strings.HasPrefix(args[i], "--set="):
			set = strings.TrimPrefix(args[i], "--set=")
		default:
			os.Stderr.WriteString("ptyd tools-mcp: unknown argument " + args[i] + "\n")
			return 2
		}
	}
	if set == "" {
		os.Stderr.WriteString("ptyd tools-mcp: --set <name> is required\n")
		return 2
	}
	return serveTools(set, "ptyd tools-mcp")
}

func serveTools(set, name string) int {
	// The CLI ends its MCP servers with SIGTERM once it closes their stdin; either ends the session.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	if err := toolsmcp.ServeSet(ctx, set, os.Getenv, os.Stdin, os.Stdout, os.Stderr); err != nil {
		os.Stderr.WriteString(name + ": " + err.Error() + "\n")
		return 1
	}
	return 0
}
