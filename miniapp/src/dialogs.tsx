// The temporary layers: a sheet (bottom drawer on a phone, dialog on a desktop), a confirmation
// that says what happens, an overflow menu, and a toast that can undo. Escape closes the top one,
// focus goes in and comes back to the control that opened it.

import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { Icon, IconName } from "./icons";

// ── sheet ────────────────────────────────────────────────────────────────────────────────

export function Sheet({ title, onClose, children, size, className, head }: { title?: ReactNode; onClose: () => void; children: ReactNode; size?: "wide" | "narrow" | "full"; className?: string; head?: ReactNode }) {
  const panel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    panel.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      opener?.focus?.();
    };
  }, [onClose]);
  return (
    <div className="sheet-backdrop" onClick={onClose}>
      <div ref={panel} tabIndex={-1} className={`sheet ${size ?? ""} ${className ?? ""}`} onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true" aria-label={typeof title === "string" ? title : undefined}>
        <div className="grip" />
        {(title || head) && (
          <div className="sheet-head">
            {title && <h3>{title}</h3>}
            {head}
            <button className="iconbtn small" onClick={onClose} aria-label="Close" title="Close"><Icon name="close" size={16} /></button>
          </div>
        )}
        <div className="sheet-body">{children}</div>
      </div>
    </div>
  );
}

// ── confirm ──────────────────────────────────────────────────────────────────────────────

export type ConfirmOptions = { title: string; body?: ReactNode; action?: string; cancel?: string; danger?: boolean };

type Pending = ConfirmOptions & { resolve: (ok: boolean) => void };
let pendingSetter: ((p: Pending | null) => void) | null = null;

/** Asks before something irreversible: the title names the action, the body says what it does. */
export function confirmDialog(opts: ConfirmOptions): Promise<boolean> {
  return new Promise((resolve) => {
    if (!pendingSetter) {
      resolve(window.confirm(opts.title));
      return;
    }
    pendingSetter({ ...opts, resolve });
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
  const done = (ok: boolean) => {
    pending.resolve(ok);
    setPending(null);
  };
  return (
    <div className="sheet-backdrop confirm" onClick={() => done(false)}>
      <div className="dialog" role="alertdialog" aria-modal="true" aria-labelledby="confirm-title" onClick={(e) => e.stopPropagation()}>
        <h3 id="confirm-title">{pending.title}</h3>
        {pending.body && <div className="dialog-body">{pending.body}</div>}
        <div className="dialog-actions">
          <button className="btn ghost" onClick={() => done(false)}>{pending.cancel ?? "Cancel"}</button>
          <button className={`btn ${pending.danger ? "danger solid" : "primary"}`} autoFocus onClick={() => done(true)} onKeyDown={(e) => e.key === "Escape" && done(false)}>
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
    document.addEventListener("mousedown", onDown);
    document.addEventListener("touchstart", onDown);
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", () => setOpen(false), { once: true, capture: true });
    (menu.current?.querySelector("button:not(:disabled)") as HTMLButtonElement | null)?.focus();
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("touchstart", onDown);
      document.removeEventListener("keydown", onKey);
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

/**
 * Deletes after a pause: the row disappears at once, the toast offers Undo, and the request
 * goes out only when the pause ends without one.
 */
export function deleteWithUndo(label: string, commit: () => Promise<void>, onUndo: () => void, onFail: (e: unknown) => void): void {
  let undone = false;
  toast(label, {
    undo: () => {
      undone = true;
      onUndo();
    },
  });
  window.setTimeout(() => {
    if (undone) return;
    commit().catch((e) => {
      onUndo();
      onFail(e);
    });
  }, 5200);
}
