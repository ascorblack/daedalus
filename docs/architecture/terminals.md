# Terminals: the daemon and its protocol

Terminals are real pseudo-terminals owned by `ptyd`, a small daemon written in Go (`ptyd/`). There
is one daemon per **environment**: `container` (a compose service beside the agent's container)
and `host` (the operator's machine). The Daedalus host talks to each over a local socket and relays
browser attachments to it. `ptyd` knows terminals, bytes, screens and processes; it knows nothing
about sessions, projects or agents.

This document is the contract both sides are tested against. A section marked *not yet* describes a
part of the protocol whose method names and shapes are fixed but which the daemon does not serve yet;
calling it returns `-32601 method not found`.

## Running it

```
ptyd serve --env container --run-dir /run/ptyd --state-dir /var/lib/ptyd
ptyd version
```

| Flag | Default and meaning |
|---|---|
| `--env` | the environment's name, echoed to clients (required) |
| `--run-dir` | the run directory: endpoint, token, socket (required) |
| `--state-dir` | the journal of agent writes, terminal logs, launch directories and dial sockets (required) |
| `--listen` | `unix` (a socket in the run directory), or `tcp:127.0.0.1:<port>` |
| `--home` | the home directory of spawned programs (`$HOME`) |
| `--shell` | the login shell: `$SHELL`, then the user's passwd entry, then `/bin/bash`, then `/bin/sh` |
| `--hooks-listen` | loopback address of the hook listener (`127.0.0.1:0`); anything off the loopback interface is refused |
| `--config` | a JSON file: `{"limits": {"max_terminals", "ring_bytes", "input_idle_ms", "kill_grace_ms"}, "exec": {"allow": []}, "fs": {"roots": [], "deny": []}}`; the lists only add to the built-in ones; unknown keys are refused |
| `--log-file`, `--log-level` | the daemon's own JSON-lines log (stderr, `info`) |

`SIGTERM` or `SIGINT` stops the daemon: the endpoint file is removed first, every running terminal
gets `SIGHUP` and then, after the grace (2 s), `SIGKILL`, all at once, and the socket and token are
removed. `SIGHUP` is ignored. The daemon owns its terminals' processes: when it stops, they stop.
Restarting the Daedalus host is the case that matters, and it leaves the daemon and its terminals
running.

## Run directory and handshake

```
<run>/endpoint   "unix:ptyd.sock" or "tcp:127.0.0.1:<port>"
<run>/token      64 hex characters (32 random bytes), mode 0600, new at every start
<run>/ptyd.sock  mode 0600
<run>/ptyd.lock  held (flock) for the daemon's lifetime
```

- The directory is created with mode 0700. The token is written before the endpoint, and the
  endpoint only once the socket accepts, so an endpoint that exists always leads to a ready daemon.
  A client is told only the directory. A relative `unix:` endpoint is resolved against it, so a
  directory bind-mounted at another path works the same.
- **One daemon per directory.** A second one fails to take the lock, or connects to the existing
  socket, and exits with `another ptyd holds the run directory`. A socket left by a daemon that died
  is removed.
- **Handshake.** The first frame, on channel 0, carries the token exactly as the file holds it
  (trailing whitespace ignored). A good token is answered with a notification
  `{"jsonrpc":"2.0","method":"hello","params":{"version","protocol","instance","env"}}`. A wrong
  one waits one second and closes. `instance` is new at every start of the daemon. The client
  refuses a `protocol` it does not know and reports the environment unavailable.

## Framing

```
frame   := u32be length | u32be channel | payload     (length = 4 + len(payload), at most 1 MiB + 4)
channel 0     : one UTF-8 JSON-RPC 2.0 message per frame
channel n > 0 : an attachment (payload = one browser frame) or a byte stream
empty payload : closed from that side; the other side answers with its own empty frame
```

Channel ids are allocated by the daemon, increase, and are never reused within a connection.
Requests run concurrently; at most 256 may be in flight per connection. Errors use the JSON-RPC
codes plus:

| Code | Name | Meaning |
|---|---|---|
| 1001 | `not_found` | no such terminal |
| 1002 | `exited` | the terminal has exited |
| 1003 | `limit` | a limit was reached (terminals, requests, reply size, write size) |
| 1004 | `forbidden` | not in this state: the id exists, the terminal still runs |
| 1005 | `timeout` | |
| 1006 | `keyboard_held` | a human holds the keyboard and an agent write timed out waiting |
| 1007 | `unsupported` | not in this build or on this platform |
| 1008 | `stale_launch` | the launch is not registered, or it has ended |
| 1009 | `invalid_size` | outside 20×4 … 500×300 |

Parameters are decoded strictly: an unknown field is `-32602`.

## Methods

| Method | Params → result | |
|---|---|---|
| `daemon.info` | → `{version, protocol, instance, env, os, arch, pid, started_at, uptime_s, home, shell, capabilities{sandbox, shells[], shell_integration[], emulator, stats}, hooks{listen, launches, held}, side_channels{exec_allow[], fs_roots[], state_dir}, limits{…}, counts{running, exited}, machine}` | |
| `terminal.create` | see below → `{id, pid, cwd, cwd_fallback, shell, created_at}` | |
| `terminal.list` | `{ids?, preview_rows? 0..12}` → `{terminals:[Info]}` | previews *not yet* |
| `terminal.get` | `{id}` → `Info` | |
| `terminal.write` | see below → `{bytes, seq_before, queued_ms, delivered_at}` | |
| `terminal.resize` | `{id, cols, rows, px_w?, px_h?}` → `{cols, rows}`; the host becomes the size owner | |
| `terminal.signal` | `{id, signal, group? = true}` | |
| `terminal.kill` | `{id, grace_ms? ≤ 30000}` → `{exit_code, signal}` | |
| `terminal.forget` | `{id}`, exited terminals only | |
| `terminal.read_output` | `{id, since_seq, max_bytes? ≤ 1 MiB, strip?}` → `{from_seq, to_seq, head_seq, gap, data \| data_b64}` | |
| `terminal.stats` | `{ids?}` → `{at, supported, terminals:[{id, pid, processes, rss_bytes, cpu_percent}], machine}` | |
| `events.subscribe` | `{after_seq}` → `{instance, from_seq, resync}`, then `event` notifications | |
| `events.unsubscribe` | | |
| `terminal.attach`, `terminal.detach`, `terminal.keyboard` | attachments, size ownership, the keyboard | *not yet* |
| `terminal.snapshot`, `terminal.read_screen`, `terminal.wait_for`, `terminal.commands` | the screen | *not yet* |
| `exec.run`, `fs.*`, `net.dial`, `net.allow`, `hooks.*` | side channels for CLI adapters: see below | |

### `terminal.create`

```
{id, argv[] | shell{program?, login? = true}, cwd?, env{}, strip_env[], cols?, rows?, title?,
 ring_bytes?, log_to_disk?, input_idle_ms?, sandbox?, launch_id?, labels{}}
```

- `id` is the host's: 1–64 of `A-Z a-z 0-9 - _`. An id in use (running or exited, not forgotten) is
  `1004`.
- `argv` is run exactly, never typed into a shell. Without it the login shell runs, as `<shell> -l`.
  `argv[0]` is resolved against the `PATH` of the program's own environment.
- `cwd` must be absolute. A missing directory falls back to the home directory and the reply says
  `cwd_fallback: true`.
- Size defaults to 80×24; outside 20×4 … 500×300 it is `1009`.
- `labels` (at most 32, each at most 256 bytes) are stored and echoed, never interpreted.
- `log_to_disk` also writes the output to `<state>/terminals/<id>.log` (rotated at 32 MiB).
- `sandbox` is `1007` in this build.
- Beyond `max_terminals` running terminals (128) it is `1003`.

### `Info`

```
{id, pid, argv, cwd, title, status: "running"|"exited", exit_code, exit_signal, created_at,
 exited_at, last_output_at, last_input_at, last_human_input_at, cols, rows, size_owner, clients[],
 last_detach_at, keyboard{owner, until}, modes{alt_screen, bracketed_paste, mouse, app_cursor,
 mouse_mode?, kitty_flags?}, busy, last_command{command, exit_code, at}, labels, launch_id, sandbox,
 shell, output_seq, preview?}
```

`busy` means a job other than the terminal's own program holds the foreground: under a shell, a
command is running. `cwd` and `title` follow OSC 7 and OSC 0/2. `output_seq` is one past the last
output byte.

### Writes

`terminal.write {id, text? | paste? | keys[]? | bytes_b64?, origin{kind: "agent", actor, launch_id?,
note?}, wait? = "keyboard" | "none", timeout_ms? = 30000 (≤ 600000)}` — exactly one payload, at most
1 MiB.

- `text` and `bytes_b64` are written as they are.
- `paste` is wrapped in `ESC[200~ … ESC[201~` when the application turned bracketed paste on, with
  every marker inside the text removed so a paste cannot end itself early. Without bracketed paste,
  line feeds become carriage returns.
- `keys` are named: `Enter Tab S-Tab Esc Backspace Up Down Left Right Home End PgUp PgDn Delete
  Insert F1…F12 C-<c> M-<key>`. Arrows, Home and End follow the application's cursor-key mode.
- **Arbitration.** A human keystroke goes straight through and takes the keyboard. An agent write
  with `wait: "keyboard"` waits until the human has been idle for `input_idle_ms` (10 s) and no human
  holds the keyboard; a grant to agents lets it through at once, and human typing ends that grant.
  A write that times out is `1006` when a human holds the keyboard, else `1005`. Once a write has
  started it is finished without waiting again, in 4 KiB chunks, so a keystroke goes in between
  chunks but never splits the agent's text into two deliveries.
- The receipt means the bytes were written to the PTY. It never means the program read them.
- Every delivered agent write is appended to `<state>/agent-writes.jsonl` (rotated 16 MiB × 3): time,
  terminal, actor, launch, kind, byte count, the first 4 KiB and the SHA-256 of the whole. Human
  keystrokes are never recorded anywhere.

### Output

Every terminal keeps a ring of its output (8 MiB, 1–64 MiB per terminal), addressed by absolute byte
offset since the terminal started. `read_output` returns bytes from `since_seq`; `gap: true` means
older bytes had already left the ring and the read starts at the oldest byte held. Raw output comes
as `data_b64` (at most 512 KiB per call); with `strip: true` it comes as text in `data` (at most
768 KiB per call) with escape sequences removed, CR LF and lone CR as LF, and the read ending on a
character boundary. `to_seq` is where the next read starts. When a terminal exits, its ring shrinks to
its last 64 KiB.

### Signals and ending

- `terminal.signal` accepts `INT TERM HUP KILL QUIT TSTP CONT WINCH USR1 USR2`. With `group` (the
  default) it goes to the PTY's foreground process group, as a typed Ctrl+C would; otherwise to the
  program alone.
- `terminal.kill` sends `SIGHUP` to the program's group and the foreground group, waits up to the
  grace for the program to exit, then sends `SIGKILL` to the group and to every process of the
  terminal it can find: on Linux, the process tree, the session, and every process whose environment
  carries the terminal's `DAEDALUS_TERMINAL_ID`. That catches a child that called `setsid` after its
  parent exited. Outside Linux only the process group is signalled.
- A program that exits while a background job still holds the PTY open is reported exited after
  half a second of draining; closing the PTY hangs the job up, as closing a terminal window would.
- Exited terminals are kept, with their screens, for an hour, then forgotten.

### Process statistics

`terminal.stats` and a `terminal.stats` event every 10 s (only while a terminal runs) report, per
terminal, the processes of its tree and session, their summed resident memory, and their CPU use
since the previous sample as a percentage of one CPU. Shared pages are counted once per process, so
the memory is an estimate of cost, not an accounting. `machine` is
`{mem_total_bytes, mem_available_bytes, cgroup_limit_bytes?, cgroup_used_bytes?, cpus, cgroup_cpus?,
cpu_percent, load1, load5, load15}`, read from `/proc` and, inside a container, from its cgroup's
memory and CPU limits. Outside Linux `supported` is false and the numbers are zero.

## Events

An `event` notification carries `{seq, at, type, terminal_id?, data}`. Every event takes its `seq`
from one counter, so a subscriber sees one total order across terminals and kinds — a hook post and
a terminal's output included. The daemon keeps the last 20 000, and no more than 64 MiB of them by
size.

- `events.subscribe {after_seq}` delivers every event after `after_seq`. `resync: true` means some
  were lost: they left the log, or `after_seq` came from an earlier life of the daemon (compare
  `instance`). Delivery then starts at the oldest event held, `from_seq`.
- A subscriber that falls behind by more than the log receives `events.resync {from_seq}` and
  continues from there.

| Type | `data` |
|---|---|
| `terminal.created` | `{pid, argv, cwd, labels, launch_id}` |
| `terminal.exited` | `{exit_code, signal, seq}` — published after every event of the terminal's output |
| `terminal.title` | `{title, seq}` — at most one per 250 ms per terminal, the latest wins |
| `terminal.cwd` | `{cwd, seq}` |
| `terminal.bell` | `{seq}` — at most one per 2 s per terminal |
| `terminal.notify` | `{title, body, seq}` — OSC 9 and OSC 777;notify, at most one per 5 s |
| `terminal.progress` | `{state, value, seq}` — OSC 9;4, at most one per 500 ms, the latest wins |
| `terminal.command` | `{phase: "D", exit_code, command, abs_row, seq}` — OSC 133/633 `D` |
| `terminal.mode` | `{alt_screen, bracketed_paste, mouse, app_cursor, mouse_mode?, kitty_flags?}` |
| `terminal.stats` | a `terminal.stats` result |
| `hook` | `{launch_id, terminal_id, name, body, size, truncated?, reply_id?, hold_ms?}` — a hook post of a launch; tagged with the launch's terminal |
| `launch.ended` | `{launch_id, reason}` — `unregistered`, `terminal_exited`, `expired` or `shutdown` |

`seq` inside `data` is the output offset at which the mark occurred. An event too large for one
frame is delivered as its envelope with `data: {"too_large": true}` rather than ending the
subscription.

## What every consumer of the output may rely on

The output is scanned before anything else sees it, and the same clamped bytes go to the ring, to
the daemon's emulator and to every browser. Several terminal emulators allocate or loop in proportion
to a parameter, or buffer an unterminated string without limit, so:

- every numeric CSI parameter is at most 10 000, and a CSI carries at most 32 parameters and
  sub-parameters;
- an OSC, DCS, APC, PM or SOS payload longer than 64 KiB is dropped whole, and a title (OSC 0/1/2) is
  cut to 4 KiB at a character boundary;
- a sequence interrupted by another is never passed on half-finished: an unfinished CSI or ESC is
  dropped, and an interrupted string is closed with ST;
- a lone UTF-8 lead byte `0xC2` becomes U+FFFD, so dropping a sequence can never join it with a later
  byte into a C1 control;
- terminal sizes are 20×4 … 500×300;
- the queue to the emulator is bounded, so a program that writes faster than its terminal is emulated
  blocks on its own writes instead of growing the daemon.

A sequence split across reads is held until complete; the result does not depend on how the output
was chunked. Per terminal, the daemon's memory is bounded by the ring, 64 KiB of pending sequence,
the emulator's queue and the emulator's scrollback of 10 000 lines.

## The emulator

Each terminal has a headless emulator, fed from the PTY whether or not anyone is attached, behind
one Go interface (`ptyd/internal/emulator`): `Feed`, `Resize`, `Size`, `Cursor`, `Modes`, `History`,
`Snapshot`, `Text`, `Runs`, `Close`. One goroutine per terminal owns it; feeds and questions are
served in order, so every answer corresponds to an exact output offset. Emulators start with
grapheme clustering (mode 2027) on, which is how the browser's width provider measures text.

The current build's emulator tracks only the size and the modes (bracketed paste, cursor-key mode,
the alternate screen, mouse reporting, the kitty keyboard stack), which is what writes need. It
answers no terminal queries and keeps no screen; `daemon.info` names it as `basic@1`.

## Browser frames

The host relays these unchanged between a WebSocket and an attachment channel. Big-endian; the first
byte is the type.

| Direction | Frame | Layout |
|---|---|---|
| to the client | `0x01 OUTPUT` | `[u64 seq][bytes]`, `seq` the offset of the first byte |
| to the client | `0x02 SNAPSHOT` | `[u16 cols][u16 rows][u64 seq][vt bytes]`; the stream continues at `seq` |
| to the client | `0x03 EVENT` | a JSON object with a string `type` |
| to the daemon | `0x10 INPUT` | `[bytes]`, at most 32 KiB |
| to the daemon | `0x11 RESIZE` | `[u16 cols][u16 rows][u16 px_w][u16 px_h]` |
| to the daemon | `0x12 ACK` | `[u64 seq]`, the offset the client has parsed |
| to the daemon | `0x13 ATTACH` | `{lastSeq, haveState, readOnly, scrollback?, theme?{fg, bg, cursor}}` |

Offsets are at most 2^53 − 1, the largest integer a browser holds exactly. The golden frames are in
`ptyd/internal/wire/testdata/frames.json` and `miniapp/src/terminal/testdata/frames.json`, which are
byte-identical (a host test checks it); each codec is tested against its copy.

**Attachments** (*not yet*). On ATTACH, a client whose `lastSeq` is still in the ring, and whose
terminal was not resized since, receives OUTPUT from `lastSeq`; any other gets `resync` and a
SNAPSHOT at the PTY's size, then OUTPUT from the snapshot's offset. The client resets its terminal
before writing a snapshot. The daemon batches OUTPUT (64 KiB or 8 ms), stops sending past 256 KiB
unacknowledged, and sends a snapshot instead of a backlog that left the ring or grew past 1 MiB; the
PTY reader never waits for a client. The most recently active client owns the size; a size below
20×4 is refused with an `error` event. The daemon is the only answerer of terminal queries; clients
swallow them.

## The environment of a spawned program

The daemon's environment, minus `DAEDALUS_PTYD_*`, `DAEDALUS_TERMINAL_ID`, `DAEDALUS_LAUNCH_*`,
`DAEDALUS_HOOK_*`, `DAEDALUS_DIAL_DIR`, `TERM_PROGRAM*`, `VSCODE_*`, `TMUX*`, `STY`, `WINDOW`,
`KITTY_*`, `ITERM_*`, `WT_SESSION`, `CLAUDE*` and the caller's `strip_env` patterns (a name, or a
prefix ending in `*`); plus `TERM=xterm-256color`, `COLORTERM=truecolor`, `CLAUDE_CODE_NO_FLICKER=1`,
`CLAUDE_CODE_SCROLL_SPEED=3`, `DAEDALUS_TERMINAL_ID=<id>` and `HOME`; plus the caller's `env` on top.
When the effective character type (the first of `LC_ALL`, `LC_CTYPE`, `LANG` that is set) is not
UTF-8, the non-UTF-8 overrides are removed and `LANG=C.UTF-8` is set; a user's `ru_RU.UTF-8` is
kept.

