// The temporary layers: a sheet (bottom drawer on a phone, dialog on a desktop), a confirmation
// that says what happens, an overflow menu, and a toast that can undo. Escape closes the top one,
// focus goes in and comes back to the control that opened it.

import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { Icon, IconName } from "./icons";

// ── sheet ────────────────────────────────────────────────────────────────────────────────

/** The layers open right now, top last: Escape goes to the top one only. */
const layers: symbol[] = [];
function useLayer(onEscape: () => void) {
  const cb = useRef(onEscape);
  cb.current = onEscape;
  useEffect(() => {
    const me = Symbol("layer");
    layers.push(me);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && layers[layers.length - 1] === me) {
        e.preventDefault();
        cb.current();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      const i = layers.indexOf(me);
      if (i >= 0) layers.splice(i, 1);
    };
  }, []);
}

export function Sheet({ title, ariaLabel, onClose, children, size, className, head }: { title?: ReactNode; ariaLabel?: string; onClose: () => void; children: ReactNode; size?: "wide" | "narrow" | "full"; className?: string; head?: ReactNode }) {
  const panel = useRef<HTMLDivElement>(null);
  useLayer(onClose);
  // Focus moves in once, on open, and back to the control that opened the sheet when it closes;
  // the callback's identity changes on every parent render and must not re-run this.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    panel.current?.focus();
    return () => {
      opener?.focus?.();
    };
  }, []);
  return (
    <div className="sheet-backdrop" onClick={onClose}>
      <div ref={panel} tabIndex={-1} className={`sheet ${size ?? ""} ${className ?? ""}`} onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true" aria-label={typeof title === "string" ? title : ariaLabel}>
        <div className="grip" />
        <div className="sheet-head">
          {title && <h3>{title}</h3>}
          {head}
          <button className="iconbtn small" onClick={onClose} aria-label="Close" title="Close"><Icon name="close" size={16} /></button>
        </div>
        <div className="sheet-body">{children}</div>
      </div>
    </div>
  );
}

// ── confirm ──────────────────────────────────────────────────────────────────────────────

export type ConfirmOptions = { title: string; body?: ReactNode; action?: string; cancel?: string; danger?: boolean };

type Pending = ConfirmOptions & { resolve: (ok: boolean) => void };
let pendingSetter: ((f: (cur: Pending | null) => Pending | null) => void) | null = null;

/** Asks before something irreversible: the title names the action, the body says what it does. */
export function confirmDialog(opts: ConfirmOptions): Promise<boolean> {
  return new Promise((resolve) => {
    if (!pendingSetter) {
      resolve(window.confirm(opts.title));
      return;
    }
    // A second question while one is open answers the first with "no" rather than leaving it hanging.
    pendingSetter((cur) => {
      cur?.resolve(false);
      return { ...opts, resolve };
    });
  });
}

export function ConfirmHost() {
  const [pending, setPending] = useState<Pending | null>(null);
  useEffect(() => {
    pendingSetter = setPending;
    return () => {
      pendingSetter = null;
    };
  }, []);
  if (!pending) return null;
  return <ConfirmDialog pending={pending} onDone={(ok) => { pending.resolve(ok); setPending(null); }} />;
}

