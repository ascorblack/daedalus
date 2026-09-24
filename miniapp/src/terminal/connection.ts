// One terminal's live connection: ticket, WebSocket, attach, the output stream into the terminal,
// acknowledgements, and getting back after anything goes wrong.
//
// The rules it keeps, each one a failure someone has already had:
// - The terminal is never cleared and replayed. A reconnect asks for the tail after the last byte this
//   terminal was given (`haveState`); only when the daemon cannot give one (the ring moved on, or the
//   PTY was resized since) does a snapshot come, and a snapshot is written after a full reset so the
//   modes of the old screen (mouse reporting, bracketed paste) cannot leak into the new one.
// - A snapshot waits for every earlier write to be parsed. xterm.js queues writes, and a reset applied
//   while old bytes are still queued would have those bytes land on the fresh screen.
// - Acknowledgements count bytes xterm.js has *parsed*, from its write callback, not bytes received:
//   the daemon's window is what keeps a flood from piling up in the browser.
// - A socket that is silent for 45 s is dead even if the browser has not noticed (a laptop lid, a
//   mobile network change); the daemon pings every 20 s, so silence is a reliable signal.
// - When tickets keep succeeding and the socket keeps failing, the host is fine and something between
//   (a reverse proxy without WebSocket upgrades) is not; that gets its own state and its own hint.

import { api, ApiError } from "../api";
import {
  AttachRequest,
  decodeServerFrame,
  encodeAck,
  encodeAttach,
  encodeInput,
  encodeResize,
  EventMessage,
  ProtocolError,
  ServerFrame,
} from "./protocol";

export type ConnectionState =
  | { kind: "connecting" }
  | { kind: "live" }
  | { kind: "reconnecting"; attempt: number; delayMs: number }
  | { kind: "exited"; code: number | null; signal: string | null }
  | { kind: "unavailable"; reason: UnavailableReason }
  | { kind: "proxy-blocked" };

/** Why a terminal cannot be reached: it is gone, its environment is down, or the page is not allowed. */
export type UnavailableReason = "gone" | "environment" | "origin" | "auth";

/** Where the stream goes: an xterm.js terminal, or a fake in tests. */
export interface TerminalSink {
  /** Write bytes; `parsed` is called once the terminal has parsed them. */
  write(data: Uint8Array, parsed: () => void): void;
  /** Reset every mode and the buffer, then take this size. Called only when no write is pending. */
  reset(cols: number, rows: number): void;
}

