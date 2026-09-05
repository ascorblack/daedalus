---
name: scheduling
description: Creating recurring or one-shot tasks that run later in their own session and workspace.
---
# Scheduling tasks

- `schedule_create(name, prompt, cron=...)` for recurring, `run_at="2026-01-31T09:00:00Z"` for one-shot.
  Cron is 5-field crontab syntax in UTC (`0 7 * * 1-5` = weekdays 07:00 UTC).
- The prompt is read by a future session that knows nothing about this conversation: state
  the goal, the inputs, where to put results and how to report (send_file, or just the reply).
- Attach files with `files=[...]`; they are copied into the task's own workspace under `inbox/`.
- A recurring task keeps one workspace across runs and receives the previous run's `SUMMARY.md`
  in its prompt. Tell the future session to keep `SUMMARY.md` short and factual.
- `schedule_list()` / `schedule_delete(id)` to inspect and remove. The operator sees the same
  list with /schedules.
