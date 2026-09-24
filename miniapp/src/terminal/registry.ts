// Every open terminal instance, kept alive across moves between the session dock, the full-screen
// view and the Terminals screen.
//
// An xterm.js instance is never thrown away and rebuilt from a snapshot just because the component
// showing it moved: its element is re-parented, and it comes back with its scrollback, selection and
// search intact. What that costs is bounded here:
// - at most `maxDetached` instances nobody shows are kept, least recently shown dropped first;
// - a detached instance's socket closes after `lingerMs` (coming back reattaches for the tail);
// - at most `webglMax` instances hold a WebGL context, the most recently used visible ones. Browsers
//   allow about sixteen contexts per page and silently kill the oldest past that, which leaves a
//   terminal blank; hidden instances give theirs up so the visible ones never hit that wall.

export const MAX_DETACHED = 8;
export const LINGER_MS = 30_000;
export const WEBGL_MAX = 6;

/** What the registry asks of an instance; the real one wraps xterm.js and its connection. */
export interface RegistryHooks<T> {
  create(id: string): T;
  dispose(instance: T): void;
  /** Close the live connection, keeping the screen. */
  sleep(instance: T): void;
  /** Reconnect after `sleep`, asking for the tail. */
  wake(instance: T): void;
  /** Give the instance a WebGL renderer, or take it away (the DOM renderer draws instead). */
  webgl(instance: T, on: boolean): void;
}

export type RegistryDeps = {
  setTimeout: (callback: () => void, ms: number) => unknown;
  clearTimeout: (handle: unknown) => void;
  now: () => number;
};

type Entry<T> = {
  id: string;
  instance: T;
  /** How many views hold it. Zero means detached. */
  holders: number;
  visible: boolean;
  usedAt: number;
  detachedAt: number;
  asleep: boolean;
  webgl: boolean;
  linger: unknown;
};

export class TerminalRegistry<T> {
  private entries = new Map<string, Entry<T>>();

  constructor(
    private readonly hooks: RegistryHooks<T>,
    private readonly limits = { maxDetached: MAX_DETACHED, lingerMs: LINGER_MS, webglMax: WEBGL_MAX },
    private readonly deps: RegistryDeps = { setTimeout: (f, ms) => setTimeout(f, ms), clearTimeout: (h) => clearTimeout(h as ReturnType<typeof setTimeout>), now: () => Date.now() },
  ) {}

  /** The instance for a terminal, created on first use. Every `acquire` is paired with a `release`. */
  acquire(id: string): T {
    let entry = this.entries.get(id);
    if (!entry) {
      entry = { id, instance: this.hooks.create(id), holders: 0, visible: false, usedAt: 0, detachedAt: 0, asleep: false, webgl: false, linger: null };
      this.entries.set(id, entry);
    }
    entry.holders++;
    entry.usedAt = this.deps.now();
    if (entry.linger !== null) this.deps.clearTimeout(entry.linger);
    entry.linger = null;
    if (entry.asleep) {
      entry.asleep = false;
      this.hooks.wake(entry.instance);
    }
    return entry.instance;
  }

  /** A view let go of the instance. When nobody holds it, it is detached: kept, then put to sleep. */
  release(id: string): void {
    const entry = this.entries.get(id);
    if (!entry || entry.holders === 0) return;
    entry.holders--;
    if (entry.holders > 0) return;
    entry.visible = false;
    entry.detachedAt = this.deps.now();
    entry.linger = this.deps.setTimeout(() => {
      entry.linger = null;
      if (entry.holders === 0 && !entry.asleep) {
        entry.asleep = true;
        this.hooks.sleep(entry.instance);
      }
    }, this.limits.lingerMs);
    this.evict();
    this.budget();
  }

  /** Whether a held instance is on screen now (a background tab holds it but does not show it). */
  setVisible(id: string, visible: boolean): void {
    const entry = this.entries.get(id);
    if (!entry || entry.holders === 0 || entry.visible === visible) return;
    entry.visible = visible;
    if (visible) entry.usedAt = this.deps.now();
    this.budget();
  }

  /** The person used this terminal: it moves to the front for a WebGL context. */
  touch(id: string): void {
    const entry = this.entries.get(id);
    if (!entry) return;
    entry.usedAt = this.deps.now();
    this.budget();
  }

  /** Forget a terminal entirely (it was ended and removed): dispose its instance now. */
  remove(id: string): void {
    const entry = this.entries.get(id);
    if (!entry) return;
    if (entry.linger !== null) this.deps.clearTimeout(entry.linger);
    this.entries.delete(id);
    this.hooks.dispose(entry.instance);
    this.budget();
  }

  has(id: string): boolean {
    return this.entries.has(id);
  }

  /** Ids by state, for the tests and for a debug view. */
  snapshot(): { id: string; holders: number; visible: boolean; asleep: boolean; webgl: boolean }[] {
    return [...this.entries.values()].map(({ id, holders, visible, asleep, webgl }) => ({ id, holders, visible, asleep, webgl }));
  }

  private evict(): void {
    const detached = [...this.entries.values()].filter((e) => e.holders === 0).sort((a, b) => a.detachedAt - b.detachedAt);
    while (detached.length > this.limits.maxDetached) this.remove(detached.shift()!.id);
  }

  /** Hand WebGL to the most recently used visible instances, at most `webglMax`, and take it from the rest. */
  private budget(): void {
    const wanted = new Set(
      [...this.entries.values()]
        .filter((e) => e.holders > 0 && e.visible)
        .sort((a, b) => b.usedAt - a.usedAt)
        .slice(0, this.limits.webglMax)
        .map((e) => e.id),
    );
    // Contexts are given up before any is created, so the count never passes the limit in between.
    for (const e of this.entries.values()) {
      if (e.webgl && !wanted.has(e.id)) {
        e.webgl = false;
        this.hooks.webgl(e.instance, false);
      }
    }
    for (const e of this.entries.values()) {
      if (!e.webgl && wanted.has(e.id)) {
        e.webgl = true;
        this.hooks.webgl(e.instance, true);
      }
    }
  }
}
