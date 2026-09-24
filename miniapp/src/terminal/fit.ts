// When a terminal may tell the PTY its size, and what size.
//
// The old web terminal fitted hidden panes too: a pane in a background tab measures as a few pixels,
// the fit addon turned that into 9×5, and the shell — shared with every other view — was squeezed to
// nine columns. Everything here exists so that cannot happen again:
// - a hidden terminal never sends a size;
// - a visible one sends only when this window has focus or the person just used this terminal, so a
//   second monitor showing the same terminal cannot fight the screen being typed on;
// - nothing below 20×4 is ever sent (the daemon refuses it as well), nor above 500×300;
// - an undefined or unchanged proposal sends nothing.
// Column changes wait 100 ms, because dragging a divider proposes a new width every frame and each
// one makes a full-screen program redraw; a change in rows alone goes out on the next frame.

export const MIN_COLS = 20;
export const MIN_ROWS = 4;
export const MAX_COLS = 500;
export const MAX_ROWS = 300;
export const COLS_DEBOUNCE_MS = 100;

export type Size = { cols: number; rows: number };

export type FitContext = {
  /** The terminal is on screen (its element has a size and is not in a hidden tab or collapsed dock). */
  visible: boolean;
  /** The window has focus. */
  focused: boolean;
  /** The person typed into, clicked or resized this terminal just now. */
  interacted: boolean;
  /**
   * How long a change in rows alone waits, in ms (0 when not given). A phone sets it: its soft keyboard
   * animates the visible height over a few hundred ms, iOS Safari reports every step of it, and the
   * program should redraw once for the keyboard, not once per frame of its slide.
   */
  rowsDelay?: number;
};

export type FitDecision = { send: false } | { send: true; cols: number; rows: number; delay: number };

const clamp = (n: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, Math.floor(n)));

/** Whether to send a size, which one, and after how long. `prev` is the last size sent (or null). */
export function nextSize(prev: Size | null, proposed: Partial<Size> | undefined | null, context: FitContext): FitDecision {
  if (!context.visible) return { send: false };
  if (!context.focused && !context.interacted) return { send: false };
  if (!proposed || !Number.isFinite(proposed.cols) || !Number.isFinite(proposed.rows)) return { send: false };
  const cols = clamp(proposed.cols!, MIN_COLS, MAX_COLS);
  const rows = clamp(proposed.rows!, MIN_ROWS, MAX_ROWS);
  if (prev && prev.cols === cols && prev.rows === rows) return { send: false };
  const delay = prev && prev.cols === cols ? context.rowsDelay ?? 0 : COLS_DEBOUNCE_MS;
  return { send: true, cols, rows, delay };
}

export type SchedulerDeps = {
  setTimeout: (callback: () => void, ms: number) => unknown;
  clearTimeout: (handle: unknown) => void;
  /** Runs a callback on the next animation frame. */
  frame: (callback: () => void) => unknown;
  cancelFrame: (handle: unknown) => void;
};

const browserScheduler = (): SchedulerDeps => ({
  setTimeout: (callback, ms) => setTimeout(callback, ms),
  clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
  frame: (callback) => requestAnimationFrame(callback),
  cancelFrame: (handle) => cancelAnimationFrame(handle as number),
});

/**
 * Applies `nextSize` over time: one pending send at most, replaced by every newer proposal, so the
 * size that goes out is the last one proposed. `send` returns whether the size actually left (a
 * closed socket keeps it pending as "not sent", and the next proposal tries again).
 */
export class ResizeScheduler {
  private sent: Size | null = null;
  private timer: unknown = null;
  private frameHandle: unknown = null;

  constructor(private readonly send: (size: Size) => boolean, private readonly deps: SchedulerDeps = browserScheduler()) {}

  /** The last size that went out. */
  get last(): Size | null {
    return this.sent;
  }

  propose(proposed: Partial<Size> | undefined | null, context: FitContext): void {
    const decision = nextSize(this.sent, proposed, context);
    this.cancel();
    if (!decision.send) return;
    const size = { cols: decision.cols, rows: decision.rows };
    const fire = () => {
      this.timer = this.frameHandle = null;
      if (this.send(size)) this.sent = size;
    };
    if (decision.delay > 0) this.timer = this.deps.setTimeout(fire, decision.delay);
    else this.frameHandle = this.deps.frame(fire);
  }

  /** Drop a pending send: the terminal was hidden, or the window lost focus, before it went out. */
  cancel(): void {
    if (this.timer !== null) this.deps.clearTimeout(this.timer);
    if (this.frameHandle !== null) this.deps.cancelFrame(this.frameHandle);
    this.timer = this.frameHandle = null;
  }

  /** Forget what was sent, so the next proposal goes out even if unchanged (after a reconnect). */
  forget(): void {
    this.sent = null;
  }
}
