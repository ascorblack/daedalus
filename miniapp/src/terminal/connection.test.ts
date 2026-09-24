// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api";
import { backoffDelay, ConnectionDeps, ConnectionState, SocketLike, TerminalConnection, TerminalSink } from "./connection";
import { EventMessage, FRAME } from "./protocol";

class FakeSocket implements SocketLike {
  binaryType = "blob";
  readyState = 0;
  sent: Uint8Array[] = [];
  closedWith: number | null = null;
  onopen: SocketLike["onopen"] = null;
  onmessage: SocketLike["onmessage"] = null;
  onclose: SocketLike["onclose"] = null;
  onerror: SocketLike["onerror"] = null;
  constructor(public url: string) {}
  send(data: Uint8Array) {
    this.sent.push(data);
  }
  close(code?: number) {
    this.closedWith = code ?? 1000;
    this.readyState = 3;
  }
  open() {
    this.readyState = 1;
    this.onopen?.({});
  }
  receive(bytes: Uint8Array) {
    this.onmessage?.({ data: bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) });
  }
  drop(code = 1006) {
    this.readyState = 3;
    this.onclose?.({ code });
  }
  frames(type: number) {
    return this.sent.filter((f) => f[0] === type);
  }
  attaches() {
    return this.frames(FRAME.ATTACH).map((f) => JSON.parse(new TextDecoder().decode(f.subarray(1))));
  }
  acks() {
    return this.frames(FRAME.ACK).map((f) => Number(new DataView(f.buffer, f.byteOffset).getBigUint64(1)));
  }
}

/** A sink whose parse callbacks run only when the test says so, as xterm.js's run later. */
class FakeSink implements TerminalSink {
  log: string[] = [];
  pending: (() => void)[] = [];
  auto = true;
  write(data: Uint8Array, parsed: () => void) {
    this.log.push(`write:${new TextDecoder().decode(data)}`);
    if (this.auto) parsed();
    else this.pending.push(parsed);
  }
  reset(cols: number, rows: number) {
    this.log.push(`reset:${cols}x${rows}`);
  }
  flush() {
    const callbacks = this.pending.splice(0);
    callbacks.forEach((c) => c());
  }
}

const enc = new TextEncoder();
const u64 = (n: number) => {
  const b = new Uint8Array(8);
  new DataView(b.buffer).setBigUint64(0, BigInt(n));
  return b;
};
const output = (seq: number, text: string | Uint8Array) => new Uint8Array([FRAME.OUTPUT, ...u64(seq), ...(typeof text === "string" ? enc.encode(text) : text)]);
const snapshot = (cols: number, rows: number, seq: number, text: string) => {
  const head = new Uint8Array(5);
  new DataView(head.buffer).setUint16(1, cols);
  new DataView(head.buffer).setUint16(3, rows);
  head[0] = FRAME.SNAPSHOT;
  return new Uint8Array([...head, ...u64(seq), ...enc.encode(text)]);
};
const event = (e: Record<string, unknown>) => new Uint8Array([FRAME.EVENT, ...enc.encode(JSON.stringify(e))]);
const hello = (ackBytes = 64 * 1024) =>
  event({ type: "hello", client_id: "c1", read_only: false, ack_bytes: ackBytes, window_bytes: 256 * 1024, terminal: { id: "t1", title: "", cwd: "/", status: "running", cols: 80, rows: 24 }, size: { cols: 80, rows: 24, owner: "you" }, keyboard: { owner: "auto", until: null }, modes: { alt_screen: false, mouse: false, bracketed_paste: false, app_cursor: false } });

type Harness = {
  sockets: FakeSocket[];
  states: ConnectionState[];
  events: EventMessage[];
  tickets: number;
  ticketError: Error | null;
  wake: () => void;
  sink: FakeSink;
  connection: TerminalConnection;
  last: () => FakeSocket;
};

async function settle() {
  for (let i = 0; i < 5; i++) await Promise.resolve();
}

function start(options: { readOnly?: boolean; ticketError?: Error; resumeSeq?: number } = {}): Harness {
  const h = { sockets: [], states: [], events: [], tickets: 0, ticketError: options.ticketError ?? null, wake: () => undefined } as unknown as Harness;
  const deps: ConnectionDeps = {
    ticket: async () => {
      h.tickets++;
      if (h.ticketError) throw h.ticketError;
      return { ticket: `tk${h.tickets}` };
    },
    socket: (url) => {
      const s = new FakeSocket(url);
      h.sockets.push(s);
      return s;
    },
    base: () => "wss://app.example",
    wake: (callback) => {
      h.wake = callback;
      return () => undefined;
    },
    random: () => 0.5,
  };
  h.sink = new FakeSink();
  h.last = () => h.sockets[h.sockets.length - 1];
  h.connection = new TerminalConnection(h.sink, { id: "t1", readOnly: options.readOnly, resumeSeq: options.resumeSeq, onState: (s) => h.states.push(s), onEvent: (e) => h.events.push(e) }, deps);
  return h;
}