/** The subset of `WebSocket` the connection uses. */
export interface SocketLike {
  binaryType: string;
  readonly readyState: number;
  send(data: Uint8Array): void;
  close(code?: number, reason?: string): void;
  onopen: ((event: unknown) => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
  onclose: ((event: { code: number; reason?: string }) => void) | null;
  onerror: ((event: unknown) => void) | null;
}

export type ConnectionDeps = {
  ticket: (id: string, readOnly: boolean) => Promise<{ ticket: string }>;
  socket: (url: string) => SocketLike;
  /** The page's origin as `ws:`/`wss:` plus host. */
  base: () => string;
  /** Calls back when the page becomes visible or the browser comes back online; returns the unsubscribe. */
  wake: (callback: () => void) => () => void;
  random: () => number;
};

export type ConnectionOptions = {
  id: string;
  readOnly?: boolean;
  scrollback?: number;
  theme?: () => AttachRequest["theme"];
  /**
   * The stream offset the terminal already holds, when a new connection takes over a terminal whose
   * previous connection was closed (a detached terminal put to sleep and shown again). The attach then
   * asks for the tail after it instead of a snapshot, which would reset a screen that is still right.
   */
  resumeSeq?: number | null;
  onState?: (state: ConnectionState) => void;
  onEvent?: (event: EventMessage) => void;
};

export const BACKOFF_MIN_MS = 500;
export const BACKOFF_MAX_MS = 10_000;
export const WATCHDOG_MS = 45_000;
export const KEEPALIVE_MS = 20_000;
/** Consecutive socket failures behind a good ticket before blaming something in between. */
export const PROXY_FAILURES = 3;
/** Until the daemon's `hello` says otherwise. */
const DEFAULT_ACK_BYTES = 64 * 1024;

const OPEN = 1;

/** The sink for a real xterm.js terminal. */
export function xtermSink(term: { write(data: Uint8Array, callback?: () => void): void; reset(): void; resize(cols: number, rows: number): void }): TerminalSink {
  return {
    write: (data, parsed) => term.write(data, parsed),
    reset: (cols, rows) => {
      term.reset();
      term.resize(cols, rows);
    },
  };
}

/** The real browser: `fetch` for the ticket, `WebSocket`, `visibilitychange` and `online`. */
export function browserDeps(): ConnectionDeps {
  return {
    ticket: (id, readOnly) => api.terminalTicket(id, readOnly),
    socket: (url) => new WebSocket(url) as unknown as SocketLike,
    base: () => `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}`,
    wake: (callback) => {
      const visible = () => {
        if (document.visibilityState === "visible") callback();
      };
      document.addEventListener("visibilitychange", visible);
      window.addEventListener("online", callback);
      return () => {
        document.removeEventListener("visibilitychange", visible);
        window.removeEventListener("online", callback);
      };
    },
    random: Math.random,
  };
}

/** The delay before reconnect attempt `attempt` (0-based): doubling from 0.5 s to 10 s, ± 20 %. */
export function backoffDelay(attempt: number, random: () => number): number {
  const base = Math.min(BACKOFF_MAX_MS, BACKOFF_MIN_MS * 2 ** attempt);
  return Math.round(base * (0.8 + 0.4 * random()));
}

type Pending = { kind: "write"; data: Uint8Array; end: number } | { kind: "snapshot"; frame: Extract<ServerFrame, { kind: "snapshot" }> };

export class TerminalConnection {
  private state: ConnectionState = { kind: "connecting" };
  private socket: SocketLike | null = null;
  private attempt = 0;
  private proxyFailures = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private watchdog: ReturnType<typeof setInterval> | null = null;
  private keepalive: ReturnType<typeof setInterval> | null = null;
  private lastFrameAt = 0;
  private closed = false;
  private unwake: () => void;
  /** Offset after the last byte handed to the sink; null until the first snapshot or output. */
  private receivedSeq: number | null = null;
  /** Offset after the last byte the sink has parsed. */
  private parsedSeq = 0;
  private ackedSeq = 0;
  private ackBytes = DEFAULT_ACK_BYTES;
  /** Writes handed to the sink and not yet parsed; a snapshot waits for zero. */
  private inFlight = 0;
  private deferred: Pending[] = [];
  private generation = 0;

  constructor(private readonly sink: TerminalSink, private readonly options: ConnectionOptions, private readonly deps: ConnectionDeps = browserDeps()) {
    if (options.resumeSeq !== undefined && options.resumeSeq !== null && Number.isSafeInteger(options.resumeSeq) && options.resumeSeq >= 0) {
      this.receivedSeq = this.parsedSeq = this.ackedSeq = options.resumeSeq;
    }
    this.unwake = deps.wake(() => this.wake());
    void this.connect();
  }

  get current(): ConnectionState {
    return this.state;
  }

  /** Offset after the last byte handed to the terminal: the `lastSeq` of the next attach. */
  get seq(): number {
    return this.receivedSeq ?? 0;
  }

  /** Send typed or pasted input. False when there is nowhere to send it (not live, or read-only). */
  input(data: string | Uint8Array): boolean {
    if (this.options.readOnly || this.state.kind !== "live" || !this.isOpen()) return false;
    for (const frame of encodeInput(data)) this.socket!.send(frame);
    return true;
  }

  /** Send a size. The caller (`fit.ts`) decides whether this terminal may send one at all. */
  resize(cols: number, rows: number, pixelWidth = 0, pixelHeight = 0): boolean {
    if (!this.isOpen()) return false;
    this.socket!.send(encodeResize(cols, rows, pixelWidth, pixelHeight));
    return true;
  }

  /** Close for good: no reconnect, no timers. The terminal keeps what it shows. */
  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.unwake();
    this.clearTimers();
    this.dropSocket();
  }

