import { Fragment, useCallback, useMemo, useState } from "react";
import { api, Project, SessionSummary, Settings, Workspace } from "../api";
import { Avatar, Dot, Skeleton, Status, ToolPicker, fmtInterval, statusWord } from "../components";
import { Sheet } from "../dialogs";
import { relTime, shortModel, untilShort } from "../format";
import { Icon } from "../icons";
import { FilePreview, PreviewSource, workspaceBase } from "../preview";
import { useProjects } from "../projects";
import { PageHeader, screenTitle } from "../shell";
import { useQuery } from "../store";
import { confirmAsync, errorText, fmtBytes } from "../ui";
import { plural, t } from "../i18n";
import { Files } from "./Session";

type Filter = "all" | "working" | "loops";
type GroupBy = "status" | "workspace" | "project";

const OWN = "\u0000own";  // sessions in a directory of their own: one group, not one group each
const NO_PROJECT = "\u0000none";  // agents that belong to no project: one group at the end

export function SessionsScreen({ onOpen, toast, current, compact, project = "", projects = [], onProjects }: { onOpen: (id: string) => void; toast: (t: string) => void; current?: string; compact?: boolean; project?: string; projects?: Project[]; onProjects?: () => void }) {
  const { data: sessions, error, loading } = useQuery<SessionSummary[]>("/api/sessions", { pollMs: 5000, staleMs: 3000 });
  const [creating, setCreating] = useState(() => new URLSearchParams(window.location.search).get("new") === "1");
  const [showWorkspaces, setShowWorkspaces] = useState(false);
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [filter, setFilter] = useState<Filter>("all");
  const [groupBy, setGroupBy] = useState<GroupBy>(() => (localStorage.getItem("agents.groupBy") as GroupBy) || "status");
  const inProject = projects.find((p) => p.id === project);
  const setGrouping = (g: GroupBy) => { setGroupBy(g); localStorage.setItem("agents.groupBy", g); };

  // Subagents sit under their leader; a child whose leader is gone is listed on its own.
  // The project the shell is showing is a lens over the whole list: everything below counts and
  // groups what is left, so "3 agents · 1 active" describes the project and not the installation.
  const all = (sessions ?? []).filter((s) => !project || s.project_id === project);
  const ids = new Set(all.map((s) => s.id));
  const children = new Map<string, SessionSummary[]>();
  for (const s of all) {
    const leader = s.metadata?.subagent_of;
    if (leader && ids.has(leader)) children.set(leader, [...(children.get(leader) ?? []), s]);
  }
  // A fork sits under the session it was taken from: it has its own directory (a copy of the source's
  // files as of the fork point), so grouping by workspace would put it elsewhere, and the kinship is what
  // the reader is looking for. A fork whose origin is gone is listed on its own.
  const forks = new Map<string, SessionSummary[]>();
  for (const s of all) {
    const origin = s.metadata?.forked_from?.session_id;
    if (origin && origin !== s.id && ids.has(origin) && !s.metadata?.subagent_of) forks.set(origin, [...(forks.get(origin) ?? []), s]);
  }
  const isNestedFork = (s: SessionSummary) => !!s.metadata?.forked_from?.session_id && ids.has(s.metadata.forked_from.session_id) && !s.metadata?.subagent_of;
  const q = query.trim().toLowerCase();
  const matchesSelf = (s: SessionSummary) => !q || s.title.toLowerCase().includes(q) || (s.model ?? "").toLowerCase().includes(q) || (children.get(s.id) ?? []).some((c) => (c.metadata?.subagent_name ?? c.title).toLowerCase().includes(q));
  const matches = (s: SessionSummary) => matchesSelf(s) || (forks.get(s.id) ?? []).some(matchesSelf);
  const top = all.filter((s) => !(s.metadata?.subagent_of && ids.has(s.metadata.subagent_of)) && !isNestedFork(s)).filter(matches);
  const childRunning = (s: SessionSummary) => (children.get(s.id) ?? []).some((c) => c.status === "running" || c.status === "waiting");
  const kind = (s: SessionSummary): "waiting" | "working" | "loop" | "idle" => {
    if (s.status === "waiting") return "waiting";
    if (s.status === "running" || childRunning(s)) return "working";
    if (s.metadata?.loop && s.metadata.loop.status === "active") return "loop";
    return "idle";
  };
  const kept = top.filter((s) => (filter === "all" ? true : filter === "working" ? kind(s) === "waiting" || kind(s) === "working" : kind(s) === "loop"));
  const byStatus: { key: string; label: string; items: SessionSummary[] }[] = (["waiting", "working", "loop", "idle"] as const).map((k) => ({
    key: k,
    label: t(`agents.group.${k}`),
    items: kept.filter((s) => kind(s) === k),
  }));
  // By workspace the shared directories come first (a project with its agents in it); everything that
  // works in a directory of its own goes into one group at the end, because one agent is not a project.
  // A directory is shared when more than one top-level agent works in it — the one it was made for
  // included: an agent whose own directory another agent joined belongs to that group, not to "own".
  const tenants = new Map<string, number>();
  for (const s of top) if (s.workspace) tenants.set(s.workspace, (tenants.get(s.workspace) ?? 0) + 1);
  const titleOf = new Map(all.map((s) => [s.id, s.title]));
  const buckets = new Map<string, SessionSummary[]>();
  for (const s of kept) {
    const key = s.workspace && (tenants.get(s.workspace) ?? 0) > 1 ? s.workspace : OWN;
    buckets.set(key, [...(buckets.get(key) ?? []), s]);
  }
  const byWorkspace = [...buckets.entries()]
    .sort((a, b) => (a[0] === OWN ? 1 : b[0] === OWN ? -1 : a[0].localeCompare(b[0])))
    .map(([key, items]) => ({ key, label: key === OWN ? t("agents.group.own") : titleOf.get(key) ?? key, items }));
  // By project the named ones come first and everything without a project goes into one group at the
  // end — an agent in a directory of its own is not a project, and there is nothing to call its group.
  const projectName = new Map(projects.map((p) => [p.id, p.name]));
  const byProjectBuckets = new Map<string, SessionSummary[]>();
  for (const s of kept) {
    const key = s.project_id && projectName.has(s.project_id) ? s.project_id : NO_PROJECT;
    byProjectBuckets.set(key, [...(byProjectBuckets.get(key) ?? []), s]);
  }
  const byProject = [...byProjectBuckets.entries()]
    .sort((a, b) => (a[0] === NO_PROJECT ? 1 : b[0] === NO_PROJECT ? -1 : (projectName.get(a[0]) ?? "").localeCompare(projectName.get(b[0]) ?? "")))
    .map(([key, items]) => ({ key, label: key === NO_PROJECT ? t("agents.group.noproject") : projectName.get(key) ?? key, items }));
  const groups = groupBy === "project" ? byProject : groupBy === "workspace" ? byWorkspace : byStatus;
  const activeCount = top.filter((s) => kind(s) === "waiting" || kind(s) === "working").length;
  const shown = groups.reduce((n, g) => n + g.items.length, 0);

  return (
    <>
      <PageHeader
        title={inProject ? inProject.name : screenTitle("agents")}
        subtitle={sessions ? `${plural("agents.count", top.length)}${activeCount ? ` · ${t("agents.active", { n: activeCount })}` : ""}${inProject ? ` · ${inProject.root}` : ""}` : undefined}
        actions={
          <>
            {/* Not a folder glyph: Workspaces sits beside it with one, and two identical icons next to each other name nothing. */}
            {!compact && onProjects && <button className="iconbtn" onClick={onProjects} title={t("shell.projects")} aria-label={t("shell.projects")}><Icon name="skill" /></button>}
            <button className={`iconbtn ${searching ? "on" : ""}`} onClick={() => { setSearching((v) => !v); if (searching) setQuery(""); }} title={t("common.search")} aria-label={t("common.search")} aria-pressed={searching}><Icon name="search" /></button>
            {!compact && <button className="iconbtn" onClick={() => setShowWorkspaces(true)} title={t("agents.workspaces")} aria-label={t("agents.workspaces")}><Icon name="folder" /></button>}
            <button className="iconbtn primary" onClick={() => setCreating(true)} title={t("agents.new")} aria-label={t("agents.new")}><Icon name="plus" /></button>
          </>
        }
      >
        {searching && <input className="field search" autoFocus placeholder={t("agents.search")} value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => { if (e.key === "Escape") { setQuery(""); setSearching(false); } }} aria-label={t("agents.search.label")} />}
        {!compact && <div className="chips">
          {(["all", "working", "loops"] as Filter[]).map((f) => (
            <button key={f} className="chip select" aria-pressed={filter === f} onClick={() => setFilter(f)}>
              {f === "all" ? t("agents.filter.all", { n: top.length }) : f === "working" ? t("agents.filter.working", { n: activeCount }) : t("agents.filter.loops", { n: top.filter((s) => kind(s) === "loop").length })}
            </button>
          ))}
          {!project && projects.length > 0 && (
            <button className="chip select" aria-pressed={groupBy === "project"} onClick={() => setGrouping(groupBy === "project" ? "status" : "project")} title={t("agents.group.project.title")}>
              <Icon name="folder" size={13} /> {t("agents.group.project")}
            </button>
          )}
          <button className="chip select" aria-pressed={groupBy === "workspace"} onClick={() => setGrouping(groupBy === "workspace" ? "status" : "workspace")} title={t("agents.group.workspace.title")}>
            <Icon name="folder" size={13} /> {t("agents.group.workspace")}
          </button>
        </div>}
      </PageHeader>
      <div className="screen narrow">
        {loading && !error && <Skeleton rows={5} />}
        {error && !sessions && <div className="empty"><b>{t("agents.error")}</b><div>{error}</div></div>}
        {sessions && all.length === 0 && (
          <div className="empty">
            <b>{inProject ? t("agents.empty.project", { name: inProject.name }) : t("agents.empty")}</b>
            <div>{inProject ? t("agents.empty.project.sub", { root: inProject.root }) : t("agents.empty.sub")}</div>
            <button className="btn primary" onClick={() => setCreating(true)}>{t("agents.empty.create")}</button>
          </div>
        )}
        {sessions && all.length > 0 && shown === 0 && <div className="empty">{t("common.nothing")}</div>}
        {groups.map((g) =>
          g.items.length === 0 ? null : (
            <section key={g.key} className="erow-group">
              <div className="section-title">
                {((groupBy === "workspace" && g.key !== OWN) || (groupBy === "project" && g.key !== NO_PROJECT)) && <Icon name="folder" size={13} />} {g.label} <span className="n">{g.items.length}</span>
              </div>
              {g.items.map((s) => (
                <Fragment key={s.id}>
                  <Row s={s} kids={children.get(s.id) ?? []} onOpen={onOpen} current={current === s.id || (children.get(s.id) ?? []).some((c) => c.id === current)} />
                  {(forks.get(s.id) ?? []).filter((f) => !q || matchesSelf(f)).map((f) => (
                    <Row key={f.id} s={f} kids={children.get(f.id) ?? []} onOpen={onOpen} current={current === f.id || (children.get(f.id) ?? []).some((c) => c.id === current)} fork={{ of: s.title, seq: f.metadata!.forked_from!.seq }} />
                  ))}
                </Fragment>
              ))}
            </section>
          ),
        )}
      </div>
      {creating && <NewAgentSheet onClose={() => setCreating(false)} onCreated={onOpen} toast={toast} project={project} />}
      {showWorkspaces && (
        <Sheet title={t("agents.workspaces")} onClose={() => setShowWorkspaces(false)}>
          <WorkspacesPanel onOpen={(id) => { setShowWorkspaces(false); onOpen(id); }} toast={toast} />
        </Sheet>
      )}
    </>
  );
}

