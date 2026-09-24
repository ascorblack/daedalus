package main

import (
	"os"

	"github.com/ascorblack/daedalus/ptyd/internal/hooks"
)

// hookPost is `ptyd hook-post <name> [--wait-ms N]`, run by a CLI inside a launch.
func hookPost(args []string) int {
	return hooks.HookPost(args, os.Getenv, os.Stdin, os.Stdout, os.Stderr)
}
