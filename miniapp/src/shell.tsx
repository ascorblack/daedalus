// The application shell: a page header with the screen's name and its actions, four tabs and a
// More sheet on a phone, a rail with grouped destinations on a desktop.

import type { ReactNode } from "react";
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
    <Sheet onClose={onClose} size="narrow" className="more-sheet">
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

export function Rail({ screen, counts }: { screen: Screen; counts: Counts }) {
  const item = (s: Screen) => {
    const n = countFor(s, counts);
    return (
      <a key={s} href={pathFor(s)} className={`rail-item ${screen === s ? "active" : ""}`} aria-current={screen === s ? "page" : undefined} onClick={(e) => go(e, pathFor(s))}>
        <Icon name={ICONS[s]} size={18} />
        <span>{TITLES[s]}</span>
        {n > 0 && <span className={`count ${s === "services" ? "ok" : s === "changes" ? "attn" : ""}`}>{n}</span>}
      </a>
    );
  };
  return (
    <nav className="rail" aria-label="Primary">
      <a className="brand" href={pathFor("agents")} onClick={(e) => go(e, pathFor("agents"))}>
        <img src="/app/icons/icon-192.png" alt="" width={26} height={26} />
        Daedalus
      </a>
      {GROUPS.map((g) => (
        <div key={g.label} className="rail-group">
          <div className="rail-label">{g.label}</div>
          {g.items.map(item)}
        </div>
      ))}
      <div className="rail-group bottom">{item("settings")}</div>
    </nav>
  );
}
