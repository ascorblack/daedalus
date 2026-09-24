# ptyd

The terminal daemon. It owns pseudo-terminals — spawning a login shell or an exact argv, resizing,
signalling, ending the whole process tree — and keeps, for each one, its output (addressed by byte
offset), a headless emulator and a scanner that finds titles, shell marks, notifications and mode
changes in the raw stream. It clamps what a hostile program could use to exhaust any emulator that
reads the same bytes. The Daedalus host reaches it over an authenticated local socket; the daemon
knows nothing about sessions, projects or agents.

```sh
export PKG_CONFIG_PATH="$(libghostty/build.sh)"   # the screen emulator, once (see below)
go build ./cmd/ptyd
./ptyd serve --env container --run-dir /tmp/ptyd-run --state-dir /tmp/ptyd-state
./ptyd version
./ptyd hook-post Stop < body.json   # inside a launch: post a hook, print the reply
./ptyd hook Stop < body.json        # a CLI's command hook: the same, and always exit 0
./ptyd team-mcp                     # inside a launch: the team tools' MCP server on stdio
```

The protocol, the run directory, the events and the guarantees about the output are specified in
[`docs/architecture/terminals.md`](../docs/architecture/terminals.md).

## Layout

| Package | What it is |
|---|---|
| `cmd/ptyd` | flags, signals, shutdown |
| `internal/server` | the run directory, the token handshake, channels, the JSON-RPC dispatcher; `clienttest` is a client for tests |
| `internal/rpc` | the methods |
| `internal/term` | one terminal (reader, emulator goroutine, input arbitration, keys, environment), its attachments (flow control, size ownership, the keyboard), and the registry |
| `internal/scan` | the clamps and the marks, over the raw output |
| `internal/ring` | the output ring, by absolute offset |
| `internal/emulator` | the emulator interface; `ghostty` (the screen emulator), `basic` (modes only, for builds without cgo), `fake` (for tests), `production` (which of them a build runs), `conformance` (the behaviour every one must have) |
| `internal/answer` | the answers to terminal queries, as xterm.js gives them; `testdata/xterm-replies.json` is recorded from xterm.js |
| `cmd/ptyd-replay` | plays a terminal recording through the emulator and writes its snapshots, for the cross-check against xterm.js |
| `libghostty` | the pinned sources of the screen emulator, the patch carried against them, and the script that builds it |
| `internal/ptyproc` | PTY start, resize, signals, ending a process tree |
| `internal/procstat` | the process table and the machine's memory and CPU, from `/proc` |
| `internal/events` | the ordered event log and its per-terminal rate limits |
| `internal/sidechan` | `exec.run` and its program list, `fs.*` with its roots and deny list, `net.dial` |
| `internal/hooks` | launches (token, overlay files, dial directory, ports), the loopback hook listener with held replies, `hook-post` and `hook` |
| `internal/teammcp` | `team-mcp`: the team tools (`Report`, `AskOrchestrator`) as an MCP server on stdio, posting to the hook listener |
| `internal/wire` | frame codecs; `testdata/frames.json` is shared byte for byte with the app |
| `internal/logx` | the log and the journal of agent writes |

## The screen emulator

The screen is libghostty-vt, the terminal core of Ghostty, linked statically through its Go bindings
(`go.mitchellh.com/libghostty`, pinned in `go.mod`). The library itself is built from source by
`libghostty/build.sh`, from what `libghostty/pins.env` names and nothing else:

- Ghostty at one commit, fetched by that commit and checked against it, with `libghostty/patches/`
  applied (fixes to its VT formatter that are meant for upstream);
- the Zig that commit needs, fetched from ziglang.org and checked against its published SHA-256;
- Zig's own dependencies, which Ghostty's `build.zig.zon` pins by hash.

Everything lands in `.cache/` (or `PTYD_CACHE`); a second run with the same pins and patches builds
nothing and prints the same directory. The script targets the machine's architecture at its baseline
CPU; `LIBGHOSTTY_TARGET` picks another Zig target. A build without cgo still works, with the
modes-only emulator, and says so in `daemon.info` (`capabilities.emulator`).

To move to a newer Ghostty or bindings: change both pins together, rebuild, run the gate below, and
re-record the width grids the app shares (`internal/emulator/ghostty/testdata/ghostty-widths.json`)
and the golden snapshots (`go test ./internal/emulator/ghostty -run Golden -update`, then review the
diff).

## Testing

The gate runs in the Go image named by `go.mod` plus `xz` (`libghostty/toolchain.Dockerfile`), builds
the library, and runs the tests with the race detector and a vet for macOS. The formatting check names
the source directories, because `.cache` holds other modules' sources. The container is capped
at 6 GB, as every build of the library should be:

```sh
docker build -q -t ptyd-toolchain -f libghostty/toolchain.Dockerfile libghostty
docker run --rm --memory 6g --memory-swap 6g --user "$(id -u):$(id -g)" -v "$PWD":/src -w /src \
  -e HOME=/tmp -e GOCACHE=/src/.cache/go-build -e GOMODCACHE=/src/.cache/go-mod -e GOFLAGS=-buildvcs=false \
  ptyd-toolchain sh -c 'export PKG_CONFIG_PATH="$(libghostty/build.sh)" && test -z "$(gofmt -l cmd internal)" &&
    go vet ./... && go test -race ./... && GOOS=darwin go vet ./...'
```

The tests start real PTYs. Fuzzers (`FuzzScan`, `FuzzFrame`) and `TestProbeCorpus` (a directory of
hostile output named by `PTYD_PROBES_DIR`) are for runs under a memory cap. `BenchmarkThroughput`
(program → PTY → daemon) is measured against `BenchmarkPTYAlone` (the same program and PTY, output
thrown away); `PTYD_BENCH_SHORT_LINES=1` runs both on `yes`, the worst case per byte.
