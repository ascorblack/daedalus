// Orchestration mode's own parts: the switch between the two modes, the left column that lists the
// main orchestrator and the projects with an orchestrator, and the same list as a page on a phone,
// where there is no left column. The rules — what belongs to which mode, what the switch counts —
// are in mode.ts; this file draws them.

import type { ReactNode } from "react";
import type { ProjectFolder, SessionList, SessionSummary } from "./api";
import { Bell } from "./bell";
import { Dot } from "./components";
import { relTime } from "./format";
import { plural, t } from "./i18n";
import { Icon } from "./icons";
import { PaneHandle } from "./layout";
import { MainEntry } from "./main/MainEntry";
import { useMain } from "./main/data";
import { Mode, modeHome, orchestratedProjects, waitingInOrchestration } from "./mode";
import { ORCHESTRATION, projectHome } from "./router";
import { PageHeader, go } from "./shell";
import { useQuery } from "./store";
import { useStreamUp } from "./events";

/** The listing both modes read: the Agents list filters it, orchestration mode picks its projects out
 *  of it. One key, so the sidebar, the switch and the strip share one request. */
function useListing() {
  const live = useStreamUp();
  return useQuery<SessionList>("/api/sessions", { pollMs: live ? 60000 : 5000, staleMs: 3000 });
}

/** How many requests wait for the operator in orchestration mode; the switch shows it from Agents. */
export function useOrchestrationWaiting(): number {
  const { data } = useListing();
  const { data: main } = useMain();
  return waitingInOrchestration(data?.projects ?? [], main);
}

/**
 * The switch between the two modes, at the top of the column (on a phone the tab bar carries the
 * same two destinations). From Agents, Orchestration carries one small count of what waits for the
 * operator there: the only trace of orchestration the Agents mode keeps, so a question is not missed
 * without the list filling with it. Folded, the switch is the one other mode's icon.
 */
export function ModeSwitch({ mode, strip = false }: { mode: Mode; strip?: boolean }) {
  const waiting = useOrchestrationWaiting();
  const count = mode === "agents" && waiting > 0 ? waiting : 0;
  const countLabel = count ? plural("mode.waiting", count) : "";
  if (strip) {
    const other: Mode = mode === "agents" ? "orchestration" : "agents";
    const label = t(`mode.to.${other}`);
    return (
      <a className="iconbtn quiet mode-strip" href={modeHome(other)} onClick={(e) => go(e, modeHome(other))} title={count ? `${label} · ${countLabel}` : label} aria-label={label} data-mode-switch={other}>
        <Icon name={other === "orchestration" ? "compass" : "bots"} size={18} />
        {count > 0 && <span className="mode-count strip" data-waiting={count} aria-hidden>{count > 99 ? "99+" : count}</span>}
      </a>
    );
  }
  // Words without icons: the column is 232 px at its narrowest, and "Оркестрация" with its count
  // needs the room an icon would take.
  const item = (m: Mode) => (
    <a className={`mode-tab ${mode === m ? "on" : ""}`} href={modeHome(m)} onClick={(e) => go(e, modeHome(m))} aria-current={mode === m ? "page" : undefined} data-mode={m}>
      <span className="truncate">{t(`mode.${m}`)}</span>
      {m === "orchestration" && count > 0 && <span className="mode-count" data-waiting={count} title={countLabel} aria-label={countLabel}>{count > 99 ? "99+" : count}</span>}
    </a>
  );
  return (
    <nav className="mode-switch" aria-label={t("mode.label")}>
      {item("agents")}
      {item("orchestration")}
    </nav>
  );
}

/** A project in orchestration mode's list: what its orchestrator is doing, the size of the team and
 *  how many at work, and how many requests wait for the operator. */
function ProjectRow({ project, sessions }: { project: ProjectFolder; sessions: SessionSummary[] }) {
  const o = project.orchestrator!;
  const href = projectHome(project.id);
  const orchestrator = sessions.find((s) => s.id === o.session_id);
  // The listing is a page of the most recent sessions; an orchestrator quiet for long may not be on
  // it, and then the project's own last activity says when it last moved.
  const state = project.setup_by === "dispatcher" ? t("orch.row.setup")
    : orchestrator?.status === "running" ? t("orch.row.working")
    : orchestrator?.status === "waiting" ? t("orch.row.waiting")
    : project.last_message_at ? relTime(project.last_message_at) : t("orch.row.idle");
  const meta = [state, plural("team.count.staff", o.staff), o.working > 0 ? plural("team.count.working", o.working) : ""].filter(Boolean).join(" · ");
  return (
    <a className="orch-row" href={href} onClick={(e) => go(e, href)} title={t("focus.entry.open", { name: project.name })} data-project={project.id}>
      <span className="orch-row-icon"><Icon name="conductor" size={15} /></span>
      <span className="orch-row-text">
        <span className="orch-row-name truncate">{project.name}</span>
        <span className="orch-row-meta truncate">{meta}</span>
      </span>
      {orchestrator?.status === "running" && <span className="folder-live"><Dot status="running" /></span>}
      {o.needs_you > 0 && <span className="needs-badge" title={plural("focus.entry.needs", o.needs_you)} aria-label={plural("focus.entry.needs", o.needs_you)}>{o.needs_you}</span>}
    </a>
  );
}

