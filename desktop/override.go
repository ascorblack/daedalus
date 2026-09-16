package main

import (
	"fmt"
	"os"
)

// overrideYAML is the compose override the launcher writes next to the data folder. It never
// changes what the server's compose file means — it only names the published image, so a desktop
// install pulls instead of building, and makes the Telegram side optional.
//
// One name is filled in twice: the key proxy is the same image as the agent, started with a
// different command into a container of its own. The compose file already says so; naming it here
// as well is what keeps a desktop install from building an image the published one would have done.
//
// The local Bot API server needs Telegram API credentials to run at all, so it sits behind a
// profile; daedalus depends on it with required:false, which is what lets the stack come up with
// the profile switched off. The profile is enabled from the command line when a bot token is set.
const overrideYAML = `# Written by daedalus-desktop. Edit deploy/compose.yaml in the checkout, not this file:
# the launcher rewrites it on every start.
services:
  daedalus:
    image: %s
    depends_on:
      telegram-bot-api:
        condition: service_started
        required: false
  keyproxy:
    image: %s
  telegram-bot-api:
    profiles: ["telegram"]
`

// WriteOverride writes the override file. It is rewritten on every start because the image name
// belongs to the launcher's version, not to the operator's folder.
func WriteOverride(p Paths) error {
	body := fmt.Sprintf(overrideYAML, agentImage(), agentImage())
	return os.WriteFile(p.Override, []byte(body), 0o644)
}