async function live(h: Harness) {
  await settle();
  h.last().open();
  h.last().receive(hello());
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the terminal connection", () => {
  it("takes over a terminal that already holds the stream, asking for the tail after it", async () => {
    // A terminal shown again after its socket was closed still has its screen; a snapshot would
    // reset it for nothing, and its reset would wipe the scrollback the reader was looking at.
    const h = start({ resumeSeq: 4096 });
    await settle();
    h.last().open();
    expect(h.last().attaches()).toEqual([{ lastSeq: 4096, haveState: true, readOnly: false }]);
    h.last().receive(hello(4));
    h.last().receive(output(4090, "0123456789"));
    // The overlap with what it already had is trimmed, and acknowledgements count from the resume point.
    expect(h.sink.log).toEqual(["write:6789"]);
    expect(h.last().acks()).toEqual([4100]);
  });

  it("opens the socket with the ticket and attaches with nothing to resume", async () => {
    const h = start();
    await settle();
    expect(h.last().url).toBe("wss://app.example/ws/terminals/t1?ticket=tk1");
    expect(h.last().binaryType).toBe("arraybuffer");
    h.last().open();
    expect(h.last().attaches()).toEqual([{ lastSeq: 0, haveState: false, readOnly: false }]);
    h.last().receive(hello());
    expect(h.connection.current).toEqual({ kind: "live" });
  });

  it("backs off from half a second to ten, with jitter around it", async () => {
    expect(backoffDelay(0, () => 0.5)).toBe(500);
    expect(backoffDelay(0, () => 0)).toBe(400);
    expect(backoffDelay(0, () => 1)).toBe(600);
    expect(backoffDelay(3, () => 0.5)).toBe(4000);
    expect(backoffDelay(10, () => 0.5)).toBe(10_000);
    const h = start({ ticketError: new TypeError("network") });
    const delays: number[] = [];
    for (let i = 0; i < 7; i++) {
      await settle();
      const state = h.connection.current;
      expect(state.kind).toBe("reconnecting");
      if (state.kind === "reconnecting") delays.push(state.delayMs);
      await vi.advanceTimersByTimeAsync(state.kind === "reconnecting" ? state.delayMs : 0);
    }
    expect(delays).toEqual([500, 1000, 2000, 4000, 8000, 10_000, 10_000]);
  });

  it("stops waiting and reconnects when the page becomes visible or comes online", async () => {
    const h = start();
    await live(h);
    h.last().drop();
    expect(h.connection.current.kind).toBe("reconnecting");
    expect(h.sockets.length).toBe(1);
    h.wake();
    await settle();
    expect(h.sockets.length).toBe(2);
  });

  it("replaces a socket that went silent while the page slept", async () => {
    const h = start();
    await live(h);
    await vi.advanceTimersByTimeAsync(26_000);
    // The keepalive and the watchdog are timers too; with the page asleep neither ran, so do as a
    // woken page does: the clock has moved and nothing arrived.
    h.wake();
    await settle();
    expect(h.sockets.length).toBe(2);
  });

  it("reconnects when nothing arrives for 45 seconds, and not while pings keep coming", async () => {
    const h = start();
    await live(h);
    for (let i = 0; i < 5; i++) {
      await vi.advanceTimersByTimeAsync(20_000);
      h.last().receive(event({ type: "ping", at: i }));
    }
    expect(h.sockets.length).toBe(1);
    await vi.advanceTimersByTimeAsync(50_000);
    await settle();
    expect(h.sockets.length).toBe(2);
    expect(h.sockets[0].closedWith).toBe(1000);
  });

  it("acknowledges parsed bytes every ack_bytes, and every 20 seconds regardless", async () => {
    const h = start();
    await settle();
    h.last().open();
    h.last().receive(hello(100));
    h.last().receive(snapshot(80, 24, 1000, "x"));
    expect(h.last().acks()).toEqual([1000]);
    h.last().receive(output(1000, "a".repeat(60)));
    expect(h.last().acks()).toEqual([1000]);
    h.last().receive(output(1060, "b".repeat(60)));
    expect(h.last().acks()).toEqual([1000, 1120]);
    h.last().receive(output(1120, "c".repeat(10)));
    await vi.advanceTimersByTimeAsync(20_000);
    expect(h.last().acks()).toEqual([1000, 1120, 1130]);
  });

  it("counts an acknowledgement only once xterm.js has parsed the bytes", async () => {
    const h = start();
    await settle();
    h.last().open();
    h.last().receive(hello(10));
    h.sink.auto = false;
    h.last().receive(output(0, "0123456789abcdef"));
    expect(h.last().acks()).toEqual([]);
    h.sink.flush();
    expect(h.last().acks()).toEqual([16]);
  });

  it("asks for the tail after the last byte it was given when it reconnects", async () => {
    const h = start();
    await live(h);
    h.last().receive(snapshot(80, 24, 500, "screen"));
    h.last().receive(output(500, "hello"));
    h.last().drop();
    await vi.advanceTimersByTimeAsync(600);
    h.last().open();
    expect(h.last().attaches()).toEqual([{ lastSeq: 505, haveState: true, readOnly: false }]);
    // The daemon resends from an earlier offset; the overlap is not written twice.
    h.last().receive(output(503, "lo world"));
    expect(h.sink.log).toEqual(["reset:80x24", "write:screen", "write:hello", "write: world"]);
  });

  it("takes a snapshot after a full reset, and only once earlier writes are parsed", async () => {
    const h = start();
    await live(h);
    h.sink.auto = false;
    h.last().receive(output(0, "old"));
    h.last().receive(event({ type: "resync", reason: "resized", first_abs_row: 0 }));
    h.last().receive(snapshot(100, 30, 900, "new"));
    h.last().receive(output(900, "after"));
    expect(h.sink.log).toEqual(["write:old"]);
    h.sink.flush();
    expect(h.sink.log).toEqual(["write:old", "reset:100x30", "write:new", "write:after"]);
  });

  it("starts over from a snapshot when the stream has a hole", async () => {
    const h = start();
    await live(h);
    h.last().receive(output(0, "abc"));
    h.last().receive(output(10, "zzz"));
    await settle();
    expect(h.sockets.length).toBe(2);
    h.last().open();
    expect(h.last().attaches()).toEqual([{ lastSeq: 0, haveState: false, readOnly: false }]);
    expect(h.sink.log).toEqual(["write:abc"]);
  });

  it("says a reverse proxy is in the way when tickets work and sockets never open", async () => {
    const h = start();
    for (let i = 0; i < 3; i++) {
      await settle();
      h.last().drop(1006);
      if (i < 2) expect(h.connection.current.kind).toBe("reconnecting");
      await vi.advanceTimersByTimeAsync(10_000);
    }
    expect(h.states.some((s) => s.kind === "proxy-blocked")).toBe(true);
    expect(h.tickets).toBeGreaterThanOrEqual(3);
  });

  it("does not blame a proxy for a socket that opened and later dropped", async () => {
    const h = start();
    for (let i = 0; i < 4; i++) {
      await settle();
      h.last().open();
      h.last().drop(1006);
      await vi.advanceTimersByTimeAsync(10_000);
    }
    expect(h.states.some((s) => s.kind === "proxy-blocked")).toBe(false);
  });

  it("stays on an exited terminal and does not reconnect when its socket closes", async () => {
    const h = start();
    await live(h);
    h.last().receive(event({ type: "exit", code: 2, signal: null }));
    expect(h.connection.current).toEqual({ kind: "exited", code: 2, signal: null });
    h.last().drop(1000);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(h.sockets.length).toBe(1);
  });

  it("gives up on a terminal that is gone, and retries at once on a stale ticket", async () => {
    const gone = start();
    await live(gone);
    gone.last().drop(4404);
    expect(gone.connection.current).toEqual({ kind: "unavailable", reason: "gone" });
    await vi.advanceTimersByTimeAsync(30_000);
    expect(gone.sockets.length).toBe(1);

    const stale = start();
    await settle();
    stale.last().drop(4401);
    await settle();
    expect(stale.sockets.length).toBe(2);

    const missing = start({ ticketError: new ApiError(404, "no such terminal") });
    await settle();
    expect(missing.connection.current).toEqual({ kind: "unavailable", reason: "gone" });
  });

  it("keeps retrying an environment that is down, and says so", async () => {
    const h = start();
    await live(h);
    h.last().drop(4409);
    expect(h.connection.current).toEqual({ kind: "unavailable", reason: "environment" });
    await vi.advanceTimersByTimeAsync(600);
    h.last().open();
    h.last().receive(hello());
    expect(h.connection.current).toEqual({ kind: "live" });
  });

  it("sends input only while live and never from a read-only view", async () => {
    const h = start();
    await settle();
    expect(h.connection.input("x")).toBe(false);
    h.last().open();
    h.last().receive(hello());
    expect(h.connection.input("ls\r")).toBe(true);
    expect(h.last().frames(FRAME.INPUT).map((f) => new TextDecoder().decode(f.subarray(1)))).toEqual(["ls\r"]);

    const viewer = start({ readOnly: true });
    await live(viewer);
    expect(viewer.connection.input("x")).toBe(false);
    expect(viewer.last().attaches()[0].readOnly).toBe(true);
  });

  it("closes for good: no timers, no reconnect", async () => {
    const h = start();
    await live(h);
    h.connection.close();
    expect(h.sockets[0].closedWith).toBe(1000);
    await vi.advanceTimersByTimeAsync(120_000);
    h.wake();
    await settle();
    expect(h.sockets.length).toBe(1);
  });
});
