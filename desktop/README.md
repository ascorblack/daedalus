# Daedalus on your own machine

`daedalus-desktop` is a single binary that turns a folder into a running Daedalus. It clones the two
repositories, asks the handful of questions the stack needs on a page in your browser, writes the
same environment files a server install uses, and runs `docker compose` against the repository's own
`deploy/compose.yaml`. Nothing is installed on the host: **Docker is the only requirement**, and the
launcher never installs it for you.

The launcher is about 8 MB and carries no runtime with it. Everything that runs is in containers.

## Get it

Download the binary for your platform from the
[releases page](https://github.com/ascorblack/daedalus/releases) (tags beginning with `desktop-v`),
put it in an empty folder, and run it.

```bash
mkdir daedalus && cd daedalus
curl -fL -o daedalus-desktop https://github.com/ascorblack/daedalus/releases/latest/download/daedalus-desktop-linux-amd64
chmod +x daedalus-desktop
./daedalus-desktop
```

On **macOS**, the binary is not signed or notarized: Gatekeeper refuses it on the first launch.
Either right-click it in Finder and choose *Open*, or clear the quarantine flag once:

```bash
xattr -d com.apple.quarantine daedalus-desktop
```

On **Windows**, run `daedalus-desktop-windows-amd64.exe` from a terminal in the folder you want the
installation to live in; SmartScreen shows the same kind of warning for an unsigned binary.

## What happens on the first run

1. Docker is checked. Without it the launcher says what to install and stops — Docker Desktop on
   macOS and Windows, Docker Engine with the compose plugin on Linux.
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

Everything lives next to the binary, in `data/` (or wherever `--data` points):

```
data/
  daedalus/               the bot checkout; deploy/compose.yaml runs from here
  protocore-exp/          the core checkout
  daedalus-secrets/
    keyproxy.env          provider keys (0600) — outside every folder the agent can read
    ssh/                  keys and config for hosts the agent may reach; may stay empty
  .env                    what compose interpolates and the agent container reads
  compose.desktop.yaml    the launcher's override: the published images, Telegram made optional
```

This is the layout a server install has, which is why the repository's compose file runs against it
unchanged. Provider keys are deliberately not in `.env`: that file is mounted into the agent
container, and the key proxy's file is not.

To move an installation, move the folder and the binary together, or pass `--data` to the folder's
new place. To use a fork, set `DAEDALUS_GIT_REMOTE` and `DAEDALUS_CORE_GIT_REMOTE` before the first
run: the images are pulled from the fork owner's namespace as well, so a fork's code never runs
upstream's image. A fork that publishes no images has nothing to pull, and the first start builds
them locally instead.

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

The module has no dependencies outside the standard library. Tests:

```bash
docker run --rm -v "$PWD":/src -w /src -e GOFLAGS=-mod=mod -e GOCACHE=/tmp/gocache -e GOMODCACHE=/tmp/gomod golang:1.23 go test ./...
```