/** Main first, then every project with an orchestrator; the same in the column and on a phone's page.
 *  A project opened leaves this list for its own column (focus mode), so no row here is ever current. */
function OrchestrationRows({ onMain }: { onMain: boolean }) {
  const { data, loading } = useListing();
  const projects = orchestratedProjects(data?.projects ?? []);
  return (
    <>
      <div className="orch-main"><MainEntry current={onMain} /></div>
      <div className="orch-section sub">{t("orch.projects")}</div>
      {projects.map((p) => <ProjectRow key={p.id} project={p} sessions={data?.sessions ?? []} />)}
      {!loading && data && projects.length === 0 && <div className="orch-empty sub">{t("orch.empty")}</div>}
    </>
  );
}

export type OrchestrationSidebarProps = {
  /** The main chat is open. */
  onMain: boolean;
  collapsed: boolean;
  onToggle: () => void;
  width: number;
  onWidth: (w: number) => void;
  onPalette: () => void;
  menu: ReactNode;
};

/** The left column in orchestration mode. It keeps the Agents column's frame — the brand row with the
 *  bell, the menu at the foot, the drag handle — so switching modes changes the list and nothing around it. */
export function OrchestrationSidebar(p: OrchestrationSidebarProps) {
  const { data } = useListing();
  const toggle = (
    <button className="iconbtn quiet" onClick={p.onToggle} title={t(p.collapsed ? "shell.sidebar.expand" : "shell.sidebar.collapse")} aria-label={t(p.collapsed ? "shell.sidebar.expand" : "shell.sidebar.collapse")} aria-expanded={!p.collapsed}>
      <Icon name="columns" size={18} />
    </button>
  );
  if (p.collapsed) {
    const projects = orchestratedProjects(data?.projects ?? []).slice(0, 8);
    return (
      <nav className="sidebar collapsed orch-sidebar" aria-label={t("orch.label")}>
        <div className="sidebar-strip">
          <a className="brand" href={ORCHESTRATION} onClick={(e) => go(e, ORCHESTRATION)} title="Daedalus">
            <img src="/app/icons/icon-192.png" alt="" width={24} height={24} />
          </a>
          {toggle}
          <Bell />
          <ModeSwitch mode="orchestration" strip />
          <button className="iconbtn quiet" onClick={p.onPalette} title={t("shell.search.title")} aria-label={t("shell.search.label")}><Icon name="search" size={18} /></button>
          <MainEntry current={p.onMain} strip />
          <div className="strip-dots" role="list">
            {projects.map((project) => (
              <a key={project.id} role="listitem" className="iconbtn quiet strip-project" href={projectHome(project.id)} onClick={(e) => go(e, projectHome(project.id))} title={project.name} aria-label={t("focus.entry.open", { name: project.name })}>
                <Icon name="conductor" size={16} />
                {(project.orchestrator?.needs_you ?? 0) > 0 && <span className="badge-dot" aria-hidden />}
              </a>
            ))}
          </div>
        </div>
        <div className="sidebar-foot">{p.menu}</div>
      </nav>
    );
  }
  return (
    <nav className="sidebar orch-sidebar" aria-label={t("orch.label")}>
      <div className="sidebar-brand">
        <a className="brand" href={ORCHESTRATION} onClick={(e) => go(e, ORCHESTRATION)} title="Daedalus">
          <img src="/app/icons/icon-192.png" alt="" width={24} height={24} />
          <span className="sidebar-text">Daedalus</span>
        </a>
        <Bell />
        {toggle}
      </div>
      <div className="sidebar-mode"><ModeSwitch mode="orchestration" /></div>
      <div className="sidebar-body orch-body">
        <OrchestrationRows onMain={p.onMain} />
      </div>
      <div className="sidebar-foot">{p.menu}</div>
      <PaneHandle side="right" onDrag={(dx) => p.onWidth(p.width + dx)} />
    </nav>
  );
}

/** A phone's stand-in for the column: the main orchestrator and the projects, as a page. */
export function OrchestrationList() {
  return (
    <>
      <PageHeader title={t("mode.orchestration")} />
      <div className="screen orch-list">
        <OrchestrationRows onMain={false} />
      </div>
    </>
  );
}
