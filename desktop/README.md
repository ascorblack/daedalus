# Daedalus on your own machine

`daedalus-desktop` is one small program — `Daedalus.app` on macOS — that turns a folder into a
running Daedalus. It clones the two
repositories, asks the handful of questions the stack needs on a page in your browser, writes the
same environment files a server install uses, and runs `docker compose` against the repository's own
`deploy/compose.yaml`. Nothing is installed on the host: **Docker is the only requirement**, and the
launcher never installs it for you.

It is about 8 MB, or twice that as the universal macOS build, and carries no runtime with it.
Everything that runs is in containers.

## Get it

One line on macOS and Linux — it takes the newest `desktop-v*` release, checks the download against
the release's `SHA256SUMS`, and unpacks it into `./Daedalus`:

```sh
curl -fsSL https://raw.githubusercontent.com/ascorblack/daedalus/main/desktop/install.sh | sh
```

`DAEDALUS_DIR=/somewhere/else` puts it elsewhere. Or take the archive by hand from the
[releases page](https://github.com/ascorblack/daedalus/releases) (the tags beginning with
`desktop-v`):

| Machine | File | What is in it |
|---|---|---|
| macOS, both kinds | `Daedalus-macOS.zip` | `Daedalus.app` — one universal build for Apple Silicon and Intel |
| Linux x86-64 | `daedalus-desktop-linux-amd64.tar.gz` | `daedalus-desktop`, already executable |
| Linux ARM64 | `daedalus-desktop-linux-arm64.tar.gz` | `daedalus-desktop`, already executable |
| Windows x86-64 | `daedalus-desktop-windows-amd64.zip` | `daedalus-desktop.exe` |

Unpack it into a folder of its own — the installation is made **inside that folder**, so deleting
the folder deletes the installation.

On **macOS**, double-click `Daedalus`. When the release was built with the signing secrets in place
the app is signed and notarized and simply opens. When it was not, it is signed ad-hoc, and macOS
asks once about a copy that arrived through a browser: right-click the app, choose *Open*, then
*Open* again. A copy fetched by the one-liner above never asks at all — the quarantine attribute
that makes Gatekeeper ask is set by the browser, and `curl` does not set it. Unzip with Finder or
`ditto -x -k`, not with `unzip`: an app bundle carries symlinks and the signature's own extended
attributes, and `unzip` drops both, which leaves an app macOS calls damaged.

On **Windows**, unpack with `Expand-Archive daedalus-desktop-windows-amd64.zip -DestinationPath
Daedalus` in PowerShell and run `daedalus-desktop.exe` from the folder you want the installation to
live in. The executable is not signed, so SmartScreen warns once: *More info → Run anyway*.

## What happens on the first run

1. Docker is checked — including the places the installers put it, since a program started from
   Finder inherits a PATH that has none of them in it. Without Docker the launcher says what to
   install, on its own page as well as in the terminal, and waits there: Docker Desktop on macOS and
   Windows, Docker Engine with the compose plugin on Linux.
2. The two repositories are cloned into the folder, with `git` running in a container: git is not
   expected on the host either.
3. A page opens at `http://127.0.0.1:8770` and asks for a model provider key, optionally the
   Telegram values, and a daily spending cap. Telegram is optional — without it you use the app in
   the browser.
4. The images are pulled (or built, if there is no published image for your platform), the stack
   comes up, and the browser opens the app.

Closing the launcher does not stop anything: the containers are `restart: unless-stopped` and come
back with the machine. The launcher's page is only a remote control.

## The folder

Everything the installation owns is in one folder — the one you unpacked into. On macOS the data
sits **beside** the app, not inside it, because the bundle is replaced by the next download:

```
Daedalus/
  Daedalus.app            or daedalus-desktop / daedalus-desktop.exe elsewhere
  data/
    daedalus/               the bot checkout; deploy/compose.yaml runs from here
    protocore-exp/          the core checkout
    daedalus-secrets/
      keyproxy.env          provider keys (0600) — outside every folder the agent can read
      ssh/                  keys and config for hosts the agent may reach; may stay empty
    .env                    what compose interpolates and the agent container reads
    compose.desktop.yaml    the launcher's override: the published images, Telegram made optional
```

`data/` is next to the `.app` when the launcher runs from a bundle, and next to the working
directory otherwise — a plain executable run from a terminal makes `./data` where you are, as
before. `--data DIR` overrides both. (Finder starts a bundled program with `/` as its working
directory, which is why the bundle does not follow that rule.)

