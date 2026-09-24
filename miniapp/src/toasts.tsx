// Notifications as they arrive: a stack of at most three in the bottom-right corner of a desktop,
// one banner at the top of a phone (the bottom of a phone holds the tab bar and the composer).
//
// Only what the host marked `toast` is shown, only when it happened now (a replay after a reconnect
// updates the lists but never pops up), never while the page is hidden, and never about the session
// or terminal this window already shows: the conversation itself says it. The status line at the
// bottom (`dialogs.tsx`) is a different thing and is left alone.

import { useCallback, useEffect, useLayoutEffect, useReducer, useRef, useState } from "react";
import type { AppEvent, Notification } from "./api";
import { type EventMeta, useEvent } from "./events";
import { Icon } from "./icons";
import { shownScopes } from "./presence";
import { useMedia } from "./shell";
import { ActionButtons, categoryLabel, noticeIcon, openEntry, toneClass } from "./notifications";
import { plural, t } from "./i18n";

export const TOAST_MS = 6000;
/** A request waits longer: it is there to be answered, not only read. */
export const ACTIONABLE_MS = 20000;
export const STACK_MAX = 3;
/** How far a banner is pushed up before it counts as swiped away. */
const SWIPE_PX = 40;

export type ToastItem = { entry: Notification; seq: number; deadline: number };

/**
 * The toasts on screen and the ones waiting for room, kept apart from React so the rules are
 * tested as rules: at most `max` shown, newest on top; the rest wait in arrival order and take the
 * place of the first one to leave; a repeat of an entry already here replaces it rather than
 * stacking; the timers stop while the reader has a hand on the stack.
 */
export class ToastQueue {
  shown: ToastItem[] = [];
  waiting: ToastItem[] = [];
  private pausedAt: number | null = null;

  constructor(public max = STACK_MAX, private now: () => number = () => Date.now()) {}

  static duration(entry: Notification): number {
    return entry.needs_you && entry.actions.some((a) => a.id !== "open") ? ACTIONABLE_MS : TOAST_MS;
  }

  push(entry: Notification, seq: number): void {
    const here = this.shown.find((i) => i.entry.id === entry.id);
    if (here) {
      here.entry = entry;
      here.seq = seq;
      here.deadline = this.now() + ToastQueue.duration(entry);
      return;
    }
    const queued = this.waiting.find((i) => i.entry.id === entry.id);
    if (queued) {
      queued.entry = entry;
      queued.seq = seq;
      return;
    }
    this.waiting.push({ entry, seq, deadline: 0 });
    this.fill();
  }

  dismiss(id: number): boolean {
    const before = this.shown.length + this.waiting.length;
    this.shown = this.shown.filter((i) => i.entry.id !== id);
    this.waiting = this.waiting.filter((i) => i.entry.id !== id);
    this.fill();
    return this.shown.length + this.waiting.length !== before;
  }

  /** Drop what has run out; returns the next moment something will, or null. */
  tick(): number | null {
    if (this.pausedAt === null) {
      const now = this.now();
      this.shown = this.shown.filter((i) => i.deadline > now);
      this.fill();
    }
    return this.next();
  }

  next(): number | null {
    if (this.pausedAt !== null || this.shown.length === 0) return null;
    return Math.min(...this.shown.map((i) => i.deadline));
  }

  pause(): void {
    if (this.pausedAt === null) this.pausedAt = this.now();
  }

  resume(): void {
    if (this.pausedAt === null) return;
    const held = this.now() - this.pausedAt;
    this.pausedAt = null;
    for (const item of this.shown) item.deadline += held;
  }

  get paused(): boolean {
    return this.pausedAt !== null;
  }

  /** Newest on top: the order the reader sees. */
  visible(): ToastItem[] {
    return [...this.shown].sort((a, b) => b.seq - a.seq);
  }

  setMax(max: number): void {
    this.max = max;
    // A window narrowed to a phone keeps the newest; the others go back in line rather than away.
    while (this.shown.length > max) {
      const oldest = this.shown.reduce((a, b) => (a.seq < b.seq ? a : b));
      this.shown = this.shown.filter((i) => i !== oldest);
      this.waiting.unshift({ ...oldest, deadline: 0 });
    }
    this.fill();
  }

  private fill(): void {
    const now = this.now();
    while (this.shown.length < this.max && this.waiting.length > 0) {
      const item = this.waiting.shift()!;
      item.deadline = now + ToastQueue.duration(item.entry);
      this.shown.push(item);
    }
  }
}

export type ToastContext = { visible: boolean; shown: { sessions: Set<string>; terminals: Set<string>; projects: Set<string> } };

/** Whether a `notify` event becomes a toast in this window. */
export function shouldToast(payload: AppEvent["payload"], meta: EventMeta, ctx: ToastContext): boolean {
  if (meta.replayed || !payload.toast) return false;
  const entry = payload.notification as Notification | undefined;
  if (!entry || typeof entry.id !== "number") return false;
  if (entry.level === "quiet" || entry.resolved) return false;
  if (!ctx.visible) return false;
  if (entry.session_id && ctx.shown.sessions.has(entry.session_id)) return false;
  if (entry.terminal_id && ctx.shown.terminals.has(entry.terminal_id)) return false;
  // A project-wide item (a report, a review) is attended by the project's own page.
  if (!entry.session_id && !entry.terminal_id && entry.project_id && ctx.shown.projects.has(entry.project_id)) return false;
  return true;
}

function currentContext(): ToastContext {
  return { visible: document.visibilityState === "visible", shown: shownScopes() };
}

/** Sequence numbers already raised: a stream that replays the same frame must not raise it twice. */
const RAISED_MAX = 200;

