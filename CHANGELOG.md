# Changelog

Notable changes, newest first. The repository's `main` is the released version.

## 2026-09-11

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
