package main

import (
	"context"
	"os/exec"
	"runtime"
)

// OpenBrowser hands a URL to whatever the platform uses to open one. A failure is not worth an
// error: the address is printed in the terminal as well, and a headless machine has no browser to
// open it with in the first place.
func OpenBrowser(ctx context.Context, url string) error {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "darwin":
		cmd = exec.CommandContext(ctx, "open", url)
	case "windows":
		cmd = exec.CommandContext(ctx, "rundll32", "url.dll,FileProtocolHandler", url)
	default:
		cmd = exec.CommandContext(ctx, "xdg-open", url)
	}
	return cmd.Start()
}
