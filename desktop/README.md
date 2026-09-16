# Daedalus on your own machine

`daedalus-desktop` is one small program — `Daedalus.app` on macOS — that turns a folder into a
running Daedalus. It clones the two
repositories, asks the handful of questions the stack needs on a page of its own, writes the
same environment files a server install uses, and runs `docker compose` against the repository's own
`deploy/compose.yaml`. Nothing is installed on the host: **Docker is the only requirement**, and the
launcher never installs it for you.

On macOS and Windows it opens a window: the system's own web view, which shows the launcher's page
while the stack comes up and the app itself once it answers. On Linux it opens a browser window with
nothing around it — no tabs, no address bar — and falls back to the default browser. [The window](#the-window)
says why the three are not the same.

It is about 8 MB without the window and 10–12 MB with it, twice that as the universal macOS build,
and carries no runtime with it. Everything that runs is in containers.

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
| macOS, both kinds | `Daedalus-macOS.zip` | `Daedalus.app` — one universal build for Apple Silicon and Intel, with the window |
| Linux x86-64 | `daedalus-desktop-linux-amd64.tar.gz` | `daedalus-desktop`, already executable; opens a browser window |
| Linux ARM64 | `daedalus-desktop-linux-arm64.tar.gz` | `daedalus-desktop`, already executable; opens a browser window |
| Windows x86-64 | `daedalus-desktop-windows-amd64.zip` | `daedalus-desktop.exe`, with the window |

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
2. The two repositories are fetched into the folder as GitHub tarballs and committed there, with
   `git` running inside the agent's own image: git is not expected on the host either, and no image
   is downloaded that the stack does not already need. Each checkout is a real local history with
   no remote — an update is the next commit on top of it.
3. A page opens at `http://127.0.0.1:8770` — in the launcher's own window where there is one — and
   asks for a model provider key, optionally the Telegram values, and a daily spending cap. Telegram
   is optional — without it you use the app in that window.
4. The image is pulled (or built, if there is no published image for your platform), the stack comes
   up, and the same window moves to the app. There is one image and two containers from it: the
   agent, and the key proxy that holds the provider keys.
5. **The app asks for a model, and that is the last step.** A key is an address; which model runs on
   it — and what it costs — is yours to pick, so the installation ships with none. The app opens on
   *Add a model*: choose the endpoint, choose a model from the list it serves (its context window,
   its modalities and its prices are shown), save. Nothing runs before that, and everything does
   after it. Later ones are added the same way from Settings → Models.

Closing the launcher — the window, or Ctrl+C in the terminal — does not stop anything: the
containers are `restart: unless-stopped` and come back with the machine. The launcher's page is only
a remote control.

Starting the launcher a second time against the same folder does not start a second one. It brings
the first to the front, hands it the link it was opened with if it was opened with one, and exits.

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
    compose.desktop.yaml    the launcher's override: the published image, Telegram made optional
    window.json             where the window was and how big, restored on the next start
    launcher.json           the running launcher's port and token (0600) — how a second launch finds it
    scheme.txt              which executable daedalus:// links are registered to
    browser-profile/        only when the app is shown in a browser window rather than the launcher's own
```

`data/` is next to the `.app` when the launcher runs from a bundle, and next to the working
directory otherwise — a plain executable run from a terminal makes `./data` where you are, as
before. `--data DIR` overrides both. (Finder starts a bundled program with `/` as its working
directory, which is why the bundle does not follow that rule.)

This is the layout a server install has, which is why the repository's compose file runs against it
unchanged. Provider keys are deliberately not in `.env`: that file is mounted into the agent
container, and the key proxy's file is not.

To move an installation, move the whole folder. To use a fork, set `DAEDALUS_GIT_REMOTE` and
`DAEDALUS_CORE_GIT_REMOTE` before the first run: the image is pulled from the fork owner's namespace
as well, so a fork's code never runs upstream's image. A fork that publishes no image has nothing to
pull, and the first start builds it locally instead. Both remotes must be GitHub repositories — the
checkouts are fetched from `codeload.github.com`, not cloned.

Double-clicked from Finder there is no terminal to read, so the launcher's page opens in the browser
first and everything — the progress, a Docker that is not installed or not started, and the buttons
to try again — is on it. The launcher keeps serving that page whether the start succeeded or not;
closing it leaves the containers running.

## The window

What shows the app is decided when the launcher starts, by what the machine can actually do, and
never by a setting. Three steps, and none of them is a hard failure:

1. **The launcher's own window** — the operating system's web view: WKWebView on macOS, WebView2 on
   Windows. Nothing is bundled to provide it and nothing is downloaded; the window is about 2–4 MB
   of binary. The title is *Daedalus*, and the size and position it was left at are remembered in
   `data/window.json` and restored on the next start.
2. **A Chromium-family browser in application mode** — `--app=<url>` in Chrome, Edge, Brave or
   Chromium, which is a window with the page in it and no tabs, address bar or bookmarks. It gets a
   profile of its own in `data/browser-profile`, so it is separate from your browsing and keeps its
   own session. The browsers are looked for on `PATH` and in the places their installers put them,
   because a program started from Finder or Explorer inherits a `PATH` with none of them in it.
3. **The default browser** — a tab, which is what the launcher has always done.

**Windows** needs the WebView2 runtime for step 1. Windows 11 has it, and so does any machine whose
Edge is current; the launcher asks the registry before it tries, and quietly takes step 2 when the
answer is no. Microsoft's Evergreen bootstrapper installs it in a minute if you would rather have
the window.

**Linux does not get step 1 at all**, and that is deliberate. `webview_go` links GTK and WebKitGTK at
load time, so a binary built with it does not start on a machine without those libraries — it does
not fall back, and it does not warn; it fails to start. One binary that runs on every Linux is worth
more than a window of our own, so the published Linux builds begin at step 2. If you want the
window on Linux, you have the source: install `libgtk-3-dev` and `libwebkit2gtk-4.0-dev` and run
`go build` without `-tags nowebview` — and then that binary needs those libraries wherever it runs.

Whatever is showing it, closing it leaves the stack running.

### Links

`daedalus://open/<session-id>` opens that conversation, from anywhere the desktop can follow a link.
The launcher registers the scheme once per executable, with no installer and no administrator:

| | How |
|---|---|
| macOS | `CFBundleURLTypes` in the bundle's `Info.plist`, read by Launch Services when it first sees the app |
| Windows | a key under `HKCU\Software\Classes\daedalus`, written on a first start |
| Linux | `~/.local/share/applications/daedalus-desktop.desktop` with `MimeType=x-scheme-handler/daedalus`, which also gives the launcher its name and icon in the desktop's menu |

A link handed to a launcher that is already running goes to that one; it never starts a second.

### Notifications

The launcher watches the stack and tells the desktop when the inbox gains an entry or an agent has
stopped and is waiting for an answer — `osascript` on macOS, a toast through PowerShell on Windows,
`notify-send` on Linux. Nothing is bundled for it; a machine without `notify-send` says so once in
the launcher's log and is not asked again. It is a poll of the app's own status endpoint every 20
seconds, using the token the app minted for itself, which the launcher reads out of the state
database through the container and keeps in memory only. An installation where that token cannot be
read gets no notifications and says so; nothing else changes.

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
`[self_change] mode` to **`local`**: the agent gets `SelfWorkspace` and edits its own code in the
checkout the stack runs from, with no fork, no remote and no pull request. The app marks the Changes
screen `local`. Give the install a `GITHUB_TOKEN` with Contents and Pull requests on both forks and it
resolves to `server` instead — the full worktree → pull request → approval → rebuild workflow.
Set `mode = "off"` in the configuration and the subsystem is not there at all: no tools, no screen, no
`/api/proposals`, and nothing in the prompt about changing its own code. `daedalus doctor` names the
mode it resolved and what a mode you chose yourself is missing.

## Disk

One image, and the key proxy is a second container from it:

| Image | Size on disk | Why |
|---|---|---|
| `ghcr.io/ascorblack/daedalus` | ~480 MB (~115 MB to pull) | Ubuntu, Python, uv, the environment, the built Mini App. Runs the agent and the key proxy |
| `aiogram/telegram-bot-api` | ~66 MB | the local Bot API server; only with Telegram on |

Budget **about half a gigabyte for the image**, plus the volumes: the database and the agent's
memory are megabytes, but the per-session workspaces grow with what the agent downloads and builds.
`uninstall` without `--keep-data` removes the volumes; images are removed with `docker image prune -a`.

What is not in it, and what it costs to add:

| | Size | How |
|---|---|---|
| the browser skills (Playwright, a headless Chromium, Pillow) | +~550 MB | run the `:browser` tag of the same image; it shares every layer below the last |
| a self-hosted SearXNG | +~382 MB | `--profile search`. Without it `WebSearch` goes to DuckDuckGo directly |
| the rebuilder, for a server that builds its own images | +~237 MB | `--profile selfdev` |

The image is published for `linux/amd64` and `linux/arm64`, so Apple Silicon pulls it like
everything else. If a pull fails anyway the launcher builds locally instead — a few minutes the
first time, and everything after it is the same.

## Building it yourself

`./build.sh` cross-compiles all five binaries into `dist/` inside the Go container, so the only
dependency is Docker here as well:

```bash
cd desktop
./build.sh            # GO_IMAGE=golang:1.23 by default; VERSION= to stamp a version
```

Those are `nowebview` builds — the browser fallback, no window. The window is cgo, and cgo is not
cross-compiled: a windowed macOS build is made on macOS and a windowed Windows build on Windows,
which is what the release workflow's matrix of native runners does. To build the windowed launcher
for the machine you are on, `go build -trimpath -o daedalus-desktop .`.

`./package-macos.sh VERSION AMD64 ARM64 OUTPUT_DIR` turns the two macOS binaries into
`Daedalus.app` inside `Daedalus-macOS.zip` — one universal executable made with `lipo`, the
`Info.plist`, an `AppIcon.icns` built from `docs/brand/avatar-bot.png`, a signature, and the zip
made with `ditto -c -k --keepParent`. It needs macOS: `lipo`, `sips`, `iconutil`, `codesign` and
`notarytool` are all Apple's. Without the signing secrets in the environment it signs ad-hoc and
says so, which is also what happens in CI when the secrets are not set.

The module has one dependency outside the standard library — `github.com/webview/webview_go`, the
web view, pinned to a commit because the project publishes no tags. Tests, for both builds:

```bash
docker run --rm -v "$PWD":/src -w /src -e GOFLAGS=-mod=mod -e GOCACHE=/tmp/gocache -e GOMODCACHE=/tmp/gomod golang:1.23 \
  sh -c 'go vet ./... && go test ./... && go vet -tags nowebview ./... && go test -tags nowebview ./...'
```

On Linux both of those are the same build, since the window is not compiled in there; the cgo build
is exercised by the release workflow on macOS and Windows runners.

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
