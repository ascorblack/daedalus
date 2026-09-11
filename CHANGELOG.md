# Changelog

Notable changes, newest first. The repository's `main` is the released version.

## 2026-09-11

- **The app is an application.** Every screen is an address under `/app` (`/app/agents/<id>` is a session,
  `/app/settings/models` a settings section), so Back, a reload, Telegram's back button and a link all land on
  the same screen; the server answers the app for any of its paths. A phone shows four tabs (Agents, Inbox,
  Board, More) with the rest behind More; a desktop has a rail with grouped destinations that folds to icons, a
  search-and-go palette on Ctrl/⌘ K, and `g` + a letter to jump between screens. Each screen names itself and
  keeps its actions in its header; the brand line is gone.
- **Rows instead of cards.** Agents are grouped by what they need (Needs you, Working, Loops, Idle) and carry the
  model, the time and the loop line, with subagents inside the leader's card and no raw ids. Inbox entries have a
  kind icon and severity, repeats fold into one card with a count, and a pending change is a card with Approve
  and Reject on it. The board is a kanban on a wide screen (cards drag between columns) and sections on a phone;
  a task shows its priority as a stripe and its checklist as a bar. A schedule reads as a sentence in the reader's
  own clock and is made from a moment, a daily or weekly time or a cadence; cron stays for the rest. Services
  open with one button and say who can reach them in plain words. Memories are clamped cards with bulk selection
  from the header. Usage leads with what is metered today, the tightest quota and the tokens, and every quota
  line says what is left and when it resets. Settings is an index of sections, side by side on a wide screen.
- **A session in less chrome.** The header is back, title and one status line (model, context when it matters,
  subagents, loop) with an overflow menu; tokens, cost, ids and the settings live in the Session info sheet. A
  live bar over the composer says which step the agent is on; the composer is one line, Stop asks first, and
  revert and fork sit in a menu on the message. On wide screens the sessions list can stay beside the
  conversation and the inspector sits on the right.
- **Shared behaviour.** Confirmations say what the action does and focus the safe button; deletions can be
  undone for five seconds; Escape closes only the top layer; times, tokens, money and schedules format through
  one module in the reader's locale, never with seconds or an ISO timestamp; `idle` is grey, so working and
  waiting stand out. A schedule can be edited and paused (`PATCH /api/schedules/{id}`), and the roster carries
  each session's model.
- **Removed models stay removed.** The Claude subscription seed re-added its presets on every start; a seed is
  now applied once and recorded in the config (`seeded`).
- **Settings, models and clients.** One line per model and per client — label, client · model, and pills for
  default, fallback, thinking, images and window — that opens into an editing panel; the same rows stack on a
  phone and spread on a desktop.
- **Rebuild without the outage.** The supervisor preflights a merged revision on a candidate checkout while
  the bot keeps serving, and stops it only to sync dependencies and restart; a revision that fails the
  preflight never touches the running bot. Sandbox wrappers the bot leaves behind are reaped.
- **Services run inside the sandbox.** `ServiceStart` ran its command outside the wall `Exec` has, and the agent
  used it to write where `Exec` could not. A service now runs under the same sandbox; the worktrees a session
  opens with `SelfWorkspace` are writable for that session's commands, which is where those writes belonged.
  A service can be removed from the app (stopped first if it runs); subagents are no longer listed twice in a
  session's sidebar.
- **Fallback chain.** A rung whose context window cannot hold the current prompt plus its output cap is
  skipped with that reason; when nothing fits, the loop retries where it was instead of ending the run on a
  model that could not take it. A subagent that ends without a reply names the error that ended it.
- **Compaction.** The whole-history pass also runs before a run that would start over the ratio (a session
  switched to a smaller-window model); the summariser's cap fits a summary in any script, a unit the
  summariser keeps failing on is left alone, and a compaction pass no longer renames the topic there and back.
- **OpenCode Go.** A provider kind `opencode` for the OpenCode Go / Zen gateway: the key proxy knows the upstream
  (`OPENCODE_API_KEY`), every request carries the session id the gateway routes and caches by, thinking goes out
  in DeepSeek's shape, and presets for its models carry the gateway's list prices so the metered spend tracks the
  subscription's allowance. Prices for the gateway's models are refreshed daily from models.dev (the operator's own
  entries win), and the Usage screen shows the subscription's 5-hour, weekly and monthly allowance windows next
  to the Codex, Grok and Claude ones.

## 2026-09-10

- **Reasoning that outruns the output cap.** The core no longer keeps a reasoning block the cap cut, and no
  longer asks the model to resume it: the retry sends the same prompt with the effort lowered, then with thinking
  off, at most twice, and the knobs return afterwards. DeepSeek presets send the effort names DeepSeek knows
  (`medium` is its default `high`). Size a DeepSeek preset's `max_output_tokens` at the vendor's thinking-mode
  default, 64K: at 32K the model ran out of budget while still reasoning on hard tasks.
- **Bench.** Memory tools are off in benchmark sessions; an Exec that hits its timeout says how to run the
  command in the background; a task that declares GPUs is excluded with `-x` rather than aborting the job.
- **Self-development gates.** `SelfPropose` takes an `execution_path`; a host-module change is refused when nothing
  reaches it, when no passing Verify receipt names the changed code, or when a large change does not say what it
  replaces. The preflight asserts every module is reachable from the entry points.
- **Tool policy.** Built-in rules refuse commands that act on the machine, the operator's paths or checkouts; a
  forced push, deleting a workspace or a host outside the egress allowlist need the operator's approval key
  (`/allow <key>`, the app, or the API). Operator rules (`[policy]`) tighten, never loosen. Operator scripts
  (`[hooks]`) run before and after tools and after runs.
- **Bench.** `daedalus bench <manifest>` runs recorded tasks headless and records verdict, turns, tokens, cost and a
  trajectory per task; `daedalus.bench.harbor:DaedalusAgent` runs the same loop under Harbor against benchmark
  containers. A provider endpoint can pin its sampling temperature.
- **Tools.** `Exec(background=true)` with `JobOutput`/`JobKill`/`JobList`; long output keeps head and tail and is
  spilled to a file; `Edit` matches loosely and shows the closest lines on a miss; `MultiEdit`; a written Python
  file is parsed and linted in the result; `SubAgent` takes `expects`, `deliverable` and `persona`; `Skill` fills
  a recipe's parameters; `SkillDraft` saves a skill distilled from a session.
- **Modes and budgets.** A `plan` mode allows only read-only tools until the operator switches it; runs can be
  capped by active minutes and by tokens; the compaction summariser and memory extraction can run on a cheaper
  preset; `AGENTS.md` and the session's open board tasks ride along in the prompt; the board writes `PLAN.md`.
- **Ops.** The sandbox fails closed; every tool call leaves a timing row and every network host an egress row
  (shown in the app); a session exports as one Markdown file; `deploy/setup.sh` sets a new installation up.
- **Memory.** Recall ranks with BM25 and recency; optional extraction of durable facts after a run.
