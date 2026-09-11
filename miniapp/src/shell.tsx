// The application shell: a page header with the screen's name and its actions, four tabs and a
// More sheet on a phone, a rail with grouped destinations on a desktop.

import { useEffect, useState, type ReactNode } from "react";
import { Icon, IconName } from "./icons";
import { Sheet } from "./dialogs";
import { Screen, navigate, pathFor } from "./router";

export type Counts = { inbox?: number; changes?: number; services?: number; agents?: number };

const TITLES: Record<Screen, string> = { agents: "Agents", inbox: "Inbox", board: "Board", changes: "Changes", schedules: "Schedules", services: "Services", memory: "Memory", usage: "Usage", health: "Health", settings: "Settings" };
const ICONS: Record<Screen, IconName> = { agents: "bots", inbox: "inbox", board: "board", changes: "changes", schedules: "clock", services: "globe", memory: "bulb", usage: "chart", health: "check", settings: "settings" };

export function screenTitle(s: Screen): string {
  return TITLES[s];
}

const PRIMARY: Screen[] = ["agents", "inbox", "board"];
const GROUPS: { label: string; items: Screen[] }[] = [
  { label: "Work", items: ["agents", "inbox", "board"] },
  { label: "Autonomy", items: ["changes", "schedules", "services"] },
  { label: "Knowledge", items: ["memory"] },
  { label: "Observe", items: ["usage", "health"] },
];
const MORE: Screen[] = ["changes", "schedules", "services", "memory", "usage", "health", "settings"];

function countFor(s: Screen, counts: Counts): number {
  if (s === "inbox") return counts.inbox ?? 0;
  if (s === "changes") return counts.changes ?? 0;
  if (s === "services") return counts.services ?? 0;
  return 0;
}

export function go(e: React.MouseEvent, path: string) {
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return;
  e.preventDefault();
  navigate(path);
}

/** The screen's name at the top, with the one or two actions that belong to it. */
export function PageHeader({ title, subtitle, actions, back, children }: { title: ReactNode; subtitle?: ReactNode; actions?: ReactNode; back?: string; children?: ReactNode }) {
  return (
    <header className="pagehead">
      <div className="pagehead-row">
        {back && (
          <a className="iconbtn" href={back} onClick={(e) => go(e, back)} aria-label="Back" title="Back">
            <Icon name="back" />
          </a>
        )}
        <div className="pagehead-title">
          <h1>{title}</h1>
          {subtitle && <div className="sub">{subtitle}</div>}
        </div>
        {actions && <div className="pagehead-actions">{actions}</div>}
      </div>
      {children}
    </header>
  );
}

export function TabBar({ screen, counts, onMore, moreOpen }: { screen: Screen; counts: Counts; onMore: () => void; moreOpen: boolean }) {
  const inMore = MORE.includes(screen);
  const moreCount = MORE.reduce((n, s) => n + countFor(s, counts), 0);
  return (
    <nav className="tabbar" aria-label="Primary">
      {PRIMARY.map((s) => {
        const n = countFor(s, counts);
        return (
          <a key={s} href={pathFor(s)} className={screen === s && !moreOpen ? "active" : ""} aria-current={screen === s ? "page" : undefined} onClick={(e) => go(e, pathFor(s))}>
            <span className="glyph">
              <Icon name={ICONS[s]} size={22} />
              {n > 0 && <span className="tab-badge">{n > 99 ? "99+" : n}</span>}
            </span>
            {TITLES[s]}
          </a>
        );
      })}
      <button className={inMore || moreOpen ? "active" : ""} onClick={onMore} aria-haspopup="dialog" aria-expanded={moreOpen}>
        <span className="glyph">
          <Icon name={inMore ? ICONS[screen] : "more"} size={22} />
          {moreCount > 0 && <span className="tab-badge dot" aria-label={`${moreCount} waiting`} />}
        </span>
        {inMore ? TITLES[screen] : "More"}
      </button>
    </nav>
  );
}

export function MoreSheet({ screen, counts, onClose }: { screen: Screen; counts: Counts; onClose: () => void }) {
  return (
    <Sheet onClose={onClose} size="narrow" className="more-sheet" ariaLabel="More">
      <div className="more-grid">
        {MORE.map((s) => {
          const n = countFor(s, counts);
          return (
            <a key={s} href={pathFor(s)} className={`more-item ${screen === s ? "active" : ""}`} onClick={(e) => { go(e, pathFor(s)); onClose(); }}>
              <Icon name={ICONS[s]} size={22} />
              <span>{TITLES[s]}</span>
              {n > 0 && <span className="tab-badge">{n}</span>}
            </a>
          );
        })}
      </div>
    </Sheet>
  );
}

