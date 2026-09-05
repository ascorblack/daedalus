# Acceptance log

Live checks of the build units, newest first.

| date | unit | result |
|---|---|---|
| 2026-09-06 | 1 providers | DeepSeek live: streaming text, thinking deltas, tool call, usage with cache fields recorded; structured JSON |
| 2026-09-06 | 2 tools/stores | terminal session: exec + write + read completed; events and usage in SQLite |
| 2026-09-06 | 3 sessions | AskUser pause → answer → resume completed in the terminal runner |
| 2026-09-06 | 6 self-develop | worktree → PR #1 → approve → squash merge on GitHub; worktree and branch cleaned up |
| 2026-09-06 | 9 mini app | production build succeeds; API serves /app and rejects unauthenticated calls |
| 2026-09-06 | 4 supervisor | integration test: good commit applied via rebuild; syntax-broken commit rolled back by preflight; rollback to earlier known-good; Docker image builds |
| 2026-09-06 | 5 telegram | unit tests with constructed updates: owner filter, direct session, fragment/file merge window, /new topic binding, AskUser keyboard → answer |
| 2026-09-06 | 9 mini app | served publicly behind the host reverse proxy over HTTPS; API returns 401 without initData/token |
| 2026-09-06 | 7 scheduler | one-shot task created through the public API fired on time in its own session and workspace, wrote SUMMARY.md (stored as last_summary), then disabled itself |
| 2026-09-06 | review | independent review (2 critical, 11 high, 17 medium, 14 low): all critical/high and most medium fixed; see daedalus-private notes; suite 32 green |
| 2026-09-06 | 4 docker | compose stack up on the host: bot + local Bot API server + rebuilder; supervisor rebuild through its socket: fetch → preflight → restart, outcome recorded |
| 2026-09-06 | 9 mini app | browser e2e (Playwright) on the production stack: settings edits, new session with exec, follow-up, files browser, all tabs |
| 2026-09-06 | e2e | full Telegram pass from the owner's account (user-bot): bind, topics, CSV+bash, file back, follow-up mid-run, AskUser keyboard, photo→inbox, ImageView, MCP enable+tool, schedule → cron topic → reply, /usage /sessions /close — 21/21 |

