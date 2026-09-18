import { Fragment, useCallback, useMemo, useState } from "react";
import { api, Project, ProjectFolder, SessionList, SessionSummary, Settings, Workspace } from "../api";
import { Avatar, Dot, Skeleton, Status, ToolPicker, fmtInterval, statusWord } from "../components";
import { Sheet } from "../dialogs";
import { relTime, shortModel, untilShort } from "../format";
import { Folder, FREE, Row as RowModel, agentName, arrange, folderOpen, rememberFolder } from "../grouping";
import { Icon } from "../icons";
import { FilePreview, PreviewSource, workspaceBase } from "../preview";
import { useProjects } from "../projects";
import { PageHeader, screenTitle } from "../shell";
import { useQuery } from "../store";
import { confirmAsync, errorText, fmtBytes } from "../ui";
import { plural, t } from "../i18n";
import { Files } from "./Session";

type Filter = "all" | "working" | "loops";

export function SessionsScreen({ onOpen, toast, current, compact, project = "", projects = [], onProjects }: { onOpen: (id: string) => void; toast: (t: string) => void; current?: string; compact?: boolean; project?: string; projects?: Project[]; onProjects?: () => void }) {
  const { data, error, loading } = useQuery<SessionList>("/api/sessions", { pollMs: 5000, staleMs: 3000 });
  const [creating, setCreating] = useState(() => new URLSearchParams(window.location.search).get("new") === "1");
  const [showWorkspaces, setShowWorkspaces] = useState(false);
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [filter, setFilter] = useState<Filter>("all");
  const inProject = projects.find((p) => p.id === project);

  // The arrangement is a pure function of what came back: folders for the projects, one bucket for
  // the agents that work in a directory of their own, and the nesting inside each. Memoised on the
  // three things that can change it, so typing in the search box does not re-walk a list that did not.
  const sessions = data?.sessions ?? [];
  const folders = useMemo(
    () => arrange(sessions, data?.projects ?? [], data?.free ?? { total: 0, active: 0, loops: 0, last_message_at: "" }, { project, filter, query }),
    [data, project, filter, query],
  );

  return (
    <>
      <PageHeader
        title={inProject ? inProject.name : screenTitle("agents")}
        subtitle={data ? `${plural("agents.count", folders.total)}${folders.active ? ` · ${t("agents.active", { n: folders.active })}` : ""}${inProject ? ` · ${inProject.root}` : ""}` : undefined}
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
        <div className="chips">
          {(["all", "working", "loops"] as Filter[]).map((f) => (
            <button key={f} className="chip select" aria-pressed={filter === f} onClick={() => setFilter(f)}>
              {f === "all" ? t("agents.filter.all", { n: folders.total }) : f === "working" ? t("agents.filter.working", { n: folders.active }) : t("agents.filter.loops", { n: folders.loops })}
            </button>
          ))}
        </div>
      </PageHeader>
      <div className="screen wide agents-screen">
        {loading && !error && <Skeleton rows={5} />}
        {error && !data && <div className="empty"><b>{t("agents.error")}</b><div>{error}</div></div>}
        {data && folders.total === 0 && (
          <div className="empty">
            <b>{inProject ? t("agents.empty.project", { name: inProject.name }) : t("agents.empty")}</b>
            <div>{inProject ? t("agents.empty.project.sub", { root: inProject.root }) : t("agents.empty.sub")}</div>
            <button className="btn primary" onClick={() => setCreating(true)}>{t("agents.empty.create")}</button>
          </div>
        )}
        {data && folders.total > 0 && folders.shown === 0 && <div className="empty">{t("common.nothing")}</div>}
        {folders.folders.map((f) => (
          <FolderSection key={f.key} folder={f} onOpen={onOpen} current={current} filtered={filter !== "all" || query.trim() !== ""} />
        ))}
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

/**
 * One folder: a project, or the bucket of agents that work in a directory of their own.
 *
 * Collapsed to its header until it is opened, and which folders are open is remembered per folder.
 * That is also what keeps the screen cheap on an installation with hundreds of agents: what is not
 * open is not in the DOM at all, so the list costs the folders and not the sessions.
 *
 * A search or a filter opens what it found: leaving a match behind a closed folder would be a screen
 * that answers "nothing" while holding the answer.
 */
function FolderSection({ folder, onOpen, current, filtered }: { folder: Folder; onOpen: (id: string) => void; current?: string; filtered: boolean }) {
  const [open, setOpen] = useState(() => folderOpen(folder.key));
  const free = folder.project === null;
  const showing = open || (filtered && folder.rows.length > 0);
  const toggle = () => {
    const next = !open;
    setOpen(next);
    rememberFolder(folder.key, next);
  };
  if (free && folder.rows.length === 0 && folder.total === 0) return null;
  return (
    <section className={`folder ${folder.system ? "system" : ""} ${showing ? "open" : ""}`}>
      <button className="folder-head" onClick={toggle} aria-expanded={showing}>
        <span className={`chev ${showing ? "down" : ""}`} aria-hidden>›</span>
        <Icon name={folder.system ? "mic" : free ? "bots" : "folder"} size={16} />
        <span className="folder-name truncate">{free ? t("agents.free") : folder.name}</span>
        {folder.active > 0 && <span className="folder-live" title={t("agents.active", { n: folder.active })}><Dot status="running" /></span>}
        <span className="folder-counts sub">
          {plural("agents.count", folder.total)}
          {folder.active > 0 && ` · ${t("agents.active", { n: folder.active })}`}
          {folder.loops > 0 && ` · ${t("agents.filter.loops", { n: folder.loops })}`}
        </span>
        {folder.last_message_at && <span className="folder-time sub num" title={new Date(folder.last_message_at).toLocaleString()}>{relTime(folder.last_message_at)}</span>}
      </button>
      {showing && folder.project && <div className="folder-root sub mono truncate" title={folder.project.root}>{folder.project.root}</div>}
      {showing && free && <div className="folder-root sub">{t("agents.free.hint")}</div>}
      {showing && folder.rows.length === 0 && <div className="folder-empty sub">{t("common.nothing")}</div>}
      {showing && folder.rows.map((r) => <RowTree key={r.s.id} row={r} onOpen={onOpen} current={current} free={free} />)}
    </section>
  );
}

/** An agent, the forks taken from it under it, and each fork's own forks under those. */
function RowTree({ row, onOpen, current, free, fork }: { row: RowModel; onOpen: (id: string) => void; current?: string; free: boolean; fork?: { of: string; seq: number } }) {
  const mine = current === row.s.id || row.kids.some((c) => c.id === current);
  return (
    <Fragment>
      <Row s={row.s} kids={row.kids} onOpen={onOpen} current={mine} fork={fork} free={free} />
      {row.forks.map((f) => (
        <RowTree key={f.s.id} row={f} onOpen={onOpen} current={current} free={free} fork={{ of: row.s.title, seq: f.s.metadata!.forked_from!.seq }} />
      ))}
    </Fragment>
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

function Row({ s, kids, onOpen, current, fork, free }: { s: SessionSummary; kids: SessionSummary[]; onOpen: (id: string) => void; current?: boolean; fork?: { of: string; seq: number }; free?: boolean }) {
  const [showKids, setShowKids] = useState(false);
  const orphan = !!s.metadata?.subagent_of;
  const status = s.status as Status;
  const spoken = status === "running" || status === "waiting" || status === "failed" || status === "compacting";
  const loop = loopLine(s);
  const visibleKids = showKids ? kids : kids.slice(0, 3);
  const open = () => onOpen(s.id);
  return (
    <div className={`erow ${status} ${current ? "current" : ""} ${fork ? "fork" : ""}`} title={s.workspace_path} role="link" aria-current={current ? "page" : undefined} tabIndex={0} onClick={open} onKeyDown={(e) => { if (e.target !== e.currentTarget) return; if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } }}>
      <Avatar status={status} seed={s.id} />
      <div className="erow-main">
        <div className="erow-head">
          <span className="erow-title clamp-2">{agentName(s)}</span>
          {/* The one thing a row in the free bucket has to say about itself: its folder is its own. */}
          {free && !fork && <span className="chip tiny" title={s.workspace_path}>{t("agents.own.chip")}</span>}
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
                <span className="truncate">{agentName(c)}</span>
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
        <option value="">{t("newagent.where.free")}</option>
        {(projects.data ?? []).map((p) => (
          <option key={p.id} value={p.id} disabled={!p.reachable}>
            {p.name} · {p.root}{p.reachable ? "" : t("newagent.where.unmounted")}
          </option>
        ))}
      </select>
      {chosen ? <div className="sub">{t("newagent.where.inside", { root: chosen.root })}</div> : <div className="sub">{t("newagent.where.free.hint")}</div>}
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
  const { data } = useQuery<SessionList>("/api/sessions", { staleMs: 15000 });
  return useMemo(() => Object.fromEntries((data?.sessions ?? []).map((s) => [s.id, s.title])), [data]);
}

