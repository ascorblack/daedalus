import { useCallback, useMemo, useState } from "react";
import { api, SessionSummary, Workspace } from "../api";
import { Avatar, Dot, STATUS_WORD, Skeleton, Status, ToolPicker, fmtInterval } from "../components";
import { Sheet } from "../dialogs";
import { relTime, shortModel, untilShort } from "../format";
import { Icon } from "../icons";
import { FilePreview, PreviewSource, workspaceBase } from "../preview";
import { PageHeader } from "../shell";
import { useQuery } from "../store";
import { confirmAsync, errorText, fmtBytes } from "../ui";
import { Files } from "./Session";

type Filter = "all" | "working" | "loops";

export function SessionsScreen({ onOpen, toast, current, compact }: { onOpen: (id: string) => void; toast: (t: string) => void; current?: string; compact?: boolean }) {
  const { data: sessions, error, loading } = useQuery<SessionSummary[]>("/api/sessions", { pollMs: 5000, staleMs: 3000 });
  const [creating, setCreating] = useState(false);
  const [showWorkspaces, setShowWorkspaces] = useState(false);
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [filter, setFilter] = useState<Filter>("all");

  // Subagents sit under their leader; a child whose leader is gone is listed on its own.
  const all = sessions ?? [];
  const ids = new Set(all.map((s) => s.id));
  const children = new Map<string, SessionSummary[]>();
  for (const s of all) {
    const leader = s.metadata?.subagent_of;
    if (leader && ids.has(leader)) children.set(leader, [...(children.get(leader) ?? []), s]);
  }
  const q = query.trim().toLowerCase();
  const matches = (s: SessionSummary) => !q || s.title.toLowerCase().includes(q) || (s.model ?? "").toLowerCase().includes(q) || (children.get(s.id) ?? []).some((c) => (c.metadata?.subagent_name ?? c.title).toLowerCase().includes(q));
  const top = all.filter((s) => !(s.metadata?.subagent_of && ids.has(s.metadata.subagent_of))).filter(matches);
  const childRunning = (s: SessionSummary) => (children.get(s.id) ?? []).some((c) => c.status === "running" || c.status === "waiting");
  const kind = (s: SessionSummary): "waiting" | "working" | "loop" | "idle" => {
    if (s.status === "waiting") return "waiting";
    if (s.status === "running" || childRunning(s)) return "working";
    if (s.metadata?.loop && s.metadata.loop.status === "active") return "loop";
    return "idle";
  };
  const groups: { key: string; label: string; items: SessionSummary[] }[] = [
    { key: "waiting", label: "Needs you", items: top.filter((s) => kind(s) === "waiting") },
    { key: "working", label: "Working", items: top.filter((s) => kind(s) === "working") },
    { key: "loop", label: "Loops", items: top.filter((s) => kind(s) === "loop") },
    { key: "idle", label: "Idle", items: top.filter((s) => kind(s) === "idle") },
  ].filter((g) => (filter === "all" ? true : filter === "working" ? g.key === "waiting" || g.key === "working" : g.key === "loop"));
  const activeCount = top.filter((s) => kind(s) === "waiting" || kind(s) === "working").length;
  const shown = groups.reduce((n, g) => n + g.items.length, 0);

  return (
    <>
      <PageHeader
        title="Agents"
        subtitle={sessions ? `${top.length} agent${top.length === 1 ? "" : "s"}${activeCount ? ` · ${activeCount} active` : ""}` : undefined}
        actions={
          <>
            <button className={`iconbtn ${searching ? "on" : ""}`} onClick={() => { setSearching((v) => !v); if (searching) setQuery(""); }} title="Search" aria-label="Search" aria-pressed={searching}><Icon name="search" /></button>
            {!compact && <button className="iconbtn" onClick={() => setShowWorkspaces(true)} title="Workspaces" aria-label="Workspaces"><Icon name="folder" /></button>}
            <button className="iconbtn primary" onClick={() => setCreating(true)} title="New agent" aria-label="New agent"><Icon name="plus" /></button>
          </>
        }
      >
        {searching && <input className="field search" autoFocus placeholder="Search agents…" value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => { if (e.key === "Escape") { setQuery(""); setSearching(false); } }} aria-label="Search agents" />}
        {!compact && <div className="chips">
          {(["all", "working", "loops"] as Filter[]).map((f) => (
            <button key={f} className="chip select" aria-pressed={filter === f} onClick={() => setFilter(f)}>
              {f === "all" ? `All · ${top.length}` : f === "working" ? `Active · ${activeCount}` : `Loops · ${top.filter((s) => kind(s) === "loop").length}`}
            </button>
          ))}
        </div>}
      </PageHeader>
      <div className="screen narrow">
        {loading && !error && <Skeleton rows={5} />}
        {error && !sessions && <div className="empty"><b>Could not load agents</b><div>{error}</div></div>}
        {sessions && sessions.length === 0 && (
          <div className="empty">
            <b>No agents yet</b>
            <div>Create one here, or write to the bot in Telegram.</div>
            <button className="btn primary" onClick={() => setCreating(true)}>Create agent</button>
          </div>
        )}
        {sessions && sessions.length > 0 && shown === 0 && <div className="empty">Nothing matches.</div>}
        {groups.map((g) =>
          g.items.length === 0 ? null : (
            <section key={g.key} className="erow-group">
              <div className="section-title">
                {g.label} <span className="n">{g.items.length}</span>
              </div>
              {g.items.map((s) => (
                <Row key={s.id} s={s} kids={children.get(s.id) ?? []} onOpen={onOpen} current={current === s.id || (children.get(s.id) ?? []).some((c) => c.id === current)} />
              ))}
            </section>
          ),
        )}
      </div>
      {creating && <NewAgentSheet onClose={() => setCreating(false)} onCreated={onOpen} toast={toast} />}
      {showWorkspaces && (
        <Sheet title="Workspaces" onClose={() => setShowWorkspaces(false)}>
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
  const cadence = loop.mode === "interval" ? `every ${fmtInterval(loop.interval_seconds)}` : "self-paced";
  const runs = `run #${loop.run_count}${loop.max_runs ? `/${loop.max_runs}` : ""}`;
  if (loop.status === "active") return `Loop ${cadence} · ${runs}${loop.next_run_at ? ` · next ${untilShort(loop.next_run_at)}` : ""}`;
  return `Loop ${loop.status}${loop.pause_note || loop.stop_reason ? `: ${(loop.pause_note || loop.stop_reason || "").slice(0, 80)}` : ""} · ${runs}`;
}

function Row({ s, kids, onOpen, current }: { s: SessionSummary; kids: SessionSummary[]; onOpen: (id: string) => void; current?: boolean }) {
  const [showKids, setShowKids] = useState(false);
  const orphan = !!s.metadata?.subagent_of;
  const status = s.status as Status;
  const spoken = status === "running" || status === "waiting" || status === "failed";
  const loop = loopLine(s);
  const visibleKids = showKids ? kids : kids.slice(0, 3);
  const open = () => onOpen(s.id);
  return (
    <div className={`erow ${status} ${current ? "current" : ""}`} role="link" aria-current={current ? "page" : undefined} tabIndex={0} onClick={open} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } }}>
      <Avatar status={status} seed={s.id} />
      <div className="erow-main">
        <div className="erow-head">
          <span className="erow-title clamp-2">{orphan ? s.metadata?.subagent_name || s.title.replace(/^\[sub\]\s*/, "") : s.title}</span>
          <span className="erow-time num" title={new Date(s.last_message_at).toLocaleString()}>{relTime(s.last_message_at)}</span>
        </div>
        <div className={`erow-meta ${spoken ? status : ""}`}>
          {spoken && <Dot status={status} />}
          {spoken && <span className="word">{STATUS_WORD[status]}</span>}
          {spoken && s.model && <span className="sep">·</span>}
          {s.model && <span title={s.model}>{shortModel(s.model, 28)}</span>}
          {orphan && <span className="sep">·</span>}
          {orphan && <span>subagent, leader gone</span>}
        </div>
        {loop && <div className={`erow-meta ${s.metadata?.loop?.status === "paused" ? "waiting" : ""}`}>{loop}</div>}
        {kids.length > 0 && (
          <div className="erow-children" onClick={(e) => e.stopPropagation()}>
            {visibleKids.map((c) => (
              <button key={c.id} className="subrow" onClick={() => onOpen(c.id)} title={`open ${c.metadata?.subagent_name ?? c.title}`}>
                <Dot status={c.status === "idle" ? "done" : c.status} />
                <span className="truncate">{c.metadata?.subagent_name || c.title.replace(/^\[sub\]\s*/, "")}</span>
                <span className="sub">{c.status === "running" ? "working" : c.status === "waiting" ? "needs you" : c.status === "failed" ? "failed" : "done"}</span>
              </button>
            ))}
            {kids.length > 3 && (
              <button className="subrow more" onClick={() => setShowKids((v) => !v)}>
                {showKids ? "Fewer" : `${kids.length - 3} more subagent${kids.length - 3 === 1 ? "" : "s"}`}
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/** The form for a new agent: a name and a first task; the workspace, the loop and the tools sit behind Advanced. */
function NewAgentSheet({ onClose, onCreated, toast }: { onClose: () => void; onCreated: (id: string) => void; toast: (t: string) => void }) {
  const [title, setTitle] = useState("");
  const [prompt, setPrompt] = useState("");
  const [toolsOff, setToolsOff] = useState<string[]>([]);
  const [loopOn, setLoopOn] = useState(false);
  const [loopText, setLoopText] = useState("");
  const [loopMode, setLoopMode] = useState<"interval" | "dynamic">("interval");
  const [loopMinutes, setLoopMinutes] = useState("10");
  const [loopMax, setLoopMax] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [busy, setBusy] = useState(false);
  const workspaces = useQuery<Workspace[]>("/api/workspaces", { staleMs: 30000 });

  async function create() {
    if (!title.trim() || busy) return;
    setBusy(true);
    try {
      const loop = loopOn && loopText.trim()
        ? { instruction: loopText.trim(), mode: loopMode, interval_minutes: loopMode === "interval" ? Math.max(1, Number(loopMinutes) || 10) : null, max_runs: loopMax.trim() ? Math.max(1, Number(loopMax) || 1) : null }
        : undefined;
      const created = await api.post<{ id: string }>("/api/sessions", { title: title.trim(), prompt: prompt.trim() || undefined, tools_off: toolsOff, loop, workspace: workspace || undefined });
      onClose();
      onCreated(created.id);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  const wsLabel = (w: Workspace) => `${w.own_session ? `${w.sessions.find((s) => s.id === w.name)?.title ?? w.name} (session workspace)` : w.name}${w.sessions.length ? ` · ${w.sessions.length} session${w.sessions.length === 1 ? "" : "s"}` : " · unused"} · ${w.files} files`;
  return (
    <Sheet title="New agent" onClose={onClose}>
      <label className="field">Name</label>
      <input className="field" autoFocus value={title} onChange={(e) => setTitle(e.target.value)} placeholder="What this agent is about" onKeyDown={(e) => e.key === "Enter" && create()} />
      <label className="field">First task (optional)</label>
      <textarea className="field" rows={3} value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder="It starts on this right away" />
      <label className="field">Workspace</label>
      <select className="field" value={workspace} onChange={(e) => setWorkspace(e.target.value)}>
        <option value="">A directory of its own</option>
        {(workspaces.data ?? []).map((w) => (
          <option key={w.name} value={w.name}>{wsLabel(w)}</option>
        ))}
      </select>
      <button type="button" className="disclosure" onClick={() => setAdvanced((v) => !v)} aria-expanded={advanced}>
        <span className={`chev ${advanced ? "down" : ""}`}>›</span> Advanced{loopOn ? " · loop" : ""}{toolsOff.length ? ` · ${toolsOff.length} tools off` : ""}
      </button>
      {advanced && (
        <div className="disclosure-body">
          <div className="sub">Several sessions can work in one directory: each keeps its own history, model and brief, and sees the same files.</div>
          <label className="toggle-row">
            <input type="checkbox" checked={loopOn} onChange={(e) => setLoopOn(e.target.checked)} />
            <span>Loop agent</span>
            <span className="sub">woken up for one standing task, on an interval or when it says so</span>
          </label>
          {loopOn && (
            <div className="loop-form">
              <label className="field">Loop instruction (what each wake-up is for)</label>
              <textarea className="field" rows={3} value={loopText} onChange={(e) => setLoopText(e.target.value)} placeholder="Check the forum for replies to my threads; answer what needs answering; report only what matters." />
              <div className="composer-row">
                <select className="field" value={loopMode} onChange={(e) => setLoopMode(e.target.value as "interval" | "dynamic")}>
                  <option value="interval">every N min</option>
                  <option value="dynamic">self-paced</option>
                </select>
                {loopMode === "interval" && <input className="field" type="number" min={1} style={{ maxWidth: 110 }} value={loopMinutes} onChange={(e) => setLoopMinutes(e.target.value)} aria-label="minutes" />}
                <input className="field" type="number" min={1} style={{ maxWidth: 130 }} placeholder="max runs" value={loopMax} onChange={(e) => setLoopMax(e.target.value)} aria-label="max runs" />
              </div>
              <div className="sub">The first iteration runs right after creation. The agent stops the loop itself when its purpose is achieved, pauses it when it needs you, and stays quiet when there is nothing to report.</div>
            </div>
          )}
          <ToolPicker off={toolsOff} onChange={setToolsOff} note="Untick what this agent must not have (self-development, spawning agents, the shell…). Everything is on by default." />
        </div>
      )}
      <div className="sheet-foot">
        <button className="btn ghost" onClick={onClose}>Cancel</button>
        <button className="btn primary" onClick={create} disabled={busy || !title.trim() || (loopOn && !loopText.trim())}>
          Create
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
      toast(`workspace ${n} created`);
      setName("");
      refresh();
    } catch (e) {
      toast(errorText(e));
    }
  }
  async function remove(w: Workspace) {
    if (!(await confirmAsync(`Delete the workspace "${w.name}"?`, { body: `Its ${w.files} file${w.files === 1 ? "" : "s"} are removed. No session works in it.`, action: "Delete" }))) return;
    try {
      await api.delete(`/api/workspaces/${encodeURIComponent(w.name)}`);
      toast("workspace deleted");
      refresh();
    } catch (e) {
      toast(errorText(e));
    }
  }
  const onClose = useCallback(() => setBrowsing(null), []);
  return (
    <div className="workspaces">
      <div className="composer-row" style={{ marginTop: 0, marginBottom: 8 }}>
        <input className="field" placeholder="new workspace name (letters, digits, - _ .)" value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && create()} />
        <button className="btn primary" disabled={!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(name.trim())} onClick={create}>Create</button>
      </div>
      {!workspaces && <div className="sub">Loading…</div>}
      {workspaces?.length === 0 && <div className="sub">No workspaces yet.</div>}
      {workspaces?.map((w) => (
        <div key={w.name} className="ws-row">
          <span aria-hidden>📁</span>
          <div className="grow" style={{ minWidth: 0 }}>
            <div className="ws-name">
              {w.name}
              {w.kind === "session" && <span className="badge" title="created with a session; it stays while anyone works in it">session</span>}
              {w.kind === "schedule" && <span className="badge" title={`the scheduled task ${w.schedule} runs here`}>schedule: {w.schedule}</span>}
              {w.kind === "heartbeat" && <span className="badge">heartbeat</span>}
              {w.kind === "named" && w.sessions.length === 0 && <span className="badge">unused</span>}
            </div>
            <div className="sub ws-meta">
              {w.files} file{w.files === 1 ? "" : "s"} · {fmtBytes(w.size)} · {relTime(w.mtime)}
              {w.sessions.map((s) => (
                <button key={s.id} className="linkbtn sub" onClick={() => onOpen(s.id)} title="open the session"> · {s.title}</button>
              ))}
            </div>
          </div>
          <button className="iconbtn small" onClick={() => setBrowsing(w)} title="Browse and upload files" aria-label="Browse files"><Icon name="folder" size={15} /></button>
          {w.sessions.length === 0 && w.kind === "named" && <button className="iconbtn small" onClick={() => remove(w)} title="Delete" aria-label="Delete"><Icon name="trash" size={15} /></button>}
        </div>
      ))}
      {browsing && (
        <Sheet title={<><span aria-hidden>📁</span> {browsing.name}</>} onClose={onClose}>
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