export function NotificationToasts() {
  const wide = useMedia("(min-width: 1024px)");
  const queue = useRef(new ToastQueue(wide ? STACK_MAX : 1));
  const raised = useRef<number[]>([]);
  const [, redraw] = useReducer((n: number) => n + 1, 0);
  const timer = useRef<number | undefined>(undefined);

  const schedule = useCallback(() => {
    window.clearTimeout(timer.current);
    const next = queue.current.tick();
    redraw();
    if (next !== null) timer.current = window.setTimeout(schedule, Math.max(50, next - Date.now()));
  }, []);

  useEffect(() => {
    queue.current.setMax(wide ? STACK_MAX : 1);
    schedule();
  }, [wide, schedule]);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  useEvent(["notify"], (event, meta) => {
    if (!shouldToast(event.payload, meta, currentContext())) return;
    const seq = event.seq || Date.now();
    if (event.seq) {
      if (raised.current.includes(event.seq)) return;
      raised.current = [...raised.current.slice(-RAISED_MAX + 1), event.seq];
    }
    queue.current.push(event.payload.notification as Notification, seq);
    schedule();
  });
  // Answered or read somewhere else: the toast has nothing left to say.
  useEvent(["notify.resolved"], (event) => {
    if (queue.current.dismiss(Number(event.payload.id))) schedule();
  });
  useEvent(["notify.seen"], (event) => {
    const ids = event.payload.ids;
    if (ids === "all") {
      for (const item of [...queue.current.shown, ...queue.current.waiting]) queue.current.dismiss(item.entry.id);
    } else if (Array.isArray(ids)) {
      for (const id of ids) queue.current.dismiss(Number(id));
    }
    schedule();
  });

  const dismiss = (id: number) => {
    queue.current.dismiss(id);
    schedule();
  };
  const hold = () => {
    queue.current.pause();
    schedule();
  };
  const letGo = () => {
    queue.current.resume();
    schedule();
  };

  const stack = useRef<HTMLDivElement>(null);
  const bottom = useComposerClearance(stack, wide, queue.current.shown.length);
  const items = queue.current.visible();
  const waiting = queue.current.waiting.length;
  if (items.length === 0) return null;
  return (
    <div
      ref={stack}
      className={`notice-toasts ${wide ? "stack" : "banner"}`}
      role="region"
      aria-label={t("notice.region")}
      style={wide && bottom !== null ? { bottom } : undefined}
      onMouseEnter={hold}
      onMouseLeave={letGo}
      onFocus={hold}
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) letGo();
      }}
    >
      {items.map((item) => (
        <Toast key={item.entry.id} entry={item.entry} onDismiss={() => dismiss(item.entry.id)} swipe={!wide} />
      ))}
      {waiting > 0 && <div className="notice-waiting">{plural("notice.queued", waiting)}</div>}
    </div>
  );
}

function Toast({ entry, onDismiss, swipe }: { entry: Notification; onDismiss: () => void; swipe: boolean }) {
  const [drag, setDrag] = useState(0);
  const start = useRef<number | null>(null);
  const open = () => {
    openEntry(entry);
    onDismiss();
  };
  return (
    <div
      className={`notice-toast ${entry.level === "urgent" ? "urgent" : ""}`}
      data-notice={entry.id}
      role={entry.level === "urgent" ? "alert" : "status"}
      style={drag < 0 ? { transform: `translateY(${drag}px)`, opacity: Math.max(0.2, 1 + drag / 120) } : undefined}
      onClick={open}
      onTouchStart={swipe ? (e) => { start.current = e.touches[0].clientY; } : undefined}
      onTouchMove={swipe ? (e) => { if (start.current !== null) setDrag(Math.min(0, e.touches[0].clientY - start.current)); } : undefined}
      onTouchEnd={swipe ? () => { start.current = null; if (drag < -SWIPE_PX) onDismiss(); else setDrag(0); } : undefined}
    >
      <div className="notice-toast-head">
        <span className={`kind ${toneClass(entry)}`} aria-label={categoryLabel(entry.category)}><Icon name={noticeIcon(entry)} size={14} /></span>
        <b className="notice-title clamp-2">{entry.title}</b>
        <button className="iconbtn small quiet" aria-label={t("notice.dismiss")} title={t("notice.dismiss")} onClick={(e) => { e.stopPropagation(); onDismiss(); }}>
          <Icon name="close" size={14} />
        </button>
      </div>
      {entry.body && <div className="notice-body clamp-2">{entry.body}</div>}
      <ActionButtons entry={entry} onDone={onDismiss} />
    </div>
  );
}

/**
 * How high the stack must sit to stay clear of a composer under it. A small laptop puts the
 * conversation's composer right across the bottom-right corner, and a toast drawn over the field the
 * operator is typing in is worse than no toast; so the stack rises above whichever composer it
 * would cover. Null: the corner is free.
 */
function useComposerClearance(stack: React.RefObject<HTMLDivElement | null>, wide: boolean, count: number): number | null {
  const [bottom, setBottom] = useState<number | null>(null);
  useLayoutEffect(() => {
    if (!wide || count === 0) return;
    const measure = () => {
      const box = stack.current?.getBoundingClientRect();
      const left = box ? box.left : window.innerWidth - 376;
      let top = Number.POSITIVE_INFINITY;
      for (const el of document.querySelectorAll<HTMLElement>(".composer-box")) {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.right > left) top = Math.min(top, r.top);
      }
      setBottom(Number.isFinite(top) ? Math.round(window.innerHeight - top + 12) : null);
    };
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [stack, wide, count]);
  return bottom;
}