This is the layout a server install has, which is why the repository's compose file runs against it
unchanged. Provider keys are deliberately not in `.env`: that file is mounted into the agent
container, and the key proxy's file is not.

To move an installation, move the whole folder. To use a fork, set `DAEDALUS_GIT_REMOTE` and
`DAEDALUS_CORE_GIT_REMOTE` before the first run: the images are pulled from the fork owner's
namespace as well, so a fork's code never runs upstream's image. A fork that publishes no images has
nothing to pull, and the first start builds them locally instead.

Double-clicked from Finder there is no terminal to read, so the launcher's page opens in the browser
first and everything — the progress, a Docker that is not installed or not started, and the buttons
to try again — is on it. The launcher keeps serving that page whether the start succeeded or not;
closing it leaves the containers running.

## Commands

| Command | What it does |
|---|---|
| `daedalus-desktop` | set up if needed, start the stack, open the app |
| `daedalus-desktop setup` | ask the questions again and rewrite the configuration |
| `daedalus-desktop status` | what is configured, what is running |
| `daedalus-desktop stop` | stop the containers; they stay down until started again |
| `daedalus-desktop logs -f` | the stack's logs |
| `daedalus-desktop update` | move both checkouts to what is published, refresh the images, restart |
| `daedalus-desktop open` | open the app in the browser |
| `daedalus-desktop pair` | print a fresh pairing link for signing in to the app |
| `daedalus-desktop uninstall [--keep-data]` | remove the containers, networks and volumes |

Flags: `--data DIR` (default `./data`), `--port N` for the launcher's own page (default 8770),
`--setup` to ask the questions again on a start, `--version`.

On the setup page, **a field left empty keeps whatever is already in force** — re-running setup to
change the daily cap does not blank the provider keys, and the public address set by hand in
`data/.env` is never touched. A value is removed only by ticking *remove* beside it.

## Signing in

The first start opens the app through a one-time link the stack writes when it comes up, and the
launcher offers that link once, while it belongs to that start. Afterwards `open` goes to the app
itself, whose login screen takes a passkey, Telegram, or a pairing link. `daedalus-desktop pair`
prints a fresh link whenever a browser needs one — a new machine, a cleared cookie jar, or a session
that has expired. Each link opens once and expires after thirty minutes.

Settings that are not on the setup page — a GitHub token for self-development, a public address for
the app, extra providers and the search APIs — are edited in `data/.env` and
`data/daedalus-secrets/keyproxy.env` afterwards, exactly as on a server. `deploy/env.example` and
`deploy/keyproxy.env.example` in the checkout describe every key.

## Self-development on a desktop install

A desktop install has the two checkouts and usually no GitHub token, so the capability probe resolves
`[self_change] mode` to **`local`**: the agent gets `SelfWorkspace` and `SelfApply` and improves its own
code in the checkout the stack runs from, with no fork, no remote and no pull request. The app marks the
Changes screen `local`.

**How a change reaches you.** The agent works in a worktree, runs the tests through `Verify`, commits, and
calls `SelfApply`. Its commits are fast-forwarded onto the checkout's own branch — a plain local git
history you can read with `git -C data/daedalus log` — and then three places say the same thing:

- the app shows a strip above the screen, *"Changes are ready — restart to apply"*, with the summary and a
  **Restart** button;
- the launcher's status page shows a card with the same line and a **Restart to apply** button;
- `daedalus-desktop status` prints a `changes` line.

Any of the three applies it, and so does the plain thing: **close the app and open it again.** The
checkout is what runs — the image only supplies the environment — so the containers coming back up is the
whole of applying a change.

**What protects you.** The restart is not a leap. The supervisor checks out the agent's commit into a
detached worktree of its own and runs there: `uv sync` (only when `uv.lock` or `pyproject.toml` changed —
the virtualenv lives on the `daedalus-venv` volume and survives), `compileall`, `daedalus check` and the
smoke tests. Only if all of that passes is the running bot stopped. A change that fails is taken back out
of the checkout and the app says so with the reason. A change that passes but cannot stay up — three
starts dying within ten minutes — puts the last known-good commit back on its own and tells you; the
commit is not lost, it is still on the branch the agent made it on. A change to the `Dockerfile` or the
system packages is applied as far as a restart can take it, and says the rest needs
`daedalus-desktop update`.

Give the install a `GITHUB_TOKEN` with Contents and Pull requests on both forks and it
resolves to `server` instead — the full worktree → pull request → approval → rebuild workflow.
Set `mode = "off"` in the configuration and the subsystem is not there at all: no tools, no screen, no
`/api/proposals`, and nothing in the prompt about changing its own code. `daedalus doctor` names the
mode it resolved and what a mode you chose yourself is missing.

