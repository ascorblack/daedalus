import { useCallback, useEffect, useState } from "react";
import { api, SessionSummary, Workspace } from "../api";
import { Avatar, Pill, Status, ToolPicker, loopLabel, timeAgo } from "../components";
import { Icon } from "../icons";
import { FilePreview, PreviewSource, workspaceBase } from "../preview";
import { confirmAsync, errorText, fmtBytes } from "../ui";
import { Files } from "./Session";

export function SessionsScreen({ onOpen, toast }: { onOpen: (id: string) => void; toast: (t: string) => void }) {
  const [sessions, setSessions] = useState<SessionSummary[] | null>(null);
  const [creating, setCreating] = useState(false);
  const [title, setTitle] = useState("");
  const [prompt, setPrompt] = useState("");
  const [toolsOff, setToolsOff] = useState<string[]>([]);
  const [loopOn, setLoopOn] = useState(false);
  const [loopText, setLoopText] = useState("");
  const [loopMode, setLoopMode] = useState<"interval" | "dynamic">("interval");
  const [loopMinutes, setLoopMinutes] = useState("10");
  const [loopMax, setLoopMax] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [workspaces, setWorkspaces] = useState<Workspace[] | null>(null);
  const [showWorkspaces, setShowWorkspaces] = useState(false);
  const loadWorkspaces = useCallback(() => {
    api.get<Workspace[]>("/api/workspaces").then(setWorkspaces).catch(() => setWorkspaces([]));
  }, []);
  useEffect(() => {
    if (creating || showWorkspaces) loadWorkspaces();
  }, [creating, showWorkspaces, loadWorkspaces]);

  const load = useCallback(async () => {
    try {
      setSessions(await api.get<SessionSummary[]>("/api/sessions"));
    } catch (e) {
      toast(`could not load sessions: ${(e as Error).message}`);
    }
  }, [toast]);

  useEffect(() => {
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, [load]);

  async function create() {
    if (!title.trim()) return;
    try {
      const loop = loopOn && loopText.trim()
        ? { instruction: loopText.trim(), mode: loopMode, interval_minutes: loopMode === "interval" ? Math.max(1, Number(loopMinutes) || 10) : null, max_runs: loopMax.trim() ? Math.max(1, Number(loopMax) || 1) : null }
        : undefined;
      const created = await api.post<{ id: string }>("/api/sessions", { title: title.trim(), prompt: prompt.trim() || undefined, tools_off: toolsOff, loop, workspace: workspace || undefined });
      setCreating(false);
      setTitle("");
      setPrompt("");
      setToolsOff([]);
      setLoopOn(false);
      setLoopText("");
      setWorkspace("");
      onOpen(created.id);
    } catch (e) {
      toast((e as Error).message);
    }
  }

  // Subagents sit under their leader; a child whose leader is gone is listed on its own.
  const all = sessions ?? [];
  const ids = new Set(all.map((s) => s.id));
  const children = new Map<string, SessionSummary[]>();
  for (const s of all) {
    const leader = s.metadata?.subagent_of;
    if (leader && ids.has(leader)) children.set(leader, [...(children.get(leader) ?? []), s]);
  }
  const top = all.filter((s) => !(s.metadata?.subagent_of && ids.has(s.metadata.subagent_of)));
  const isActive = (s: SessionSummary) => s.status === "running" || s.status === "waiting" || (children.get(s.id) ?? []).some((c) => c.status === "running");
  const active = top.filter(isActive);
  const rest = top.filter((s) => !isActive(s));
  const group = (s: SessionSummary) => (
    <div key={s.id}>
      <Row s={s} onOpen={onOpen} />
      {(children.get(s.id) ?? []).map((c) => (
        <Row key={c.id} s={c} onOpen={onOpen} child />
      ))}
    </div>
  );

  return (
    <>
      <div className="btnrow" style={{ marginTop: 0, marginBottom: 12 }}>
        <button className="btn primary" onClick={() => setCreating((v) => !v)}>
          {creating ? "Cancel" : "+ New agent"}
        </button>
        <button className={`btn ${showWorkspaces ? "primary" : ""}`} onClick={() => setShowWorkspaces((v) => !v)} title="The directories sessions work in">
          <Icon name="folder" size={15} /> Workspaces
        </button>
      </div>
      {showWorkspaces && <WorkspacesPanel workspaces={workspaces} onChange={loadWorkspaces} onOpen={onOpen} toast={toast} />}
      {creating && (
        <div className="card">
          <label className="field">Title</label>
          <input className="field" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="what is this session about" />
          <label className="field">First task (optional)</label>
          <textarea className="field" rows={3} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
          <label className="field">Workspace</label>
          <select className="field" value={workspace} onChange={(e) => setWorkspace(e.target.value)}>
            <option value="">a directory of its own</option>
            {(workspaces ?? []).map((w) => (
              <option key={w.name} value={w.name}>
                {w.own_session ? `${w.sessions.find((s) => s.id === w.name)?.title ?? w.name} (session workspace)` : w.name}
                {w.sessions.length ? ` · ${w.sessions.length} session${w.sessions.length === 1 ? "" : "s"}` : " · unused"} · {w.files} files
              </option>
            ))}
          </select>
          <div className="sub">Several sessions can work in one directory: each keeps its own history, model and brief, and sees the same files. The directory outlives any one of them.</div>
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
          <div className="btnrow">
            <button className="btn primary" onClick={create} disabled={!title.trim() || (loopOn && !loopText.trim())}>
              Create
            </button>
          </div>
        </div>
      )}
      {sessions === null && <div className="empty">Loading…</div>}
      {sessions !== null && sessions.length === 0 && <div className="empty">No agents yet. Create one, or write to the bot in Telegram.</div>}
      {active.length > 0 && <div className="section-title">Active</div>}
      {active.map(group)}
      {rest.length > 0 && <div className="section-title">Roster</div>}
      {rest.map(group)}
    </>
  );
}

function Row({ s, onOpen, child }: { s: SessionSummary; onOpen: (id: string) => void; child?: boolean }) {
  const orphan = !child && !!s.metadata?.subagent_of;
  return (
    <div className={"card pressable row" + (child ? " subrow" : "")} onClick={() => onOpen(s.id)}>
      {child && <span className="subarrow" aria-hidden>↳</span>}
      <Avatar status={s.status as Status} seed={s.id} />
      <div className="grow">
        <div className="title-row">
          <span className="title">{child ? s.metadata?.subagent_name || s.title.replace(/^\[sub\]\s*/, "") : s.title}</span>
          {(child || orphan) && <span className="badge sub">subagent</span>}
          {s.metadata?.loop && <span className={`badge loop ${s.metadata.loop.status}`}>{loopLabel(s.metadata.loop)}</span>}
        </div>
        <div className="sub">
          {s.id} · {timeAgo(s.last_message_at)}
          {orphan && " · leader gone"}
        </div>
      </div>
      <Pill status={s.status} />
    </div>
  );
}


/** The directories under the workspaces root: who works in each, what is in it; make one, fill it, browse it, drop an unused one. */
function WorkspacesPanel({ workspaces, onChange, onOpen, toast }: { workspaces: Workspace[] | null; onChange: () => void; onOpen: (id: string) => void; toast: (t: string) => void }) {
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
      onChange();
    } catch (e) {
      toast(errorText(e));
    }
  }
  async function remove(w: Workspace) {
    if (!(await confirmAsync(`Delete the workspace "${w.name}" and its ${w.files} file${w.files === 1 ? "" : "s"}?`))) return;
    try {
      await api.delete(`/api/workspaces/${encodeURIComponent(w.name)}`);
      toast("workspace deleted");
      onChange();
    } catch (e) {
      toast(errorText(e));
    }
  }
  return (
    <div className="card workspaces">
      <div className="section-title" style={{ marginTop: 0 }}>Workspaces</div>
      <div className="composer-row" style={{ marginTop: 0, marginBottom: 8 }}>
        <input className="field" placeholder="new workspace name (letters, digits, - _ .)" value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && create()} />
        <button className="btn primary" disabled={!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(name.trim())} onClick={create}>Create</button>
      </div>
      {workspaces === null && <div className="sub">Loading…</div>}
      {workspaces?.length === 0 && <div className="sub">No workspaces yet.</div>}
      {workspaces?.map((w) => (
        <div key={w.name} className="ws-row">
          <span aria-hidden>📁</span>
          <div className="grow" style={{ minWidth: 0 }}>
            <div className="ws-name">
              {w.name}
              {w.kind === "session" && <span className="badge" title="created with a session; it stays while anyone works in it">session</span>}
              {w.kind === "schedule" && <span className="badge" title={`the scheduled task ${w.schedule} runs here`}>cron: {w.schedule}</span>}
              {w.kind === "heartbeat" && <span className="badge">heartbeat</span>}
              {w.kind === "named" && w.sessions.length === 0 && <span className="badge">unused</span>}
            </div>
            <div className="sub ws-meta">
              {w.files} file{w.files === 1 ? "" : "s"} · {fmtBytes(w.size)} · {timeAgo(new Date(w.mtime * 1000).toISOString())}
              {w.sessions.map((s) => (
                <button key={s.id} className="linkbtn sub" onClick={() => onOpen(s.id)} title="open the session"> · {s.title}</button>
              ))}
            </div>
          </div>
          <button className="iconbtn small" onClick={() => setBrowsing(w)} title="Browse and upload files" aria-label="browse"><Icon name="folder" size={15} /></button>
          {w.sessions.length === 0 && w.kind === "named" && <button className="iconbtn small" onClick={() => remove(w)} title="Delete" aria-label="delete"><Icon name="trash" size={15} /></button>}
        </div>
      ))}
      {browsing && (
        <div className="sheet-backdrop" onClick={() => setBrowsing(null)}>
          <div className="sheet" onClick={(e) => e.stopPropagation()} role="dialog" aria-label={`workspace ${browsing.name}`}>
            <div className="grip" />
            <div className="sheet-head">
              <h3><span aria-hidden>📁</span> {browsing.name}</h3>
              <button className="iconbtn small" onClick={() => setBrowsing(null)} aria-label="close"><Icon name="close" size={16} /></button>
            </div>
            <div className="sheet-body">
              <Files base={workspaceBase(browsing.name)} uploadUrl={`${workspaceBase(browsing.name)}/upload`} onPreview={setPreview} toast={toast} />
            </div>
          </div>
        </div>
      )}
      {preview && <FilePreview src={preview} onClose={() => setPreview(null)} />}
    </div>
  );
}
