import { useCallback, useEffect, useState } from "react";
import { api, SessionSummary } from "../api";
import { Avatar, Pill, Status, timeAgo } from "../components";

export function SessionsScreen({ onOpen, toast }: { onOpen: (id: string) => void; toast: (t: string) => void }) {
  const [sessions, setSessions] = useState<SessionSummary[] | null>(null);
  const [creating, setCreating] = useState(false);
  const [title, setTitle] = useState("");
  const [prompt, setPrompt] = useState("");

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
      const created = await api.post<{ id: string }>("/api/sessions", { title: title.trim(), prompt: prompt.trim() || undefined });
      setCreating(false);
      setTitle("");
      setPrompt("");
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
          {creating ? "Cancel" : "+ New bot"}
        </button>
      </div>
      {creating && (
        <div className="card">
          <label className="field">Title</label>
          <input className="field" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="what is this session about" />
          <label className="field">First task (optional)</label>
          <textarea className="field" rows={3} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
          <div className="btnrow">
            <button className="btn primary" onClick={create} disabled={!title.trim()}>
              Create
            </button>
          </div>
        </div>
      )}
      {sessions === null && <div className="empty">Loading…</div>}
      {sessions !== null && sessions.length === 0 && <div className="empty">No sessions yet. Create one, or write to the bot in Telegram.</div>}
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