## Side channels

What a CLI adapter reaches besides a terminal. Every one is fenced by a list, all over the same
authenticated socket.

### `exec.run`

`{argv, cwd?, env{}, timeout_ms? ≤ 1 800 000, stdin_b64?, max_output? ≤ 4 MiB}` →
`{exit_code, signal, stdout, stderr, truncated, timed_out, duration_ms, path}`

- Not a terminal: the program runs in its own process group, with the environment a terminal gets
  (without `DAEDALUS_TERMINAL_ID`), and the whole group gets `SIGHUP` and then, after 2 s, `SIGKILL`
  when the time runs out or the caller's connection closes.
- `argv[0]`, resolved through the program's `PATH`, must have a basename on the list: `claude codex
  opencode pi grok npm npx node git uname`, plus the configuration's `exec.allow`. `1004` otherwise.
  This guards against mistakes, not against a hostile host: the token already starts any terminal.
- stdout and stderr are captured up to `max_output` each (default 1 MiB), the rest read and
  dropped, as text (invalid UTF-8 replaced). One reply is one frame, so together they are cut to
  about 640 KiB; `truncated` says so. A program that fails is a result, not an error. At most 16
  run at once (`1003`).

### `fs.*`

A path must be absolute. It is allowed when, both as written (cleaned) and as the filesystem
resolves it, it lies under a root and matches no deny pattern; the file actually opened is checked
again by the path the kernel reports for it, and its last component is never a followed symlink.
FIFOs and devices are refused.

- **Roots** are the host's list (`fs.set_roots`) plus the configuration's `fs.roots`. `fs.set_roots
  {roots[]}` → `{roots, accepted, refused[{root, reason}]}` replaces the host's list; a root that is
  `/`, holds the home directory, or is the daemon's own is refused.
- **Deny** patterns (`**` any directories, `*` part of a name) are compiled in and only extended by
  the configuration's `fs.deny`: every CLI's login (`**/.claude/.credentials.json`,
  `**/.codex/auth.json`, `**/.grok/auth.json`, `**/.local/share/opencode/auth.json`,
  `**/.pi/agent/auth.json`), `**/.ssh/**`, `**/.gnupg/**`, `**/.config/gh/hosts.yml`, `**/.netrc`,
  `**/.git-credentials`, `**/.docker/config.json`, `**/.aws/**`, `**/.kube/**`,
  `**/.config/gcloud/**`, `**/.azure/**`, `**/.npmrc`, `**/.pypirc`, and the daemon's run and state
  directories. A refusal is `1004` and is written to the daemon's log.

| Method | Params → result |
|---|---|
| `fs.stat` | `{path}` → `{exists, type, size, mtime, mode, file_id}`; a missing path under a root is `{exists: false}` |
| `fs.list` | `{path, glob?, sort? "name"\|"mtime", limit? ≤ 5000}` → `{entries[{name, type, size, mtime}], truncated}`; denied entries are left out, symlinks listed as such |
| `fs.read` | `{path, offset?, max? ≤ 4 MiB}` → `{data_b64, offset, size, eof, file_id}` |
| `fs.tail` | `{path, from_offset, max?, follow_ms? ≤ 60 000, file_id?}` → `{data_b64, next_offset, size, rotated, file_id}` |

One reply carries at most 640 KiB of a file; a longer read continues at its offset. `fs.tail`
waits up to `follow_ms` for bytes past `from_offset` (at most 128 tails wait at once). `rotated`
means the file at the path is another one than `file_id` names, or it shrank; its data then starts
at the beginning of the file.

### Launches and hooks

A **launch** is how a program in a terminal speaks back without typing on a screen.

`hooks.register_launch {launch_id?, terminal_id?, files{name: base64}, ports[], hold_max_ms? ≤ 600 000,
ttl_s?}` → `{launch_id, hook_url, hook_token, dir, dial_dir, env{}, files[]}`

- `launch_id` is the host's, like a terminal id; without one the daemon picks it.
- `files` (at most 32 of 512 KiB, each one plain name) are written 0600 into `dir`,
  `<state>/launches/<launch_id>/` (0700): a CLI's settings overlay, its MCP entry, a long prompt.
- `ports` are the loopback ports `net.dial` may reach for it; `net.allow {launch_id, port}` adds one.
- A terminal created with `launch_id` gets the launch's environment on top of the caller's:
  `DAEDALUS_LAUNCH_ID`, `DAEDALUS_HOOK_URL` (`http://127.0.0.1:<port>/hook/<launch_id>`),
  `DAEDALUS_HOOK_TOKEN`, `DAEDALUS_HOOK_CMD` (`<state>/bin/hook-post`, one path, a link to the
  daemon), `DAEDALUS_PTYD_BIN` (the daemon's executable), `DAEDALUS_DIAL_DIR` (`dial_dir`,
  `<state>/dial/<launch_id>`) and `DAEDALUS_LAUNCH_DIR` (`dir`). `env` in the reply is the same set.
  A launch that is not registered is `1008`.
