package main

// Local speech recognition in a native installation.
//
// The engine that runs a downloaded speech model is an optional extra, for the same reason the
// headless browser is: it is about fifteen megabytes of wheels that an installation which never
// recognises speech on this machine should never carry. In a container it rides in the image, where
// fifteen megabytes against the rest of it is nothing. Natively there is no image, so it is fetched
// the first time the operator asks for a model — the app calls this, waits, and then downloads the
// model itself.
//
// The wheels go into the installation's own environment, the same one `syncVenv` builds, so nothing
// here touches the machine's Python.

import (
	"context"
	"fmt"
	"os/exec"
	"strings"
)

// SpeechExtra is the name of the optional dependency group in pyproject.toml.
const SpeechExtra = "speech"

// InstallSpeech adds the speech engine to the installation's environment.
//
// `--inexact` is what keeps this from being destructive: uv would otherwise remove everything the
// lock file does not mention, which in a running installation is the browser extra somebody installed
// earlier. The sync is idempotent, so calling it on an environment that already has the engine costs
// one resolution and no downloads.
func (n *Native) InstallSpeech(ctx context.Context) error {
	if !exists(n.paths.Bot) {
		return fmt.Errorf("the checkout is missing; the speech engine cannot be installed into it")
	}
	n.log("installing the speech engine (about 15 MB)")
	cmd := exec.CommandContext(ctx, uvBinary(n.paths), "sync", "--frozen", "--inexact", "--extra", SpeechExtra)
	cmd.Dir = n.paths.Bot
	cmd.Env = n.runtimeEnv()
	if out, err := runCmd(cmd); err != nil {
		return fmt.Errorf("the speech engine could not be installed: %s", strings.TrimSpace(out))
	}
	n.log("the speech engine is installed; models are downloaded from the app")
	return nil
}