export function Rail({ screen, counts, collapsed, onToggle, onPalette }: { screen: Screen; counts: Counts; collapsed: boolean; onToggle: () => void; onPalette: () => void }) {
  const item = (s: Screen) => {
    const n = countFor(s, counts);
    return (
      <a key={s} href={pathFor(s)} className={`rail-item ${screen === s ? "active" : ""}`} aria-current={screen === s ? "page" : undefined} onClick={(e) => go(e, pathFor(s))} title={collapsed ? TITLES[s] : undefined}>
        <Icon name={ICONS[s]} size={18} />
        <span className="rail-text">{TITLES[s]}</span>
        {n > 0 && <span className={`count ${s === "services" ? "ok" : s === "changes" ? "attn" : ""}`}>{n}</span>}
      </a>
    );
  };
  return (
    <nav className={`rail ${collapsed ? "collapsed" : ""}`} aria-label="Primary">
      <a className="brand" href={pathFor("agents")} onClick={(e) => go(e, pathFor("agents"))} title="Daedalus">
        <img src="/app/icons/icon-192.png" alt="" width={26} height={26} />
        <span className="rail-text">Daedalus</span>
      </a>
      <button className="rail-item search" onClick={onPalette} title="Search and go (Ctrl/⌘ K)">
        <Icon name="search" size={18} />
        <span className="rail-text">Search…</span>
        <kbd className="rail-text">⌘K</kbd>
      </button>
      {GROUPS.map((g) => (
        <div key={g.label} className="rail-group">
          <div className="rail-label">{g.label}</div>
          {g.items.map(item)}
        </div>
      ))}
      <div className="rail-group bottom">
        {item("settings")}
        <button className="rail-item collapse" onClick={onToggle} title={collapsed ? "Expand the rail" : "Collapse the rail"} aria-label={collapsed ? "Expand the rail" : "Collapse the rail"} aria-expanded={!collapsed}>
          <Icon name={collapsed ? "columns" : "back"} size={18} />
          <span className="rail-text">Collapse</span>
        </button>
      </div>
    </nav>
  );
}

// ── command palette ──────────────────────────────────────────────────────────────────────

export type PaletteItem = { id: string; label: string; hint?: string; icon: IconName; run: () => void };

/** Ctrl/⌘ K: go somewhere by name — a screen, a session, an action. */
export function Palette({ items, onClose }: { items: PaletteItem[]; onClose: () => void }) {
  const [q, setQ] = useState("");
  const [cursor, setCursor] = useState(0);
  const needle = q.trim().toLowerCase();
  const shown = (needle ? items.filter((it) => `${it.label} ${it.hint ?? ""}`.toLowerCase().includes(needle)) : items).slice(0, 12);
  useEffect(() => setCursor(0), [needle]);
  const run = (it: PaletteItem) => {
    onClose();
    it.run();
  };
  return (
    <Sheet ariaLabel="Search and go" onClose={onClose} size="narrow" className="palette-sheet">
      <input
        className="field"
        autoFocus
        placeholder="Go to, open, create…"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") {
            setCursor((c) => Math.min(shown.length - 1, c + 1));
            e.preventDefault();
          } else if (e.key === "ArrowUp") {
            setCursor((c) => Math.max(0, c - 1));
            e.preventDefault();
          } else if (e.key === "Enter" && shown[cursor]) run(shown[cursor]);
        }}
        aria-label="Search and go"
      />
      <div className="palette-list" role="listbox">
        {shown.map((it, i) => (
          <button key={it.id} role="option" aria-selected={i === cursor} className={`palette-row ${i === cursor ? "on" : ""}`} onMouseEnter={() => setCursor(i)} onClick={() => run(it)}>
            <Icon name={it.icon} size={16} />
            <span className="truncate">{it.label}</span>
            {it.hint && <span className="sub truncate">{it.hint}</span>}
          </button>
        ))}
        {shown.length === 0 && <div className="sub" style={{ padding: "10px 12px" }}>Nothing matches.</div>}
      </div>
    </Sheet>
  );
}

const GO_KEYS: Record<string, Screen> = { a: "agents", i: "inbox", b: "board", c: "changes", m: "memory", u: "usage", s: "settings" };

/** Keyboard on a desktop: Ctrl/⌘ K opens the palette, `g` then a letter goes to a screen. Never inside a text field. */
export function useShortcuts(onPalette: () => void) {
  useEffect(() => {
    let pendingG = 0;
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      const typing = !!t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        onPalette();
        return;
      }
      if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "g") {
        pendingG = Date.now();
        return;
      }
      if (pendingG && Date.now() - pendingG < 1200 && GO_KEYS[e.key]) {
        e.preventDefault();
        navigate(pathFor(GO_KEYS[e.key]));
      }
      pendingG = 0;
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onPalette]);
}

/** Whether a media query matches, kept current as the window changes. */
export function useMedia(query: string): boolean {
  const [on, setOn] = useState(() => window.matchMedia?.(query).matches ?? false);
  useEffect(() => {
    const mq = window.matchMedia?.(query);
    if (!mq) return;
    const update = () => setOn(mq.matches);
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, [query]);
  return on;
}
