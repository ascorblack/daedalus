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
- **Rich replies.** Answers go out as Telegram rich messages: Markdown with headings,
  tables, code blocks, quotes and collapsible blocks renders natively. In the private chat
  the answer also streams as a live draft (with a Stop button); everywhere a status message
  shows the run's vitals with the tool log folded into a collapsible block.
- **Mini App.** A Telegram Mini App (and plain browser page) with the roster of sessions,
  live transcripts (kept in full even after the model's context is compacted), change
  proposals with diffs, scheduled tasks, usage and settings.
- **Self-development.** The agent edits its host code or its core in a git worktree, opens a
  pull request, and you approve or reject it (with a reason) in chat. After the merge the
  supervisor pulls, runs preflight checks and restarts; a bad build rolls back on its own.
- **Scheduler.** Recurring or one-shot tasks that run in fresh sessions with a persistent
  workspace and a summary handed from run to run.
- **Spend visibility.** Every provider response is recorded as reported; balance thresholds
  alert you in chat; a daily cap is enforced by the supervisor.
- **Any OpenAI-compatible model.** Clients are endpoints (DeepSeek, OpenRouter, self-hosted
  vLLM, anything OpenAI-compatible, each with its own base_url, optional key and timeout).
  Models are named presets on top of them: client, model id, label, thinking mode and effort,
  image support, context window and output cap, several per client if you like. One preset is
  the default for new sessions, others can be marked as fallbacks, any session can switch from
  the chat, and ImageView uses whichever image-capable preset you point it at.
- **Eyes on demand.** `ImageView` sends an image to a small vision model (OpenRouter,
  `qwen/qwen3.7-flash` by default) and returns what the agent asked about it, so the main
  model's context never carries raw pixels.
- **MCP servers per session.** Configure servers under `[mcp.servers.<name>]`; every session
  starts with them off. The agent enables one with `McpEnable`, you toggle them in the Mini
  App; their tools appear as `Mcp_<Server>_<tool>`.

## Run it

Requirements: Docker with Compose, a Telegram bot token, your numeric Telegram user id,
Telegram API credentials for the local Bot API server (files above 20 MB), and at least one
model API key.

```bash
git clone https://github.com/ascorblack/daedalus
git clone https://github.com/ascorblack/protocore-exp   # next to it
cd daedalus
cp deploy/env.example .env      # fill in the values
mkdir -p ../daedalus-secrets && cp deploy/keyproxy.env.example ../daedalus-secrets/keyproxy.env
chmod 600 ../daedalus-secrets/keyproxy.env   # provider keys go HERE, outside the checkout
docker compose -f deploy/compose.yaml --env-file .env up -d --build
```

Your ChatGPT (Codex) and SuperGrok logins can serve as model providers: the key proxy
reads the CLIs' own login files (`~/.codex/auth.json`, `~/.grok/auth.json`), refreshes them, and
exposes them as `codex` and `grok` providers; the Usage screen shows their quota windows. This
follows the practice of pi and OpenCode — OpenAI documents ChatGPT sign-in for Codex clients and
xAI books such use under its own "API product" category. Claude is deliberately not bridged.

Provider keys never enter the agent container: a small key-proxy container holds them and injects
them into upstream calls (`http://keyproxy:3200/deepseek`, `…/openrouter`, `…/openai`, plus any
`KEYPROXY_UPSTREAM_<NAME>` you add). The proxy also refuses model calls once the daily budget is spent.

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
| `/rename <title>` | rename the session and its topic (also from the Mini App header) |
| `/compact [focus]` | replace the session history with a model-written summary (shown collapsed in the Mini App) |
| `/prompt` | show the working rules of the system prompt (edit them in the Mini App → Settings) |
| `/sessions`, `/status` | list sessions; what is running |
| `/model [provider/]name\|default`, `/thinking on\|off\|low\|medium\|high` | model settings (default in General, per session in a topic; the Mini App chat has a model picker too) |
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
  tools/        one tool per module, PascalCase names: Exec, Read, Write, Edit, Find, Search, WebFetch,
                WebSearch, ImageView, SendFile, SpawnAgent, SubAgent, AskPeer, Self*, Schedule*, Mcp*
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
commands and the Mini App: model presets (`[presets.<id>]` with provider, model, thinking,
reasoning_effort, images, context_window, max_output_tokens; `[model] preset` and `chain` pick
the default and the fallbacks), the working rules of the system prompt
(`[prompt] rules`, empty = built-in default), fallback chain, approval mode, spend limits,
balance thresholds, scheduler behaviour, per-model pricing overrides (DeepSeek list prices with
their peak/off-peak schedule are built in), the vision model, and MCP servers:

```toml
[mcp.servers.filesystem]
transport = "stdio"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/srv/workspaces"]
description = "read and write files under the workspaces directory"

[mcp.servers.remote]
transport = "http"
url = "https://example.com/mcp"
headers = { Authorization = "Bearer ..." }
```

## License

MIT.