- It ends 30 s after the last of its terminals exits, after `ttl_s` (default 600) if no terminal was
  started with it, or at `hooks.unregister_launch {launch_id}` → `{removed}`. Its directories are
  removed, its streams closed, its waiting posts answered 410, and `launch.ended` is published.

The **hook listener** takes `POST /hook/<launch_id>/<name>[?wait_ms=N]` with `Authorization: Bearer
<hook_token>` (`name`: 1–64 of `A-Z a-z 0-9 . - _`; a body of at most 1 MiB; at most 100 posts a
second per launch):

| Answer | When |
|---|---|
| 204 | the `hook` event was published (and a held post got no reply in time) |
| 200 + body | a held post, answered by `hooks.reply` |
| 401 | a wrong token |
| 410 | no such launch, or it ended; nothing is published |
| 413 | a body over 1 MiB |
| 429 | over the rate, or too many posts waiting (32 per launch, 256 in all) |

A post is **held** with `wait_ms` (or `hold_ms`) in the query, or a `daedalus_hold_ms` field in a JSON
object body, at most the launch's `hold_max_ms`. Its event carries `reply_id`, and
`hooks.reply {reply_id, launch_id?, status? = 200, body?, content_type?}` → `{delivered}` answers it:
a JSON string body is sent as text, anything else as JSON. A reply for a post that stopped waiting,
or whose `launch_id` differs, is `1001`. The event's `body` is the post's JSON, or its text; one over
256 KiB has its long strings shortened (every key kept) and `truncated: true`.

