# ptyd

The terminal daemon. It owns pseudo-terminals — spawning a login shell or an exact argv, resizing,
signalling, ending the whole process tree — and keeps, for each one, its output (addressed by byte
offset), a headless emulator and a scanner that finds titles, shell marks, notifications and mode
changes in the raw stream. It clamps what a hostile program could use to exhaust any emulator that
reads the same bytes. The Daedalus host reaches it over an authenticated local socket; the daemon
knows nothing about sessions, projects or agents.

```sh
go build ./cmd/ptyd
./ptyd serve --env container --run-dir /tmp/ptyd-run --state-dir /tmp/ptyd-state
./ptyd version
```

The protocol, the run directory, the events and the guarantees about the output are specified in
[`docs/architecture/terminals.md`](../docs/architecture/terminals.md).

## Layout

| Package | What it is |
|---|---|
| `cmd/ptyd` | flags, signals, shutdown |
| `internal/server` | the run directory, the token handshake, channels, the JSON-RPC dispatcher; `clienttest` is a client for tests |
| `internal/rpc` | the methods |
| `internal/term` | one terminal (reader, emulator goroutine, input arbitration, keys, environment) and the registry |
| `internal/scan` | the clamps and the marks, over the raw output |
| `internal/ring` | the output ring, by absolute offset |
| `internal/emulator` | the emulator interface; `basic` (modes only) and `fake` (for tests) |
| `internal/ptyproc` | PTY start, resize, signals, ending a process tree |
| `internal/procstat` | the process table and the machine's memory and CPU, from `/proc` |
| `internal/events` | the ordered event log and its per-terminal rate limits |
| `internal/wire` | frame codecs; `testdata/frames.json` is shared byte for byte with the app |
| `internal/logx` | the log and the journal of agent writes |

## Testing

The gate runs in the Go image named by `go.mod`, with the race detector and a vet for macOS:

```sh
docker run --rm -v "$PWD":/src -w /src -e HOME=/tmp -e GOCACHE=/tmp/gocache -e GOFLAGS=-buildvcs=false \
  golang:1.26 sh -c 'test -z "$(gofmt -l .)" && go vet ./... && go test -race ./... && GOOS=darwin go vet ./...'
```

The tests start real PTYs. Fuzzers (`FuzzScan`, `FuzzFrame`) and `TestProbeCorpus` (a directory of
hostile output named by `PTYD_PROBES_DIR`) are for runs under a memory cap.
