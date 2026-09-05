---
name: telegram-output
description: How to shape replies for Telegram: short messages, files for long output, images inline.
---
# Writing for Telegram

- A message is at most 4096 characters; the transport splits longer replies, but a wall of
  text reads badly on a phone. Aim for a few short paragraphs or a bulleted list.
- Anything longer than a screen (logs, reports, tables, generated code) goes to a file:
  write it in the workspace and call `send_file(path, caption)`. Markdown files render
  well; use `.md` for reports.
- Images render inline when sent with `send_file` (png, jpg). Plots: save with matplotlib
  to the workspace, then send.
- Markdown that works: `**bold**`, `*italic*`, `` `code` ``, fenced code blocks, links.
  Tables do not render; use a file or a compact list instead.
- Progress is shown automatically in the status message; do not narrate every step.
- End with the outcome and, if relevant, one line about what you would do next.