  private isOpen(): boolean {
    return !!this.socket && this.socket.readyState === OPEN;
  }

  private setState(state: ConnectionState): void {
    this.state = state;
    this.options.onState?.(state);
  }

  private async connect(): Promise<void> {
    if (this.closed) return;
    const generation = ++this.generation;
    if (this.state.kind !== "proxy-blocked" && this.state.kind !== "unavailable") this.setState(this.attempt ? { kind: "reconnecting", attempt: this.attempt, delayMs: 0 } : { kind: "connecting" });
    let ticket: string;
    try {
      ticket = (await this.deps.ticket(this.options.id, !!this.options.readOnly)).ticket;
    } catch (error) {
      if (generation !== this.generation || this.closed) return;
      if (error instanceof ApiError && error.status === 404) return this.fail("gone");
      if (error instanceof ApiError && (error.status === 401 || error.status === 403)) return this.fail("auth");
      if (error instanceof ApiError && error.status === 409) this.setState({ kind: "unavailable", reason: "environment" });
      return this.retry();
    }
    if (generation !== this.generation || this.closed) return;
    this.open(`${this.deps.base()}/ws/terminals/${encodeURIComponent(this.options.id)}?ticket=${encodeURIComponent(ticket)}`, generation);
  }

  private open(url: string, generation: number): void {
    let socket: SocketLike;
    try {
      socket = this.deps.socket(url);
    } catch {
      this.proxyFailures++;
      return this.retry();
    }
    socket.binaryType = "arraybuffer";
    this.socket = socket;
    let opened = false;
    socket.onopen = () => {
      if (generation !== this.generation) return;
      opened = true;
      this.proxyFailures = 0;
      this.lastFrameAt = Date.now();
      const request: AttachRequest = {
        lastSeq: this.receivedSeq ?? 0,
        haveState: this.receivedSeq !== null,
        readOnly: !!this.options.readOnly,
      };
      if (this.options.scrollback !== undefined) request.scrollback = this.options.scrollback;
      const theme = this.options.theme?.();
      if (theme) request.theme = theme;
      socket.send(encodeAttach(request));
      this.startTimers();
    };
    socket.onmessage = (event) => {
      if (generation !== this.generation) return;
      this.lastFrameAt = Date.now();
      let frame: ServerFrame;
      try {
        frame = decodeServerFrame(event.data instanceof ArrayBuffer ? event.data : new Uint8Array(0));
      } catch (error) {
        if (error instanceof ProtocolError) return this.resync();
        throw error;
      }
      this.onFrame(frame);
    };
    socket.onclose = (event) => {
      if (generation !== this.generation) return;
      this.socket = null;
      this.clearTimers();
      if (this.closed || this.state.kind === "exited") return;
      if (!opened) this.proxyFailures++;
      switch (event.code) {
        case 4404:
          return this.fail("gone");
        case 4403:
          return this.fail("origin");
        case 4409:
          this.setState({ kind: "unavailable", reason: "environment" });
          return this.retry();
        case 4401:
          // The ticket expired between issue and use (a slow network, a suspended tab): try again at once.
          if (this.attempt === 0) {
            this.attempt = 1;
            void this.connect();
            return;
          }
          return this.retry();
        default:
          return this.retry();
      }
    };
    socket.onerror = () => {
      /* every error is followed by a close, which decides */
    };
  }

  private onFrame(frame: ServerFrame): void {
    if (frame.kind === "event") {
      const event = frame.event;
      if (event.type === "hello") {
        this.ackBytes = event.ack_bytes > 0 ? event.ack_bytes : DEFAULT_ACK_BYTES;
        this.attempt = 0;
        this.setState({ kind: "live" });
      } else if (event.type === "exit") {
        this.setState({ kind: "exited", code: event.code, signal: event.signal });
      }
      this.options.onEvent?.(event);
      return;
    }
    if (frame.kind === "snapshot") {
      this.receivedSeq = frame.seq;
      this.apply({ kind: "snapshot", frame });
      return;
    }
    if (this.receivedSeq === null) this.receivedSeq = frame.seq;
    if (frame.seq > this.receivedSeq) {
      // A hole in the stream cannot be filled from here; start over from a snapshot.
      return this.resync();
    }
    const skip = this.receivedSeq - frame.seq;
    if (skip >= frame.data.length) return;
    const data = skip ? frame.data.subarray(skip) : frame.data;
    this.receivedSeq += data.length;
    this.apply({ kind: "write", data, end: this.receivedSeq });
  }

