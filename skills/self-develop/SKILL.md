---
name: self-develop
description: How to change your own code (host or core) safely: worktree, tests, pull request, rebuild.
---
# Self-development workflow

1. `SelfWorkspace(repo, branch)` — get a worktree on `agent/<branch>` from `origin/main`.
   `repo` is `bot` (host: tools, providers, transport, Mini App, skills) or `core` (agent loop).
2. Edit inside the worktree only. Layout of the host repository:
   - `daedalus/tools/<name>.py` — one tool per module, exported in `TOOLS`; use `@tool` from the core.
   - `daedalus/providers/` — OpenAI-compatible adapter; a new endpoint is usually just config.
   - `daedalus/transport/telegram/` — chat surface. `daedalus/extensions/` — scheduler, self-dev, API.
   - `skills/<id>/SKILL.md` — skills (front matter: name, description).
   - `tests/unit`, `tests/smoke` — smoke tests are the supervisor's preflight gate; keep them fast.
3. Run the gates in the worktree **through Verify**, so the receipts land on the proposal card:
   `Verify("unit suite passes", "uv sync --extra dev && uv run pytest -q", cwd=<worktree>)` (host) or
   `uv run pytest -q` (core, ~3k tests; run the subset you touched first, then the whole suite).
   A proposal whose card says "nothing was checked with Verify" is asking the operator to trust prose.
   Before proposing, walk this checklist and say in the summary what you found:
   - every reader of a field or threshold you changed (`grep` the name across the repo) agrees with
     the new rule — a check added in one place and an older check elsewhere must not disagree;
   - the new path behaves when the object is missing, not linked, expired, or restarted mid-way;
   - a fast path you add does not repeat work already done by the slow path it precedes;
   - the full suite and `ruff check` pass, not only the tests you wrote.
4. Commit with a message that describes the change on its own terms. The repositories are public:
   no session or run ids, no trailers or co-author lines, no model or tool names as authors, and
   nothing about the operator (addresses, hostnames, paths, accounts, workloads, circumstances) in
   commits, pull requests, code comments or docs. What you know about the operator stays in the session.
5. `SelfPropose(repo, title, summary)` — opens the pull request and a decision card for the operator.
6. After the merge, `SelfRebuild(reason)`. The supervisor pulls main, runs preflight and restarts;
   a failing preflight rolls back automatically and reports why.

Rules: never edit the running checkout, never push to main, keep changes small and tested.
