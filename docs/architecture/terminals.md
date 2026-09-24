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
| `--state-dir` | the daemon's log, the journal of agent writes, terminal logs (required) |
| `--listen` | `unix` (a socket in the run directory), or `tcp:127.0.0.1:<port>` |
| `--home` | the home directory of spawned programs (`$HOME`) |
| `--shell` | the login shell: `$SHELL`, then the user's passwd entry, then `/bin/bash`, then `/bin/sh` |
| `--hooks-listen` | loopback address of the hook listener (*not yet*) |
| `--config` | a JSON file of limits: `{"limits": {"max_terminals", "ring_bytes", "input_idle_ms", "kill_grace_ms"}}`; unknown keys are refused |
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
| 1008 | `stale_launch` | |
| 1009 | `invalid_size` | outside 20×4 … 500×300 |

Parameters are decoded strictly: an unknown field is `-32602`.

## Methods

| Method | Params → result | |
|---|---|---|
| `daemon.info` | → `{version, protocol, instance, env, os, arch, pid, started_at, uptime_s, home, shell, capabilities{sandbox, shells[], shell_integration[], emulator, stats}, hooks{listen}, limits{…}, counts{running, exited}, machine}` | |
| `terminal.create` | see below → `{id, pid, cwd, cwd_fallback, shell, created_at}` | |
| `terminal.list` | `{ids?, preview_rows? 0..12}` → `{terminals:[Info]}` | previews *not yet* |
| `terminal.get` | `{id}` → `Info` | |
| `terminal.write` | see below → `{bytes, seq_before, queued_ms, delivered_at}` | |
| `terminal.resize` | `{id, cols, rows, px_w?, px_h?}` → `{cols, rows}`; the host becomes the size owner | |
| `terminal.signal` | `{id, signal, group? = true}` | |
| `terminal.kill` | `{id, grace_ms? ≤ 30000}` → `{exit_code, signal}` | |
| `terminal.forget` | `{id}`, exited terminals only | |
| `terminal.read_output` | `{id, since_seq, max_bytes? ≤ 1 MiB, strip?}` → `{from_seq, to_seq, head_seq, gap, data \| data_b64}` | |
| `terminal.stats` | `{ids?}` → `{at, supported, terminals:[{id, pid, processes, rss_bytes, cpu_percent, daemon_bytes}], daemon{pid, rss_bytes, cpu_percent}, machine}` | |
| `events.subscribe` | `{after_seq}` → `{instance, from_seq, resync}`, then `event` notifications | |
| `events.unsubscribe` | | |
| `terminal.attach` | `{id, client{kind? = "human" \| "viewer", label?, via?, read_only?}}` → `{channel, client_id}` | see Attachments |
| `terminal.detach` | `{channel}`; the daemon closes the channel | |
| `terminal.keyboard` | `{id, owner: "auto" \| "human" \| "agent", ttl_ms? ≤ 86 400 000}` → `{owner, until?}` | |
| `terminal.snapshot`, `terminal.read_screen`, `terminal.wait_for`, `terminal.commands` | the screen | *not yet* |
| `exec.run`, `fs.*`, `net.dial`, `net.allow`, `hooks.*` | side channels for CLI adapters | *not yet* |

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
command is running. `clients` are `{id, kind, label, via?, read_only, attached_at}`; `size_owner` is
`"host"`, `"human:<client id>"`, or null once the owning client left and no other offered a size. `cwd` and `title` follow OSC 7 and OSC 0/2. `output_seq` is one past the last
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
terminal, the processes of its tree, its session and every process whose environment carries its
`DAEDALUS_TERMINAL_ID` (a daemon that forked twice and called `setsid` has left both of the others),
their summed resident memory, and their CPU use since the previous sample as a percentage of one CPU.
Shared pages are counted once per process, so the memory is an estimate of cost, not an accounting.
`daemon_bytes` is what the terminal holds inside the daemon that none of its processes shows: its
output ring as allocated now. `daemon` is the daemon's own process, whose memory holds every
terminal's emulator; a load estimate adds it, shared out over the terminals. `machine` is
`{mem_total_bytes, mem_available_bytes, cgroup_limit_bytes?, cgroup_used_bytes?, cpus, cgroup_cpus?,
cpu_percent, load1, load5, load15}`, read from `/proc` and, inside a container, from its cgroup's
memory and CPU limits. Outside Linux `supported` is false and the numbers are zero.

## Events

An `event` notification carries `{seq, at, type, terminal_id?, data}`. Every event takes its `seq`
from one counter, so a subscriber sees one total order across terminals and kinds. The daemon keeps
the last 20 000.

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

`seq` inside `data` is the output offset at which the mark occurred.

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

## Attachments

`terminal.attach` opens a channel that carries browser frames both ways; the host relays them to a
WebSocket unchanged. The daemon sends nothing on it until the client's first ATTACH. `read_only` (or
`kind: "viewer"`, or `readOnly` in ATTACH, whichever says so) makes the client a watcher: its INPUT
and RESIZE are dropped. A terminal takes at most 32 clients (`1003` past that). An exited terminal can
still be attached, to see its last screen. `terminal.detach`, the host closing the channel, the
connection ending and the terminal being forgotten all end the client; the channel's closing frame is
always its last.

