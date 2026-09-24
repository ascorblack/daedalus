// The left column on a desktop: the sessions, grouped by project, with the project switcher, search
// and New at the top and the menu button at the bottom. It is the one column beside the
// conversation — the destinations live in the menu that opens over the content, not in a second
// column. Folded, it is a 48 px strip: the same controls as icons, and a dot for every agent that is
// working or waiting.

import { Suspense, lazy, useCallback, useEffect, useState, type RefObject } from "react";
import { SessionList, Project } from "./api";
import { Dot } from "./components";
import { Icon } from "./icons";
import { PaneHandle } from "./layout";
import { ProjectChip } from "./projects";
import { Screen, navigate, pathFor, sessionPath } from "./router";
import { SelfDevMode } from "./capabilities";
import { Counts, go } from "./shell";
import { useQuery } from "./store";
import { useStreamUp } from "./events";
import { t } from "./i18n";
import { agentName } from "./grouping";

const SessionsScreen = lazy(() => import("./screens/Sessions").then((m) => ({ default: m.SessionsScreen })));

export type SidebarProps = {
  screen: Screen;
  session: string | null;
  counts: Counts;
  selfdev: SelfDevMode;
  collapsed: boolean;
  /** Folds the column. The agents screen is the start canvas, so this column is the list of chats. */
  onToggle?: () => void;
  width: number;
  onWidth: (w: number) => void;
  onPalette: () => void;
  projects: Project[];
  project: string;
  onProjects: () => void;
  onOpen: (id: string) => void;
  toast: (t: string) => void;
  menuOpen: boolean;
  onMenu: () => void;
  menuButton: RefObject<HTMLButtonElement | null>;
};

export function Sidebar(p: SidebarProps) {
  const attention = (p.counts.inbox ?? 0) + (p.counts.changes ?? 0) > 0;
  const toggle = p.onToggle && (
    <button className="iconbtn quiet" onClick={p.onToggle} title={t(p.collapsed ? "shell.sidebar.expand" : "shell.sidebar.collapse")} aria-label={t(p.collapsed ? "shell.sidebar.expand" : "shell.sidebar.collapse")} aria-expanded={!p.collapsed}>
      <Icon name="columns" size={18} />
    </button>
  );
  const menu = (
    <button ref={p.menuButton} className={`sidebar-menu ${p.collapsed ? "iconbtn quiet" : ""}`} onClick={p.onMenu} title={t("nav.menu.title")} aria-label={t("nav.menu")} aria-haspopup="menu" aria-expanded={p.menuOpen}>
      <Icon name="more" size={18} />
      {!p.collapsed && <span className="sidebar-text">{t("nav.menu")}</span>}
      {!p.collapsed && <kbd>⌘⇧M</kbd>}
      {attention && <span className="badge-dot" aria-hidden />}
    </button>
  );
  if (p.collapsed) {
    return (
      <nav className="sidebar collapsed" aria-label={t("shell.sidebar.label")}>
        <div className="sidebar-strip">
          <a className="brand" href={pathFor("agents")} onClick={(e) => go(e, pathFor("agents"))} title="Daedalus">
            <img src="/app/icons/icon-192.png" alt="" width={24} height={24} />
          </a>
          {toggle}
          <ProjectChip projects={p.projects} current={p.project} onOpen={p.onProjects} collapsed />
          <button className="iconbtn quiet" onClick={p.onPalette} title={t("shell.search.title")} aria-label={t("shell.search.label")}><Icon name="search" size={18} /></button>
          <button className="iconbtn quiet" onClick={() => navigate(pathFor("agents", null, { new: "1" }))} title={t("agents.new")} aria-label={t("agents.new")}><Icon name="plus" size={18} /></button>
          <LiveDots current={p.session} onOpen={p.onOpen} />
        </div>
        <div className="sidebar-foot">{menu}</div>
      </nav>
    );
  }
  return (
    <nav className="sidebar" aria-label={t("shell.sidebar.label")}>
      <div className="sidebar-brand">
        <a className="brand" href={pathFor("agents")} onClick={(e) => go(e, pathFor("agents"))} title="Daedalus">
          <img src="/app/icons/icon-192.png" alt="" width={24} height={24} />
          <span className="sidebar-text">Daedalus</span>
        </a>
        {toggle}
      </div>
      <div className="sidebar-body">
        <Suspense fallback={null}>
          <SessionsScreen onOpen={p.onOpen} toast={p.toast} current={p.session ?? undefined} compact project={p.project} projects={p.projects} onProjects={p.onProjects} />
        </Suspense>
      </div>
      <div className="sidebar-foot">{menu}</div>
      <PaneHandle side="right" onDrag={(dx) => p.onWidth(p.width + dx)} />
    </nav>
  );
}

/** The strip's view of the list: one dot per agent that is working or waiting, the open one marked. */
function LiveDots({ current, onOpen }: { current: string | null; onOpen: (id: string) => void }) {
  // Every change of state arrives as an event while the stream is up; the slow poll is the net.
  const streaming = useStreamUp();
  const { data } = useQuery<SessionList>("/api/sessions", { pollMs: streaming ? 60000 : 5000, staleMs: 3000 });
  const live = (data?.sessions ?? []).filter((s) => s.status === "running" || s.status === "waiting").slice(0, 8);
  if (live.length === 0) return null;
  return (
    <div className="strip-dots" role="list">
      {live.map((s) => (
        <a key={s.id} role="listitem" className={`strip-dot ${s.id === current ? "current" : ""}`} href={sessionPath(s.id)} onClick={(e) => { if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return; e.preventDefault(); onOpen(s.id); }} title={`${agentName(s)} · ${s.status}`}>
          <Dot status={s.status} />
        </a>
      ))}
    </div>
  );
}

/** The sidebar's collapse, with the window's width as the default until the operator chooses. */
export function useSidebar(read: (w: number) => boolean, remember: (c: boolean) => void): [boolean, () => void] {
  const [collapsed, setCollapsed] = useState(() => read(window.innerWidth));
  const toggle = useCallback(() => {
    setCollapsed((c) => {
      remember(!c);
      return !c;
    });
  }, [remember]);
  // A choice never made follows the window: a laptop lid at 1100 px gets the strip, docked at 1440 the column.
  useEffect(() => {
    const on = () => setCollapsed(read(window.innerWidth));
    window.addEventListener("resize", on);
    return () => window.removeEventListener("resize", on);
  }, [read]);
  return [collapsed, toggle];
}