`ptyd hook-post <name> [--wait-ms N]` (also `$DAEDALUS_HOOK_CMD <name>`) posts its stdin with the
URL and token from its environment and prints a successful reply's body. It exits 0 on 2xx, 2 on
401 or 410, and 1 on anything else, including no listener.

### `net.dial`

`{target, launch_id}` → `{channel}`: a byte stream on its own channel. `unix:<name>` is a socket in
the launch's dial directory; `tcp:127.0.0.1:<port>` only a port registered for a live launch
(natively the loopback interface also holds the Daedalus API and the key proxy). An empty frame
from either side closes it. The daemon holds at most 1 MiB the socket has not read before it closes
the stream; the host bounds its side the same way. At most 16 streams per launch and 256 in all.

## The host side

The host (`daedalus/terminals/`) keeps a row per terminal in the `terminals` table and an append-only
`terminal_audit`. It is told each environment's run directory (`TERMINALS_CONTAINER_DIR`,
`TERMINALS_HOST_DIR`), connects in the background and retries until the daemon is there. Both
directories are sealed from the agent — named in a command, even inside a container, it is refused —
because the token in them is a shell.

- **Ids and labels.** The host makes the id (12 hex characters) and writes the row before
  `terminal.create`, so the row is the reservation under the cap. `labels` carry `owner_kind`,
  `owner_id`, `project_id` and `profile`.
