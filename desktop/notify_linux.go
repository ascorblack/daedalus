package main

import (
	"errors"
	"os/exec"
)

// Linux through notify-send, which is what the desktop notification daemons are spoken to with and
// what libnotify installs. It is not on every machine — a bare window manager has neither — and the
// launcher says so once and stops rather than failing repeatedly.
func notify(n Notification) error {
	if _, err := exec.LookPath("notify-send"); err != nil {
		return errors.New("notify-send is not installed, so the desktop has no way to be told")
	}
	args := []string{"--app-name=Daedalus", "--icon=daedalus-desktop", n.Title}
	if n.Body != "" {
		args = append(args, n.Body)
	}
	if out, err := exec.Command("notify-send", args...).CombinedOutput(); err != nil {
		return combinedError("notify-send", out, err)
	}
	return nil
}
