package main

import "testing"

// The hook command is the daemon under another name, and on Windows that name has an extension
// and any case.
func TestTheDaemonKnowsTheNameItIsCalledBy(t *testing.T) {
	cases := map[string]string{
		"/state/bin/hook-post":               "hook-post",
		`C:\data\state\bin\hook-post.exe`:    "hook-post",
		`C:\data\state\bin\Hook-Post.EXE`:    "hook-post",
		"ptyd":                               "ptyd",
		`C:\Program Files\Daedalus\ptyd.exe`: "ptyd",
		"hook-post.exe.bak":                  "hook-post.exe.bak",
	}
	for arg0, want := range cases {
		if got := calledAs(arg0); got != want {
			t.Errorf("%s: %q, want %q", arg0, got, want)
		}
	}
}