- **Reconcile**, on every connection and every minute: a running row the daemon does not list is
  `lost` when the daemon's `instance` changed and `exited` when it did not (ended and forgotten
  while the host was away); a running terminal with no row is adopted from its labels; a terminal
  whose owner no longer exists is ended. Events are resumed from the last `seq` seen when the
  instance is the same.
- **Owners** are `session`, `staff`, `project` or `free`. Deleting a session or a project ends its
  terminals. Ended rows are kept for `terminals.exited_retention_hours` with their last screen.
- **The cap.** At most `terminals.running_cap` terminals run at once across both environments. An
  agent's launch waits in line for a place; the operator's is refused with `409 {"code":
  "over_cap"}` and admitted past the cap when repeated with `confirm: true`.
- **The audit** records create, kill, restart, signal, keyboard, update and remove for every
  terminal, and every agent write with its first 4 KiB and the SHA-256 of the whole. What a person
  types is never recorded.
- **Load.** The daemons' `terminal.stats` events feed a rolling average cost per profile; `GET
  /api/terminals/load?cap=N` reports what runs now and the machine with the cap filled.
- **Side channels** (`daedalus/terminals/sidechannels.py`, methods of the same service): the file
  roots of each environment are its project folders plus the directories adapters name
  (`set_extra_roots`), sent on every connection and whenever they change. `hook_events(launch_id)`
  yields a launch's posts in the daemon's order, kept from the moment the launch is registered, and
  ends with the launch. `net_dial` returns a stream bounded at 1 MiB unread. Every program run,
  launch registered or ended, port opened, stream dialled and hook answered is in the audit, with
  the launch's terminal when it is known. Natively the daemons' state directories are sealed like
  the rest of the installation.

| Route | |
|---|---|
| `GET /api/terminals?env&owner_kind&owner_id&project_id&status&preview=0..12` | `{envs, terminals, capacity}` |
| `POST /api/terminals` | `{env, owner_kind, owner_id?, project_id?, cwd?, title?, sandbox?, cols?, rows?, confirm?}` → the view (201) |
| `GET /api/terminals/load?cap=` | the cost now and the projection |
| `GET`, `PATCH`, `DELETE /api/terminals/{id}` | the view; rename or hand to another owner; remove an ended row |
| `POST /api/terminals/{id}/kill`, `/signal`, `/restart` | end; `{signal}`; the same program as a new terminal |
| `GET /api/terminals/{id}/screen`, `/audit` | the screen (with the emulator); the audit, newest first |

The browser reaches a terminal through the host: `POST /api/terminals/{id}/ticket` returns a
single-use ticket valid for 30 s, and `WS /ws/terminals/{id}?ticket=…` checks the Origin, spends the
ticket and relays frames. Close codes: 4401 ticket, 4403 origin, 4404 no terminal, 4409 environment
unavailable, 1012 terminal service restarting.
