# Setting up Daedalus on a server — instructions for a coding agent

This page is written for an agent (Claude Code, Codex, Cursor, OpenCode, a Daedalus of your own …) that
was asked to install Daedalus on a Linux server for one person, the operator. Every step is a command or
a file; every check names what it must print. Ask the operator only for what the steps say you need.

## 0. What you are installing

Daedalus is a personal, self-developing agent: a Docker Compose stack of five containers (the agent, a
key proxy that holds provider keys, SearXNG for web search, a rebuilder, and optionally a local Telegram
Bot API server). The operator talks to it in a web app (a PWA, installable on a phone) and, optionally,
in Telegram. Read `README.md` once; it is short.

Requirements on the server: Docker Engine with the compose plugin (`docker compose version`), 4 GB of
RAM free, 10 GB of disk (the agent image carries Chromium), outbound HTTPS. A public HTTPS address is
optional: without it the operator opens the app over an SSH tunnel or a VPN; with it the app is a
proper PWA and Telegram's Mini App works.

## 1. What to ask the operator before you start

Ask these once, together; do not start until you have the answers.

1. **A model.** One of: an API key for DeepSeek, OpenRouter or OpenCode Go; or a self-hosted
   OpenAI-compatible endpoint (base URL + key); or a ChatGPT / Claude Code / SuperGrok login already
   present on this server (`~/.codex`, `~/.claude`, `~/.grok`) — those are used as providers through
   the key proxy.
2. **Telegram, or not.** If they want Telegram: a bot token from @BotFather, their numeric Telegram
   user id, and (for files over 20 MB) API id + hash from https://my.telegram.org/apps. If not, skip
   every Telegram value: the app in the browser is the whole interface.
3. **A public address, or not.** A domain that already points at this server (for HTTPS) or "no".
4. **The daily spend cap** in USD (default 20).
5. **Self-development.** A fine-grained GitHub token limited to the two repositories they fork
   (`daedalus`, `protocore-exp`), with Contents and Pull requests read/write — or "later", or "never".
   This answer decides `[self_change] mode` (see §7): with the token, the checkouts and the
   `rebuilder` service the installation resolves to `server`; without the token, to `local` — the
   agent edits its own checkout and the change applies on a restart; "never" is `mode = "off"`, and
   the tools, the API, the app screen and the prompt section all go with it.

## 2. Install

```bash
# as the operator's user, in their home directory
git clone https://github.com/ascorblack/daedalus
git clone https://github.com/ascorblack/protocore-exp     # the core, next to the bot: ../protocore-exp
mkdir -p daedalus-secrets/ssh && chmod 700 daedalus-secrets
cd daedalus
cp deploy/env.example .env
cp deploy/keyproxy.env.example ../daedalus-secrets/keyproxy.env && chmod 600 ../daedalus-secrets/keyproxy.env
```

Fill `.env` (secrets do NOT go here; it is mounted into the agent container):

| key | value |
|---|---|
| `TELEGRAM_BOT_TOKEN`, `OWNER_USER_ID`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | the Telegram answers, or leave empty |
| `MINIAPP_PUBLIC_URL` | `https://<domain>` when there is one, else empty |
| `USD_PER_DAY` | the cap |
| `SEARXNG_SECRET` | any random string (`openssl rand -hex 16`) |
| `DAEDALUS_COMPOSE_PROJECT_DIR`, `DAEDALUS_COMPOSE_FILE`, `DAEDALUS_CORE_PROJECT_DIR` | absolute paths: the `daedalus` checkout, its `deploy/compose.yaml`, the `protocore-exp` checkout |
| `SERVICES_PUBLIC_HOST` | the server's LAN or public address (the operator opens agent-hosted services at `<host>:<port>`) |
| `GITHUB_TOKEN` | the self-development token, or empty (empty resolves self-development to `local`) |

Fill `../daedalus-secrets/keyproxy.env` with the provider keys (`DEEPSEEK_API_KEY=…`, `OPENROUTER_API_KEY=…`,
`OPENCODE_API_KEY=…`; a self-hosted endpoint is `VLLM_BASE_URL`/`VLLM_API_KEY` in `.env`).

Start:

```bash
# with Telegram
docker compose -f deploy/compose.yaml --env-file .env --profile telegram up -d --build
# without Telegram
docker compose -f deploy/compose.yaml --env-file .env up -d --build
```

The first build takes several minutes (Chromium, Node, the Python environment). Check:

```bash
docker compose -f deploy/compose.yaml --env-file .env ps        # daedalus, keyproxy, searxng, rebuilder (+ telegram-bot-api) "running"
docker logs deploy-daedalus-1 2>&1 | grep -E "bot started|pairing link|polling"     # "pairing link written to …" without Telegram
```

`bot started pid=… bot=<sha> core=<sha>` must appear. Without Telegram there is no "polling" line; that is
expected.

## 3. Log the operator in