**On ATTACH** the client receives `hello`, then `clients`, then either the bytes it is missing or a
fresh screen. `hello` is `{client_id, read_only, ack_bytes, window_bytes, terminal{id, title, cwd,
status, cols, rows}, size{cols, rows, owner}, keyboard{owner, until}, modes{alt_screen, mouse,
bracketed_paste, app_cursor}}`; `clients` is `{count, others[]}`, sent again whenever someone attaches
or leaves.

- OUTPUT from `lastSeq` when `haveState` is set, `lastSeq` is still in the ring, the PTY was not
  resized after `lastSeq`, and at most 1 MiB is missing. Bytes drawn for another size would be
  garbage on the client's screen, and replaying more than a megabyte costs more than one screen.
- Otherwise `resync {reason, first_abs_row}` and a SNAPSHOT at the emulator's size, with
  `min(scrollback, 10000)` lines of history (2000 by default), and OUTPUT from the snapshot's `seq`.
  `reason` is `attach` (no state), `ring` (`lastSeq` left the ring, or is ahead of it), `resized` or
  `backlog`. A screen that does not fit in one frame is taken again with half the history; if even the
  bare screen does not fit, an empty one is sent at the right offset, followed by
  `error {code: "snapshot_too_large"}`.
- The client resets its terminal before writing a snapshot, so modes of the old screen cannot survive.
- For an exited terminal, then `exit {code, signal}` once every byte is sent. The channel stays open.

A second ATTACH on the same channel starts over the same way.

**Flow control.** OUTPUT goes out when 64 KiB are waiting or 8 ms after the previous frame, so a
keystroke's echo after a quiet spell is sent at once and a flood in large frames. The daemon stops
sending once 256 KiB are unacknowledged (`window_bytes`); the client acknowledges every 64 KiB it has
*parsed* (`ack_bytes`), and at least every 20 s. An acknowledgement past what was sent counts as
everything sent. When an acknowledgement reopens a full window and what the client missed has left the
ring or is more than 1 MiB, the client gets `resync` (`lagged`) and a snapshot instead of the backlog;
the window starts again from the snapshot. The PTY reader never waits for a client: a client that
stops acknowledging holds its window and nothing else. `ping {at}` (milliseconds) comes every 20 s.

**Size.** Each client's RESIZE is the size it would like. The client that last resized or typed owns
the PTY's size; the others render at it. A RESIZE, or INPUT from a client with a size of its own
while another owns the size, applies that client's size before the keystroke is written. When the
owner leaves, the most recently active client with a size takes over; with none, the PTY keeps its
size and the owner is nobody. `terminal.resize` from the host makes the host the owner until a client
claims. A size outside 20×4 … 500×300 is refused with `error {code: "invalid_size"}` and not
remembered. A read-only client never owns the size. Every change of the grid or the owner is sent as
`size {cols, rows, owner}`, with `owner` `you`, `other` or `host` as the receiving client sees it.

**The keyboard.** `auto` is the default: an agent write waits for `input_idle_ms` of human quiet.
`human` holds agents back until the grant's expiry (or until changed); `agent` lets their writes
through at once, and human typing ends it. Clients receive `keyboard {owner, until}` (`until` in
milliseconds, or null) at every change and when a grant expires, and `agent_typing {actor, active}`
when an agent's writes start being delivered and after 1.5 s without one.

**Terminal queries.** The daemon is their only answerer; clients swallow them. An INPUT frame that
repeats byte for byte an answer the daemon gave to a query that client was shown, within 2 s, is
taken for the client answering too and dropped once; the same bytes later are typing and pass.

**Other events**, as the terminal produces them: `title`, `cwd`, `bell`, `notify {title, body}`,
`progress {state, value}` and `mode {alt_screen, mouse, bracketed_paste, app_cursor}`. A client that
is behind receives only the latest of each kind, except `notify`, of which at most 64 wait. A frame the
daemon cannot use is answered with `error {code: "bad_frame"}`.

## The environment of a spawned program

The daemon's environment, minus `DAEDALUS_PTYD_*`, `DAEDALUS_TERMINAL_ID`, `DAEDALUS_LAUNCH_*`,
`DAEDALUS_HOOK_*`, `DAEDALUS_DIAL_DIR`, `TERM_PROGRAM*`, `VSCODE_*`, `TMUX*`, `STY`, `WINDOW`,
`KITTY_*`, `ITERM_*`, `WT_SESSION`, `CLAUDE*` and the caller's `strip_env` patterns (a name, or a
prefix ending in `*`); plus `TERM=xterm-256color`, `COLORTERM=truecolor`, `CLAUDE_CODE_NO_FLICKER=1`,
`CLAUDE_CODE_SCROLL_SPEED=3`, `DAEDALUS_TERMINAL_ID=<id>` and `HOME`; plus the caller's `env` on top.
When the effective character type (the first of `LC_ALL`, `LC_CTYPE`, `LANG` that is set) is not
UTF-8, the non-UTF-8 overrides are removed and `LANG=C.UTF-8` is set; a user's `ru_RU.UTF-8` is
kept.

## The host side

The browser reaches a terminal through the host: `POST /api/terminals/{id}/ticket` returns a
single-use ticket valid for 30 s, and `WS /ws/terminals/{id}?ticket=…` checks the Origin, spends the
ticket and relays frames. Close codes: 4401 ticket, 4403 origin, 4404 no terminal, 4409 environment
unavailable, 1012 terminal service restarting.