## Disk

The images are not small, and the agent's own image is the reason:

| Image | Size on disk | Why |
|---|---|---|
| `ghcr.io/ascorblack/daedalus` | ~4 GB | Ubuntu, Python, Node, the GitHub CLI and a headless Chromium for the browser tools |
| `searxng/searxng` | ~500 MB | the self-hosted search behind the WebSearch tool |
| `aiogram/telegram-bot-api` | ~250 MB | the local Bot API server; only with Telegram on |
| `ghcr.io/ascorblack/daedalus-keyproxy` | ~200 MB | the container that holds the provider keys |
| `docker:cli` | ~100 MB | the rebuilder |

Budget **about 6 GB for the images**, plus the volumes: the database and the agent's memory are
megabytes, but the per-session workspaces grow with what the agent downloads and builds. `uninstall`
without `--keep-data` removes the volumes; the images are removed with `docker image prune -a`.

On Apple Silicon there may be no published image for `linux/arm64` yet. The launcher notices that
the pull failed and builds the image locally instead — the first start then takes several minutes
and needs the build dependencies to download, but everything after it is the same.

## Building it yourself

`./build.sh` cross-compiles all five binaries into `dist/` inside the Go container, so the only
dependency is Docker here as well:

```bash
cd desktop
./build.sh            # GO_IMAGE=golang:1.23 by default; VERSION= to stamp a version
```

`./package-macos.sh VERSION AMD64 ARM64 OUTPUT_DIR` turns the two macOS binaries into
`Daedalus.app` inside `Daedalus-macOS.zip` — one universal executable made with `lipo`, the
`Info.plist`, an `AppIcon.icns` built from `docs/brand/avatar-bot.png`, a signature, and the zip
made with `ditto -c -k --keepParent`. It needs macOS: `lipo`, `sips`, `iconutil`, `codesign` and
`notarytool` are all Apple's. Without the signing secrets in the environment it signs ad-hoc and
says so, which is also what happens in CI when the secrets are not set.

The module has no dependencies outside the standard library. Tests:

```bash
docker run --rm -v "$PWD":/src -w /src -e GOFLAGS=-mod=mod -e GOCACHE=/tmp/gocache -e GOMODCACHE=/tmp/gomod golang:1.23 go test ./...
```

## Signing releases

A release signed with a Developer ID certificate and notarized by Apple opens with a double-click
and no questions; without the secrets below the same release is signed ad-hoc, which is fine for
anyone installing with the one-liner and one right-click → *Open* for anyone who downloaded it in a
browser. **A missing secret never fails the release** — the workflow signs ad-hoc, says so in the
job log, and says so in the release notes.

Five repository secrets, all five needed before signing is attempted:

| Secret | What it is | Where it comes from |
|---|---|---|
| `APPLE_CERTIFICATE_P12` | base64 of a **Developer ID Application** certificate exported as `.p12` | Apple Developer account → *Certificates, Identifiers & Profiles* → *Certificates* → **+** → *Developer ID Application*. Upload a CSR made by Keychain Access (*Certificate Assistant → Request a Certificate From a Certificate Authority*), download the `.cer`, open it (it lands in the login keychain), then right-click the **private key** under *My Certificates* → *Export* to get the `.p12`. Requires the Apple Developer Program. |
| `APPLE_CERTIFICATE_PASSWORD` | the password that export asked for | you choose it during the export |
| `APPLE_ID` | the Apple account the app is notarized under | the account that owns the certificate |
| `APPLE_TEAM_ID` | ten characters, e.g. `A1B2C3D4E5` | [developer.apple.com/account](https://developer.apple.com/account) → *Membership details* → *Team ID* |
| `APPLE_APP_PASSWORD` | an **app-specific** password, not the account password | [appleid.apple.com](https://appleid.apple.com) → *Sign-In and Security* → *App-Specific Passwords* → **+** |

The certificate is put into the secret base64-encoded, because a repository secret holds text:

```bash
base64 -i DeveloperID.p12 | tr -d '\n' | pbcopy    # macOS
base64 -w0 DeveloperID.p12                         # Linux
```

What the workflow then does on the macOS runner: imports the certificate into a temporary keychain
it deletes afterwards, signs the executable and the bundle with `--options runtime --timestamp` and
the (deliberately empty) entitlements in `macos/entitlements.plist` — the hardened runtime is what
notarization requires — submits the zip with `xcrun notarytool submit --wait`, staples the ticket to
the bundle so the first launch needs no network, and zips it again.
