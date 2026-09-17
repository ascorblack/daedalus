package main

// Choosing a folder is the one thing a page cannot do for itself. A browser's file input hands
// back the contents of a directory, never its path, and a path is the whole of what a project is:
// "add this folder as a project" means the agent works in that directory on this machine.
//
// So the launcher does it, through the dialog the desktop already has, and the page asks for it:
//
//	const path = await window.daedalus.pickFolder();   // "" when the operator cancelled
//
// The function exists only where the launcher shows the app in its own window — a page in an
// ordinary browser tab is served from a different origin and cannot reach the launcher at all. A
// page that wants a folder must therefore check for it and offer a typed path where it is missing:
//
//	if (window.daedalus?.pickFolder) { … } else { /* ask for the path in a text field */ }
//
// The dialogs are the platform's own and are driven as subprocesses rather than through cgo, so
// this file compiles into every build, windowed or not.

import (
	"errors"
	"os/exec"
	"runtime"
	"strings"
)

// folderDialogs is the command to run per platform, and the argument list that makes it print a
// path on standard output and nothing else. Linux has two, because a desktop has one or the other.
func folderDialogs(goos string) [][]string {
	switch goos {
	case "darwin":
		// POSIX path of: without it AppleScript answers in the colon-separated Mac form, which is
		// not a path anything else on the machine understands.
		return [][]string{{"osascript", "-e", `POSIX path of (choose folder with prompt "Choose a folder for Daedalus to work in")`}}
	case "windows":
		return [][]string{{"powershell", "-NoProfile", "-NonInteractive", "-Command",
			`Add-Type -AssemblyName System.Windows.Forms; $d = New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description = 'Choose a folder for Daedalus to work in'; if ($d.ShowDialog() -eq 'OK') { [Console]::Out.Write($d.SelectedPath) }`}}
	default:
		return [][]string{
			{"zenity", "--file-selection", "--directory", "--title=Choose a folder for Daedalus to work in"},
			{"kdialog", "--getexistingdirectory", "."},
		}
	}
}

// PickFolder opens the desktop's folder dialog and returns what was chosen. An empty path with no
// error is a cancellation, which is an answer and not a failure: the page shows nothing and the
// operator carries on.
func PickFolder() (string, error) {
	for _, argv := range folderDialogs(runtime.GOOS) {
		if _, err := exec.LookPath(argv[0]); err != nil {
			continue
		}
		out, err := exec.Command(argv[0], argv[1:]...).Output()
		if err != nil {
			// Every one of these dialogs reports a cancellation as a non-zero exit, and a
			// cancellation is by far the commonest one. It is not worth a message.
			return "", nil
		}
		return strings.TrimSpace(strings.TrimSuffix(string(out), "\n")), nil
	}
	return "", errors.New("this desktop has no folder dialog the launcher can open; type the path instead")
}
