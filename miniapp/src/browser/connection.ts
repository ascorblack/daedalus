// One live view of a browser group: ticket, WebSocket, ATTACH, frames drawn and acknowledged, input
// while the operator drives, and getting back after anything goes wrong.
//
// It keeps the terminal connection's rules where they apply (terminal/connection.ts says why each one
// exists): a single-use ticket per socket, a reconnect with backoff that a visible page or a network
// coming back cuts short, 45 s of silence meaning a dead socket (the daemon pings every 20 s), and a
// socket that keeps failing behind good tickets meaning a proxy in between.
//
// Its own rule is flow control. The daemon sends the next frame only after the client has drawn the
// last one and said so (ACK), so a slow phone gets fewer frames and never a backlog. The ACK goes when
// the picture is on the screen, not when the bytes arrived: acknowledging on arrival would let a
// client that decodes slowly ask for frames faster than it can show them. Should a second frame arrive
// while one is still being decoded, only the newest is drawn.

import { api, ApiError } from "../api";
import { backoffDelay, type SocketLike } from "../terminal/connection";
import {
  decodeServerFrame,
  encodeAck,
  encodeAttach,
  encodeInput,
  encodeView,
  ProtocolError,
  splitText,
  type Attach,
  type FrameMeta,
  type InputMessage,
  type ServerFrame,
  type Tier,
  type ViewChange,
  type ViewEvent,
} from "./protocol";

export type ViewState =
  | { kind: "connecting" }
  | { kind: "live" }
  | { kind: "reconnecting"; attempt: number; delayMs: number }
  | { kind: "unavailable"; reason: "gone" | "environment" | "origin" | "auth" }
  | { kind: "proxy-blocked" };

export type ViewDeps = {
  ticket: (group: string, tier: Tier, readOnly: boolean) => Promise<{ ticket: string }>;
  socket: (url: string) => SocketLike;
  base: () => string;
  wake: (callback: () => void) => () => void;
  random: () => number;
};

/** Where frames go: the viewer's canvas, or a fake in tests. `draw` settles once the picture is shown. */
export interface FrameSink {
  draw(frame: { frameNo: number; meta: FrameMeta; image: Uint8Array }): Promise<void>;
}

export type ViewOptions = {
  group: string;
  tier: Tier;
  tab?: string;
  readOnly?: boolean;
  /** The box to ask for, read at every attach: the element may have changed size while offline. */
  box: () => Omit<Attach, "tier" | "tab">;
  sink: FrameSink;
  onState?: (state: ViewState) => void;
  onEvent?: (event: ViewEvent) => void;
};

export const WATCHDOG_MS = 45_000;
export const PING_MS = 20_000;
export const PROXY_FAILURES = 3;
const OPEN = 1;