  private apply(op: Pending): void {
    if (this.deferred.length || (op.kind === "snapshot" && this.inFlight > 0)) {
      this.deferred.push(op);
      return;
    }
    this.run(op);
  }

  private run(op: Pending): void {
    if (op.kind === "snapshot") {
      this.sink.reset(op.frame.cols, op.frame.rows);
      this.parsedSeq = op.frame.seq;
      // Acknowledged as soon as it is parsed, whatever its size: the daemon's window starts again
      // from the snapshot, and it should not wait for 64 KiB of new output to hear so.
      this.write(op.frame.data, op.frame.seq, true);
    } else {
      this.write(op.data, op.end, false);
    }
  }

  private write(data: Uint8Array, end: number, ackNow: boolean): void {
    this.inFlight++;
    this.sink.write(data, () => {
      this.inFlight--;
      this.parsedSeq = Math.max(this.parsedSeq, end);
      if (ackNow || this.parsedSeq - this.ackedSeq >= this.ackBytes) this.ack();
      this.drain();
    });
  }

  private drain(): void {
    while (this.deferred.length) {
      const next = this.deferred[0];
      if (next.kind === "snapshot" && this.inFlight > 0) return;
      this.deferred.shift();
      this.run(next);
    }
  }

  private ack(): void {
    if (!this.isOpen()) return;
    this.ackedSeq = this.parsedSeq;
    this.socket!.send(encodeAck(this.parsedSeq));
  }

  private startTimers(): void {
    this.clearTimers();
    this.keepalive = setInterval(() => this.ack(), KEEPALIVE_MS);
    this.watchdog = setInterval(() => {
      if (Date.now() - this.lastFrameAt > WATCHDOG_MS) this.restart();
    }, 5_000);
  }

  private clearTimers(): void {
    if (this.keepalive) clearInterval(this.keepalive);
    if (this.watchdog) clearInterval(this.watchdog);
    this.keepalive = this.watchdog = null;
  }

  private dropSocket(): void {
    this.generation++;
    const socket = this.socket;
    this.socket = null;
    if (socket) {
      socket.onopen = socket.onmessage = socket.onclose = socket.onerror = null;
      try {
        socket.close(1000);
      } catch {
        /* already closing */
      }
    }
  }

  /** Drop the socket and reconnect at once, keeping what the terminal holds. */
  private restart(): void {
    if (this.closed) return;
    this.clearTimers();
    this.dropSocket();
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = null;
    void this.connect();
  }

  /** Forget the stream position so the next attach brings a snapshot, and reconnect. */
  private resync(): void {
    this.receivedSeq = null;
    this.restart();
  }

  private retry(): void {
    if (this.closed) return;
    const delayMs = backoffDelay(this.attempt, this.deps.random);
    this.attempt++;
    if (this.proxyFailures >= PROXY_FAILURES) this.setState({ kind: "proxy-blocked" });
    else if (this.state.kind !== "unavailable") this.setState({ kind: "reconnecting", attempt: this.attempt, delayMs });
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      void this.connect();
    }, delayMs);
  }

  private fail(reason: UnavailableReason): void {
    this.clearTimers();
    this.dropSocket();
    this.setState({ kind: "unavailable", reason });
  }

  /** The page came back into view or online: a pending retry goes now, and a stale socket is replaced. */
  private wake(): void {
    if (this.closed || this.state.kind === "exited") return;
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
      void this.connect();
      return;
    }
    // A socket that has heard nothing for longer than a ping interval did not survive the sleep.
    if (this.socket && Date.now() - this.lastFrameAt > KEEPALIVE_MS + 5_000) this.restart();
  }
}
