---
name: self-develop
description: How to change your own code (host or core) safely: worktree, tests, pull request, rebuild.
---
# Self-development workflow

1. `self_workspace(repo, branch)` — get a worktree on `agent/<branch>` from `origin/main`.
   `repo` is `bot` (host: tools, providers, transport, Mini App, skills) or `core` (agent loop).
2. Edit inside the worktree only. Layout of the host repository:
   - `daedalus/tools/<name>.py` — one tool per module, exported in `TOOLS`; use `@tool` from the core.
   - `daedalus/providers/` — OpenAI-compatible adapter; a new endpoint is usually just config.
   - `daedalus/transport/telegram/` — chat surface. `daedalus/extensions/` — scheduler, self-dev, API.
   - `skills/<id>/SKILL.md` — skills (front matter: name, description).
   - `tests/unit`, `tests/smoke` — smoke tests are the supervisor's preflight gate; keep them fast.
3. Run the gates in the worktree: `uv sync --extra dev && uv run pytest -q` (host) or
   `uv run pytest -q` (core, ~3k tests; run the subset you touched first).
4. Commit with a message that describes the change on its own terms.
5. `self_propose(repo, title, summary)` — opens the pull request and a decision card for the operator.
6. After the merge, `self_rebuild(reason)`. The supervisor pulls main, runs preflight and restarts;
   a failing preflight rolls back automatically and reports why.

Rules: never edit the running checkout, never push to main, keep changes small and tested.
