//go:build unix

package main

import (
	"errors"
	"os/exec"
	"strings"
	"testing"
)

// TestToolsMCPNeedsItsSet: `ptyd tools-mcp` without a set, or with an argument it does not know,
// is a usage error a CLI's MCP diagnostics show, not a server serving nothing.
func TestToolsMCPNeedsItsSet(t *testing.T) {
	for _, tc := range []struct {
		args []string
		want string
	}{
		{[]string{"tools-mcp"}, "--set <name> is required"},
		{[]string{"tools-mcp", "--sets", "browser"}, "unknown argument --sets"},
	} {
		out, err := subcommand(nil, tc.args...).CombinedOutput()
		var exit *exec.ExitError
		if !errors.As(err, &exit) || exit.ExitCode() != 2 || !strings.Contains(string(out), tc.want) {
			t.Fatalf("%v: %v %q", tc.args, err, out)
		}
	}
}