The app is at `http://127.0.0.1:8765/app` on the server. When there is no other way in (no Telegram, no
passkey yet) the server writes a one-time **pairing link** to the state volume at start (the log says
`pairing link written to …`; the link itself is never logged):

```bash
docker exec deploy-daedalus-1 cat /srv/state/pairing-url
# a new one at any time:
docker exec deploy-daedalus-1 uv run --frozen python -m daedalus auth pair
```

Give that link to the operator (it works once, for 30 minutes). Opening it signs the browser in. Then tell
them to add a **passkey** in the app (Settings → Security → Add a passkey) so the next login is Face ID /
Touch ID / a security key and needs no link. With Telegram configured, the login page also offers
"Log in with Telegram".

If there is no public address, the operator reaches the app through a tunnel:
`ssh -L 8765:127.0.0.1:8765 <server>` and then `http://127.0.0.1:8765/app` on their machine; the pairing
link works through the tunnel as printed (it uses `http://127.0.0.1:8765`).

## 4. HTTPS (when there is a domain)

Put any reverse proxy with TLS in front of port 8765 and set `MINIAPP_PUBLIC_URL=https://<domain>` (that is
what marks the session cookie secure). The proxy must keep long connections open (the app streams events).
Caddy needs one line:

```
<domain> {
    reverse_proxy 127.0.0.1:8765
}
```

nginx: `proxy_pass http://127.0.0.1:8765; proxy_http_version 1.1; proxy_buffering off; proxy_read_timeout 3600s;`.
Restart the stack after changing `.env`
(`docker compose … up -d`). With Telegram, register `https://<domain>/app` as the bot's menu button in
@BotFather.

## 5. Telegram (when wanted)

Two shapes; both keep every feature:

- **Private chat only** (default, no group): the operator writes to the bot; `/new <title>` creates
  sessions, `/sessions` lists them, `/use <n>` switches; the app shows all of them.
- **A group with topics**: create a private supergroup with topics, add the bot as administrator with
  *manage topics*, send `/bind` there; every session is then a topic.

## 6. Verify the whole thing works

1. In the app, create an agent with the task "list the files in your workspace and tell me the date";
   it must answer within a minute and the run must show tool steps.
2. Settings → Models shows the configured presets; Usage shows the call you just made.
3. `docker logs deploy-daedalus-1 | grep -i error` shows nothing about providers.

## 7. Updating, and whether the agent may update itself

`[self_change] mode` in `config.toml` decides what the agent may do to its own code. Left at `auto`
— the default — it is worked out at startup from what the installation has:

| resolved | needs | what the agent gets |
|---|---|---|
| `server` | `GITHUB_TOKEN`, an `origin` on both checkouts the token may push to, and the `rebuilder` service (or the supervisor socket) | `SelfWorkspace`, `SelfPropose`, `SelfRebuild`, `SelfRollback`, the proposals API and the Changes screen |
| `local` | writable git checkouts of `daedalus` and `protocore-exp` | `SelfWorkspace` and `SelfApply`: it edits a worktree and commits into the checkout, and the change applies on a restart |
| `off` | — | nothing: no tools, no `/api/proposals`, no Changes screen, no self-development text in the prompt |

Set the mode explicitly to overrule the resolution — `mode = "off"` on an installation the operator
does not want changing itself. `daedalus doctor` (and `GET /api/capabilities`) names the mode, the
reasons behind it, and anything a mode you chose yourself is missing. A change takes effect on the
next restart.

In `local` mode — the desktop default — there is no pull request to approve: the agent commits into the
checkout the installation runs from, and the app shows *"Changes are ready — restart to apply"* with a
Restart button (`POST /api/self/restart`). The supervisor preflights that commit on a detached worktree of
itself before it stops anything, keeps the running version if the checks fail, and puts the last
known-good commit back on its own if the new one starts and dies three times inside ten minutes. Tell the
operator that restarting the app is how a change goes live, and that closing and reopening it does the
same. A change to `deploy/Dockerfile` or `deploy/apt-packages.txt` needs a new image, which a restart
cannot deliver: the app says so and the fix is `docker compose … up -d --build` (or
`daedalus-desktop update`). The virtualenv is on the `daedalus-venv` volume so it survives a replaced
container; it is re-synced only when `uv.lock` or `pyproject.toml` changed.

In `server` mode the agent updates itself through pull requests the operator approves; the supervisor
pulls `main`, preflights and restarts (rolling back on failure). To update by hand: `git -C
~/daedalus pull`, `git -C ~/protocore-exp pull`, then `docker compose … up -d --build`. In `server`
mode never edit files inside the `daedalus` checkout on the server: the supervisor resets it to
`origin/main` on every rebuild.

## 8. What to report back

Tell the operator: the app address, how to log in (the pairing link, then a passkey), which model is the
default and what it costs, the daily cap, and whether Telegram is on. Do not paste tokens or keys into
the chat.