function ConfirmDialog({ pending, onDone }: { pending: Pending; onDone: (ok: boolean) => void }) {
  const box = useRef<HTMLDivElement>(null);
  useLayer(() => onDone(false));
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    // The safe button takes focus when the action destroys something; Tab stays inside the dialog.
    const buttons = box.current?.querySelectorAll<HTMLButtonElement>("button") ?? [];
    (pending.danger ? buttons[0] : buttons[1])?.focus();
    return () => opener?.focus?.();
  }, [pending.danger]);
  const trap = (e: React.KeyboardEvent) => {
    if (e.key !== "Tab") return;
    const buttons = Array.from(box.current?.querySelectorAll<HTMLButtonElement>("button") ?? []);
    if (!buttons.length) return;
    const i = buttons.indexOf(document.activeElement as HTMLButtonElement);
    const next = e.shiftKey ? buttons[(i - 1 + buttons.length) % buttons.length] : buttons[(i + 1) % buttons.length];
    next.focus();
    e.preventDefault();
  };
  return (
    <div className="sheet-backdrop confirm" onClick={() => onDone(false)}>
      <div ref={box} className="dialog" role="alertdialog" aria-modal="true" aria-labelledby="confirm-title" onClick={(e) => e.stopPropagation()} onKeyDown={trap}>
        <h3 id="confirm-title">{pending.title}</h3>
        {pending.body && <div className="dialog-body">{pending.body}</div>}
        <div className="dialog-actions">
          <button className="btn ghost" onClick={() => onDone(false)}>{pending.cancel ?? "Cancel"}</button>
          <button className={`btn ${pending.danger ? "danger solid" : "primary"}`} onClick={() => onDone(true)}>
            {pending.action ?? "OK"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── overflow menu ────────────────────────────────────────────────────────────────────────

export type MenuItem = { label: string; icon?: IconName; danger?: boolean; disabled?: boolean; onSelect: () => void } | "-";

export function OverflowMenu({ items, label = "More", icon = "more", small, className }: { items: MenuItem[]; label?: string; icon?: IconName; small?: boolean; className?: string }) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ top: number; right: number } | null>(null);
  useLayoutEffect(() => {
    if (!open || !trigger.current) return;
    const r = trigger.current.getBoundingClientRect();
    setPos({ top: r.bottom + 4, right: Math.max(8, window.innerWidth - r.right) });
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent | TouchEvent) => {
      if (menu.current?.contains(e.target as Node) || trigger.current?.contains(e.target as Node)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        const buttons = Array.from(menu.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? []);
        const i = buttons.indexOf(document.activeElement as HTMLButtonElement);
        const next = e.key === "ArrowDown" ? buttons[(i + 1) % buttons.length] : buttons[(i - 1 + buttons.length) % buttons.length];
        next?.focus();
        e.preventDefault();
      }
    };
    const onScroll = () => setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("touchstart", onDown);
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, { capture: true });
    (menu.current?.querySelector("button:not(:disabled)") as HTMLButtonElement | null)?.focus();
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("touchstart", onDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll, { capture: true });
      // The menu is gone; a keyboard reader continues from the control that opened it.
      if (document.activeElement === document.body || !document.activeElement) trigger.current?.focus();
    };
  }, [open]);
  return (
    <>
      <button ref={trigger} className={`iconbtn ${small ? "small" : ""} ${open ? "on" : ""} ${className ?? ""}`} aria-label={label} title={label} aria-haspopup="menu" aria-expanded={open} onClick={(e) => { e.stopPropagation(); setOpen((o) => !o); }}>
        <Icon name={icon} size={small ? 16 : 18} />
      </button>
      {open && pos && (
        <div ref={menu} className="menu" role="menu" style={{ position: "fixed", top: pos.top, right: pos.right }} onClick={(e) => e.stopPropagation()}>
          {items.map((it, i) =>
            it === "-" ? (
              <div key={i} className="menu-sep" />
            ) : (
              <button key={i} role="menuitem" className={it.danger ? "danger" : ""} disabled={it.disabled} onClick={() => { setOpen(false); it.onSelect(); }}>
                {it.icon && <Icon name={it.icon} size={16} />}
                {it.label}
              </button>
            ),
          )}
        </div>
      )}
    </>
  );
}

// ── toast ────────────────────────────────────────────────────────────────────────────────

type ToastState = { text: string; undo?: () => void; id: number };
type ToastUpdate = (f: (cur: ToastState | null) => ToastState | null) => void;
let toastSetter: ToastUpdate | null = null;
let toastSeq = 0;

/** A short status line at the bottom; with `undo`, a button that calls it before the timer runs out. */
export function toast(text: string, opts: { undo?: () => void; ms?: number } = {}): void {
  if (!toastSetter) return;
  const id = ++toastSeq;
  toastSetter(() => ({ text, undo: opts.undo, id }));
  window.setTimeout(() => toastSetter?.((cur) => (cur && cur.id === id ? null : cur)), opts.ms ?? (opts.undo ? 6000 : 2600));
}

export function ToastHost() {
  const [state, setState] = useState<ToastState | null>(null);
  useEffect(() => {
    toastSetter = setState;
    return () => {
      toastSetter = null;
    };
  }, []);
  if (!state) return null;
  return (
    <div className="toast" role="status" aria-live="polite">
      <span>{state.text}</span>
      {state.undo && (
        <button className="btn small ghost" onClick={() => { state.undo?.(); setState(null); }}>
          Undo
        </button>
      )}
    </div>
  );
}

const UNDO_MS = 5000;

/**
 * Deletes after a pause: the row disappears at once, the toast offers Undo for exactly as long as
 * the request is held back, and the request goes out only when the pause ends without one.
 */
export function deleteWithUndo(label: string, commit: () => Promise<void>, onUndo: () => void, onFail: (e: unknown) => void): void {
  let undone = false;
  const timer = window.setTimeout(() => {
    toastSetter?.((cur) => (cur && cur.text === label ? null : cur));
    commit().catch((e) => {
      onUndo();
      onFail(e);
    });
  }, UNDO_MS);
  toast(label, {
    ms: UNDO_MS,
    undo: () => {
      undone = true;
      window.clearTimeout(timer);
      onUndo();
    },
  });
  void undone;
}