/** "Loop every 40m · run #4 · next in 12m", or the reason it is paused. */
function loopLine(s: SessionSummary): string {
  const loop = s.metadata?.loop;
  if (!loop) return "";
  const cadence = loop.mode === "interval" ? t("loop.every", { t: fmtInterval(loop.interval_seconds) }) : t("loop.selfpaced");
  const runs = `${t("agents.loop.runs", { n: loop.run_count })}${loop.max_runs ? `/${loop.max_runs}` : ""}`;
  if (loop.status === "active") return `${t("agents.loop.line", { cadence, runs })}${loop.next_run_at ? t("agents.loop.next", { t: untilShort(loop.next_run_at) }) : ""}`;
  const why = loop.pause_note || loop.stop_reason || "";
  return `${t("agents.loop.stopped", { status: statusWord(loop.status).toLowerCase() })}${why ? `: ${why.slice(0, 80)}` : ""} · ${runs}`;
}

function Row({ s, kids, onOpen, current, fork }: { s: SessionSummary; kids: SessionSummary[]; onOpen: (id: string) => void; current?: boolean; fork?: { of: string; seq: number } }) {
  const [showKids, setShowKids] = useState(false);
  const orphan = !!s.metadata?.subagent_of;
  const status = s.status as Status;
  const spoken = status === "running" || status === "waiting" || status === "failed" || status === "compacting";
  const loop = loopLine(s);
  const visibleKids = showKids ? kids : kids.slice(0, 3);
  const open = () => onOpen(s.id);
  return (
    <div className={`erow ${status} ${current ? "current" : ""} ${fork ? "fork" : ""}`} role="link" aria-current={current ? "page" : undefined} tabIndex={0} onClick={open} onKeyDown={(e) => { if (e.target !== e.currentTarget) return; if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } }}>
      <Avatar status={status} seed={s.id} />
      <div className="erow-main">
        <div className="erow-head">
          <span className="erow-title clamp-2">{orphan ? s.metadata?.subagent_name || s.title.replace(/^\[sub\]\s*/, "") : s.title}</span>
          <span className="erow-time num" title={new Date(s.last_message_at).toLocaleString()}>{relTime(s.last_message_at)}</span>
        </div>
        <div className={`erow-meta ${spoken ? status : ""}`}>
          {spoken && <Dot status={status} />}
          {spoken && <span className="word">{statusWord(status)}</span>}
          {spoken && s.model && <span className="sep">·</span>}
          {s.model && <span title={s.model}>{shortModel(s.model, 28)}</span>}
          {orphan && <span className="sep">·</span>}
          {orphan && <span>{t("agents.orphan")}</span>}
        </div>
        {fork && <div className="erow-meta"><Icon name="fork" size={12} /> <span title={t("agents.fork.of", { name: fork.of, n: fork.seq })}>{t("agents.fork.at", { n: fork.seq })}</span></div>}
        {loop && <div className={`erow-meta ${s.metadata?.loop?.status === "paused" ? "waiting" : ""}`}>{loop}</div>}
        {kids.length > 0 && (
          <div className="erow-children" onClick={(e) => e.stopPropagation()}>
            {visibleKids.map((c) => (
              <button key={c.id} className="subrow" onClick={() => onOpen(c.id)} title={t("agents.sub.open", { name: c.metadata?.subagent_name ?? c.title })}>
                <Dot status={c.status === "idle" ? "done" : c.status} />
                <span className="truncate">{c.metadata?.subagent_name || c.title.replace(/^\[sub\]\s*/, "")}</span>
                <span className="sub">{statusWord(c.status === "running" ? "running" : c.status === "waiting" ? "waiting" : c.status === "failed" ? "failed" : "done").toLowerCase()}</span>
              </button>
            ))}
            {kids.length > 3 && (
              <button className="subrow more" onClick={() => setShowKids((v) => !v)}>
                {showKids ? t("agents.sub.fewer") : plural("agents.sub.more", kids.length - 3)}
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/** The form for a new agent: a name, a first task and where it works; the loop and the tools sit behind Advanced. */
function NewAgentSheet({ onClose, onCreated, toast, project: initial = "" }: { onClose: () => void; onCreated: (id: string) => void; toast: (t: string) => void; project?: string }) {
  const [title, setTitle] = useState("");
  const [prompt, setPrompt] = useState("");
  const [toolsOff, setToolsOff] = useState<string[]>([]);
  const [loopOn, setLoopOn] = useState(false);
  const [loopText, setLoopText] = useState("");
  const [loopMode, setLoopMode] = useState<"interval" | "dynamic">("interval");
  const [loopMinutes, setLoopMinutes] = useState("10");
  const [loopMax, setLoopMax] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [project, setProject] = useState(initial);
  const [preset, setPreset] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [busy, setBusy] = useState(false);
  const workspaces = useQuery<Workspace[]>("/api/workspaces", { staleMs: 30000 });
  const projects = useProjects();
  const chosen = (projects.data ?? []).find((p) => p.id === project);
  const settings = useQuery<Settings>("/api/settings", { staleMs: 60000 });
  const presets = settings.data?.presets ?? {};
  const defaultPreset = settings.data?.model?.preset ?? "";
  const presetLabel = (id: string) => {
    const p = presets[id];
    return p ? p.label || `${p.provider}/${p.model}` : id;
  };

  async function create() {
    if (!title.trim() || busy) return;
    setBusy(true);
    try {
      const loop = loopOn && loopText.trim()
        ? { instruction: loopText.trim(), mode: loopMode, interval_minutes: loopMode === "interval" ? Math.max(1, Number(loopMinutes) || 10) : null, max_runs: loopMax.trim() ? Math.max(1, Number(loopMax) || 1) : null }
        : undefined;
      // A project and a workspace are two answers to the same question; the project wins, and the
      // workspace select is not shown while one is chosen.
      const created = await api.post<{ id: string }>("/api/sessions", { title: title.trim(), prompt: prompt.trim() || undefined, tools_off: toolsOff, loop, project_id: project || undefined, workspace: project ? undefined : workspace || undefined, preset: preset || undefined });
      onClose();
      onCreated(created.id);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  const wsLabel = (w: Workspace) =>
    `${w.own_session ? t("newagent.ws.session", { name: w.sessions.find((s) => s.id === w.name)?.title ?? w.name }) : w.name}` +
    `${w.sessions.length ? plural("newagent.ws.sessions", w.sessions.length) : t("newagent.ws.unused")}` +
    t("newagent.ws.files", { n: w.files });
  return (
    <Sheet title={t("newagent.title")} onClose={onClose}>
      <label className="field">{t("common.name")}</label>
      <input className="field" autoFocus value={title} onChange={(e) => setTitle(e.target.value)} placeholder={t("newagent.name.placeholder")} onKeyDown={(e) => e.key === "Enter" && create()} />
      <label className="field">{t("newagent.first")}</label>
      <textarea className="field" rows={3} value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder={t("newagent.first.placeholder")} />
      <label className="field">{t("newagent.model")}</label>
      <select className="field" value={preset} onChange={(e) => setPreset(e.target.value)}>
        <option value="">{t("newagent.model.default")}{defaultPreset ? ` · ${presetLabel(defaultPreset)}` : ""}</option>
        {Object.keys(presets).map((id) => (
          <option key={id} value={id}>{presetLabel(id)}</option>
        ))}
      </select>
      <label className="field">{t("newagent.where")}</label>
      <select className="field" value={project} onChange={(e) => setProject(e.target.value)}>
        <option value="">{t("newagent.where.none")}</option>
        {(projects.data ?? []).map((p) => (
          <option key={p.id} value={p.id} disabled={!p.reachable}>
            {p.name} · {p.root}{p.reachable ? "" : t("newagent.where.unmounted")}
          </option>
        ))}
      </select>
      {chosen && <div className="sub">{t("newagent.where.inside", { root: chosen.root })}</div>}
      {!project && (
        <>
          <label className="field">{t("newagent.workspace")}</label>
          <select className="field" value={workspace} onChange={(e) => setWorkspace(e.target.value)}>
            <option value="">{t("newagent.workspace.own")}</option>
            {(workspaces.data ?? []).map((w) => (
              <option key={w.name} value={w.name}>{wsLabel(w)}</option>
            ))}
          </select>
        </>
      )}
      <button type="button" className="disclosure" onClick={() => setAdvanced((v) => !v)} aria-expanded={advanced}>
        <span className={`chev ${advanced ? "down" : ""}`}>›</span> {t("newagent.advanced")}{loopOn ? t("newagent.advanced.loop") : ""}{toolsOff.length ? t("newagent.advanced.tools", { n: toolsOff.length }) : ""}
      </button>
      {advanced && (
        <div className="disclosure-body">
          <div className="sub">{t("newagent.shared")}</div>
          <label className="toggle-row">
            <input type="checkbox" checked={loopOn} onChange={(e) => setLoopOn(e.target.checked)} />
            <span>{t("newagent.loop")}</span>
            <span className="sub">{t("newagent.loop.hint")}</span>
          </label>
          {loopOn && (
            <div className="loop-form">
              <label className="field">{t("newagent.loop.label")}</label>
              <textarea className="field" rows={3} value={loopText} onChange={(e) => setLoopText(e.target.value)} placeholder={t("newagent.loop.placeholder")} />
              <div className="composer-row">
                <select className="field" value={loopMode} onChange={(e) => setLoopMode(e.target.value as "interval" | "dynamic")}>
                  <option value="interval">{t("newagent.loop.interval")}</option>
                  <option value="dynamic">{t("loop.selfpaced")}</option>
                </select>
                {loopMode === "interval" && <input className="field" type="number" min={1} style={{ maxWidth: 110 }} value={loopMinutes} onChange={(e) => setLoopMinutes(e.target.value)} aria-label={t("newagent.loop.minutes")} />}
                <input className="field" type="number" min={1} style={{ maxWidth: 130 }} placeholder={t("newagent.loop.max")} value={loopMax} onChange={(e) => setLoopMax(e.target.value)} aria-label={t("newagent.loop.max")} />
              </div>
              <div className="sub">{t("newagent.loop.note")}</div>
            </div>
          )}
          <ToolPicker off={toolsOff} onChange={setToolsOff} note={t("newagent.tools.note")} />
        </div>
      )}
      <div className="sheet-foot">
        <button className="btn ghost" onClick={onClose}>{t("common.cancel")}</button>
        <button className="btn primary" onClick={create} disabled={busy || !title.trim() || (loopOn && !loopText.trim())}>
          {t("common.create")}
        </button>
      </div>
    </Sheet>
  );
}

/** The directories under the workspaces root: who works in each, what is in it; make one, fill it, browse it, drop an unused one. */
function WorkspacesPanel({ onOpen, toast }: { onOpen: (id: string) => void; toast: (t: string) => void }) {
  const { data: workspaces, refresh } = useQuery<Workspace[]>("/api/workspaces", { staleMs: 0 });
  const [name, setName] = useState("");
  const [browsing, setBrowsing] = useState<Workspace | null>(null);
  const [preview, setPreview] = useState<PreviewSource | null>(null);
  async function create() {
    const n = name.trim();
    if (!n) return;
    try {
      await api.post("/api/workspaces", { name: n });
      toast(t("ws.created", { name: n }));
      setName("");
      refresh();
    } catch (e) {
      toast(errorText(e));
    }
  }
  async function remove(w: Workspace) {
    if (!(await confirmAsync(t("ws.delete.title", { name: w.name }), { body: plural("ws.delete.body", w.files), action: t("common.delete") }))) return;
    try {
      await api.delete(`/api/workspaces/${encodeURIComponent(w.name)}`);
      toast(t("ws.deleted"));
      refresh();
    } catch (e) {
      toast(errorText(e));
    }
  }
  const onClose = useCallback(() => setBrowsing(null), []);
  return (
    <div className="workspaces">
      <div className="composer-row" style={{ marginTop: 0, marginBottom: 8 }}>
        <input className="field" placeholder={t("ws.new.placeholder")} value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && create()} />
        <button className="btn primary" disabled={!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(name.trim())} onClick={create}>{t("common.create")}</button>
      </div>
      {!workspaces && <div className="sub">{t("common.loading")}</div>}
      {workspaces?.length === 0 && <div className="sub">{t("ws.empty")}</div>}
      {workspaces?.map((w) => (
        <div key={w.name} className="ws-row">
          <span aria-hidden>📁</span>
          <div className="grow" style={{ minWidth: 0 }}>
            <div className="ws-name">
              {w.name}
              {w.kind === "session" && <span className="badge" title={t("ws.badge.session.title")}>{t("ws.badge.session")}</span>}
              {w.kind === "schedule" && <span className="badge" title={t("ws.badge.schedule.title", { name: w.schedule ?? "" })}>{t("ws.badge.schedule", { name: w.schedule ?? "" })}</span>}
              {w.kind === "heartbeat" && <span className="badge">{t("ws.badge.heartbeat")}</span>}
              {w.kind === "named" && w.sessions.length === 0 && <span className="badge">{t("ws.badge.unused")}</span>}
            </div>
            <div className="sub ws-meta">
              {plural("ws.files", w.files)} · {fmtBytes(w.size)} · {relTime(w.mtime)}
              {w.sessions.map((s) => (
                <button key={s.id} className="linkbtn sub" onClick={() => onOpen(s.id)} title={t("ws.open.session")}> · {s.title}</button>
              ))}
            </div>
          </div>
          <button className="iconbtn small" onClick={() => setBrowsing(w)} title={t("ws.browse")} aria-label={t("ws.browse.label")}><Icon name="folder" size={15} /></button>
          {w.sessions.length === 0 && w.kind === "named" && <button className="iconbtn small" onClick={() => remove(w)} title={t("common.delete")} aria-label={t("common.delete")}><Icon name="trash" size={15} /></button>}
        </div>
      ))}
      {browsing && (
        <Sheet title={<><span aria-hidden>📁</span> {browsing.name}</>} ariaLabel={t("ws.sheet", { name: browsing.name })} onClose={onClose}>
          <Files base={workspaceBase(browsing.name)} uploadUrl={`${workspaceBase(browsing.name)}/upload`} onPreview={setPreview} toast={toast} />
        </Sheet>
      )}
      {preview && <FilePreview src={preview} onClose={() => setPreview(null)} />}
    </div>
  );
}

export function useSessionTitles(): Record<string, string> {
  const { data } = useQuery<SessionSummary[]>("/api/sessions", { staleMs: 15000 });
  return useMemo(() => Object.fromEntries((data ?? []).map((s) => [s.id, s.title])), [data]);
}

