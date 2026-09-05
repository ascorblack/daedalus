# Daedalus

A personal, self-developing agent that lives in Telegram and runs inside its own Linux
container. One operator, unrestricted tools inside the container, and the ability to change
its own code through pull requests you approve from the chat.

Built on [protocore](https://github.com/ascorblack-labs/protocore-community), the open agent
core; the copy it runs on is [protocore-exp](https://github.com/ascorblack/protocore-exp).

## What it does

- **Sessions as forum topics.** Each topic in your Telegram group is one agent session with
  its own workspace. Write to a topic, send files, get files back. Several sessions run in
  parallel; `/new <title>` opens another.
- **Follow-ups at any time.** Messages sent while the agent works are queued into the run.
  Questions from the agent arrive as inline buttons.
- **Mini App.** A Telegram Mini App (and plain browser page) with the roster of sessions,
  live transcripts, change proposals with diffs, scheduled tasks, usage and settings.
- **Self-development.** The agent edits its host code or its core in a git worktree, opens a
  pull request, and you approve or reject it (with a reason) in chat. After the merge the
  supervisor pulls, runs preflight checks and restarts; a bad build rolls back on its own.
- **Scheduler.** Recurring or one-shot tasks that run in fresh sessions with a persistent
  workspace and a summary handed from run to run.
- **Spend visibility.** Every provider response is recorded as reported; balance thresholds
  alert you in chat; a daily cap is enforced by the supervisor.
- **Any OpenAI-compatible model.** DeepSeek by default (thinking mode configurable), OpenRouter
  and self-hosted vLLM out of the box, with a fallback chain.

## Run it

Requirements: Docker with Compose, a Telegram bot token, your numeric Telegram user id,
Telegram API credentials for the local Bot API server (files above 20 MB), and at least one
model API key.

```bash
git clone https://github.com/ascorblack/daedalus
git clone https://github.com/ascorblack/protocore-exp   # next to it
cd daedalus
cp deploy/env.example .env      # fill in the values
docker compose -f deploy/compose.yaml --env-file .env up -d --build
```

Then, in Telegram:

1. Send `/start` to the bot in a private chat. That chat is a single session on its own.
2. For parallel sessions, create a supergroup with topics enabled, add the bot as an
   administrator with *manage topics*, and send `/bind` there. From now on `/new <title>`
   creates a topic per session; topics you create by hand are adopted too.
3. Open the Mini App with `/app` (set `MINIAPP_PUBLIC_URL` to an HTTPS address that proxies
   to port 8765 and register it as the bot's menu button in @BotFather).

Without Docker, for development:

```bash
uv sync --extra dev
uv run python -m daedalus check                  # configuration and tool registry
uv run python -m daedalus run -p "say hello"     # one session in the terminal
uv run python -m daedalus serve                  # the bot
uv run pytest -q                                 # tests
```

## Commands

| Command | Effect |
|---|---|
| `/new <title>` | new session (a new topic when a group is bound) |
| `/stop`, `/close` | stop the current run; close this session's topic |
| `/sessions`, `/status` | list sessions; what is running |
| `/model [provider/]name`, `/thinking on\|off\|low\|medium\|high` | model settings (default in General, per session in a topic) |
| `/usage`, `/balance` | spend today and per session; provider balances |
| `/schedules`, `/schedule run\|on\|off\|delete <id>` | scheduled tasks |
| `/approval manual\|auto`, `/verbosity 0\|1\|2` | self-change approval mode; chat detail |
| `/rebuild`, `/rollback [n]`, `/panic` | supervisor operations |
| `/settings`, `/app` | current configuration; Mini App link |

## Layout

```
daedalus/
  host/         sessions, engine wiring, prompts, skills store
  providers/    OpenAI-compatible adapter, fallback chain, registry
  tools/        one tool per module (exec, read, write, edit, find, search, web, files, self_*, schedule_*)
  stores/       SQLite stores, blob store, durable memory
  transport/    Telegram (aiogram 3)
  extensions/   self-development, scheduler, balance monitor, HTTP API
launcher/       the supervisor (PID 1, never edited by the agent)
miniapp/        Vite + React Mini App
skills/         SKILL.md skills the agent can load
deploy/         Dockerfile, compose, env example
```

The agent's changes land through pull requests in this repository and in `protocore-exp`.
`GOVERNANCE.md` holds the rules that are always in context and never editable by tools.

## Configuration

Secrets and machine facts live in `.env` (see `deploy/env.example`). Everything the operator
may change at runtime lives in `config.toml` on the state volume and is edited through the bot
commands and the Mini App: model and thinking, fallback chain, approval mode, spend limits,
balance thresholds, scheduler behaviour, per-model pricing for cost estimates.

## License

MIT.