export function browserViewDeps(): ViewDeps {
  return {
    ticket: (group, tier, readOnly) => api.browserTicket(group, tier, readOnly),
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

type Frame = Extract<ServerFrame, { kind: "frame" }>;

export class BrowserConnection {
  private state: ViewState = { kind: "connecting" };
  private socket: SocketLike | null = null;
  private attempt = 0;
  private proxyFailures = 0;
  private generation = 0;
  private closed = false;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private watchdog: ReturnType<typeof setInterval> | null = null;
  private lastFrameAt = 0;
  private unwake: () => void;
  private drawing = false;
  private waiting: Frame | null = null;
  private tier: Tier;
  private tab: string | undefined;
  /** This client's id in the daemon, from `hello`: what taking control names. */
  clientId: string | null = null;
  /** Frames dropped because a newer one arrived before they could be drawn. */
  dropped = 0;
  /** Frames drawn and acknowledged. */
  drawn = 0;

  constructor(private readonly options: ViewOptions, private readonly deps: ViewDeps = browserViewDeps()) {
    this.tier = options.tier;
    this.tab = options.tab;
    this.unwake = deps.wake(() => this.wake());
    void this.connect();
  }

  get current(): ViewState {
    return this.state;
  }

  /** Another tier, another tab or another size, on the same socket. */
  view(change: ViewChange): void {
    if (change.tier) this.tier = change.tier;
    if (change.tab) this.tab = change.tab;
    if (this.isLive()) this.socket!.send(encodeView(change));
  }

  /** Send the operator's input. False when there is nowhere to send it: not live, or read-only. */
  input(message: InputMessage): boolean {
    if (this.options.readOnly || !this.isLive()) return false;
    if (message.t === "text") {
      for (const piece of splitText(message.text)) this.socket!.send(encodeInput({ t: "text", text: piece }));
      return true;
    }
    this.socket!.send(encodeInput(message));
    return true;
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.unwake();
    this.clearTimers();
    this.dropSocket();
  }

  private isLive(): boolean {
    return this.state.kind === "live" && !!this.socket && this.socket.readyState === OPEN;
  }

  private setState(state: ViewState): void {
    this.state = state;
    this.options.onState?.(state);
  }

  private async connect(): Promise<void> {
    if (this.closed) return;
    const generation = ++this.generation;
    if (this.state.kind !== "proxy-blocked" && this.state.kind !== "unavailable") this.setState(this.attempt ? { kind: "reconnecting", attempt: this.attempt, delayMs: 0 } : { kind: "connecting" });
    let ticket: string;
    try {
      ticket = (await this.deps.ticket(this.options.group, this.tier, !!this.options.readOnly)).ticket;
    } catch (error) {
      if (generation !== this.generation || this.closed) return;
      if (error instanceof ApiError && error.status === 404) return this.fail("gone");
      if (error instanceof ApiError && (error.status === 401 || error.status === 403)) return this.fail("auth");
      if (error instanceof ApiError && error.status === 409) this.setState({ kind: "unavailable", reason: "environment" });
      return this.retry();
    }
    if (generation !== this.generation || this.closed) return;
    this.open(`${this.deps.base()}/ws/browsers/${encodeURIComponent(this.options.group)}?ticket=${encodeURIComponent(ticket)}`, generation);
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
      this.drawing = false;
      this.waiting = null;
      socket.send(encodeAttach({ tier: this.tier, ...(this.tab ? { tab: this.tab } : {}), ...this.options.box() }));
      this.startTimers();
    };
    socket.onmessage = (event) => {
      if (generation !== this.generation) return;
      this.lastFrameAt = Date.now();
      let frame: ServerFrame;
      try {
        frame = decodeServerFrame(event.data instanceof ArrayBuffer ? event.data : new Uint8Array(0));
      } catch (error) {
        // A frame this client cannot read is dropped rather than drawn wrong; the next one replaces it.
        if (error instanceof ProtocolError) return;
        throw error;
      }
      if (frame.kind === "event") return this.onEvent(frame.event);
      this.onFrame(frame, generation);
    };
    socket.onclose = (event) => {
      if (generation !== this.generation) return;
      this.socket = null;
      this.clearTimers();
      if (this.closed) return;
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
          // The ticket expired between issue and use: try again at once, once.
          if (this.attempt === 0) {
            this.attempt = 1;
            void this.connect();
            return;
          }
          return this.retry();
        default:
          // 1012 is the host or the daemon restarting; like any other drop, it is retried.
          return this.retry();
      }
    };
    socket.onerror = () => {
      /* every error is followed by a close, which decides */
    };
  }

  private onEvent(event: ViewEvent): void {
    if (event.type === "hello") {
      this.clientId = event.client_id;
      this.attempt = 0;
      this.setState({ kind: "live" });
    }
    this.options.onEvent?.(event);
  }

  private onFrame(frame: Frame, generation: number): void {
    if (this.drawing) {
      if (this.waiting) this.dropped++;
      this.waiting = frame;
      return;
    }
    this.draw(frame, generation);
  }

  private draw(frame: Frame, generation: number): void {
    this.drawing = true;
    const done = () => {
      if (generation !== this.generation) return;
      this.drawing = false;
      this.drawn++;
      if (this.socket && this.socket.readyState === OPEN) this.socket.send(encodeAck(frame.frameNo));
      const next = this.waiting;
      this.waiting = null;
      if (next) this.draw(next, generation);
    };
    // A frame the sink could not decode is still acknowledged: the daemon must not stop sending
    // because one JPEG was bad, and the next one repaints the whole picture anyway.
    this.options.sink.draw(frame).then(done, done);
  }

  private startTimers(): void {
    this.clearTimers();
    this.watchdog = setInterval(() => {
      if (Date.now() - this.lastFrameAt > WATCHDOG_MS) this.restart();
    }, 5_000);
  }

  private clearTimers(): void {
    if (this.watchdog) clearInterval(this.watchdog);
    this.watchdog = null;
  }

  private dropSocket(): void {
    this.generation++;
    this.drawing = false;
    this.waiting = null;
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

  private restart(): void {
    if (this.closed) return;
    this.clearTimers();
    this.dropSocket();
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = null;
    void this.connect();
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

  private fail(reason: "gone" | "environment" | "origin" | "auth"): void {
    this.clearTimers();
    this.dropSocket();
    this.setState({ kind: "unavailable", reason });
  }

  private wake(): void {
    if (this.closed) return;
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
      void this.connect();
      return;
    }
    if (this.socket && Date.now() - this.lastFrameAt > PING_MS + 5_000) this.restart();
  }
}
