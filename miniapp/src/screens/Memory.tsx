import { useCallback, useEffect, useMemo, useState } from "react";
import { api, MemoryListing, MemoryRecord } from "../api";
import { timeAgo } from "../components";
import { Icon } from "../icons";
import { confirmAsync, errorText } from "../ui";

const KINDS = ["fact", "decision", "preference", "reflection", "note"];

/** What the agent remembered — global and per session — to read, correct, add to and prune. */
export function MemoryScreen({ toast, onOpen }: { toast: (t: string) => void; onOpen: (id: string) => void }) {
  const [data, setData] = useState<MemoryListing | null>(null);
  const [bucket, setBucket] = useState<string>("all");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [editing, setEditing] = useState<{ id: string; text: string; kind: string } | null>(null);
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState({ scope: "global", scope_key: "", kind: "fact", text: "" });
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setData(await api.get<MemoryListing>("/api/memory"));
    } catch (e) {
      toast(errorText(e));
    }
  }, [toast]);
  useEffect(() => {
    load();
  }, [load]);

  const q = query.trim().toLowerCase();
  const records = useMemo(() => {
    const all = data?.records ?? [];
    return all.filter((r) => (bucket === "all" || `${r.scope}:${r.scope_key}` === bucket) && (!q || r.text.toLowerCase().includes(q) || r.kind.includes(q)));
  }, [data, bucket, q]);
  const bucketLabel = (scope: string, key: string) => (scope === "global" ? "Global" : scope === "session" ? data?.sessions[key] ?? `deleted session ${key}` : `${scope}: ${key}`);

  function toggle(id: string) {
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  }
  const allShown = records.length > 0 && records.every((r) => selected.has(r.id));
  function toggleAll() {
    setSelected((s) => {
      const n = new Set(s);
      if (allShown) records.forEach((r) => n.delete(r.id));
      else records.forEach((r) => n.add(r.id));
      return n;
    });
  }

  async function removeMany(ids: string[]) {
    if (!ids.length) return;
    if (!(await confirmAsync(ids.length === 1 ? "Forget this memory?" : `Forget ${ids.length} memories?`))) return;
    setBusy(true);
    try {
      const r = await api.post<{ deleted: number }>("/api/memory/delete", { ids });
      toast(`forgot ${r.deleted}`);
      setSelected((s) => {
        const n = new Set(s);
        ids.forEach((id) => n.delete(id));
        return n;
      });
      load();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  async function saveEdit() {
    if (!editing) return;
    setBusy(true);
    try {
      await api.patch(`/api/memory/${encodeURIComponent(editing.id)}`, { text: editing.text, kind: editing.kind });
      toast("memory updated");
      setEditing(null);
      load();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  async function add() {
    if (!draft.text.trim()) return;
    setBusy(true);
    try {
      const r = await api.post<{ decision: string; record: MemoryRecord }>("/api/memory", { ...draft, scope_key: draft.scope === "global" ? "" : draft.scope_key });
      toast(r.decision === "create" || r.decision === "CREATE" ? "remembered" : `${r.decision.toLowerCase()}: a similar memory existed`);
      setDraft((d) => ({ ...d, text: "" }));
      setAdding(false);
      load();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  if (!data) return <div className="empty">Loading…</div>;
  const sessions = Object.entries(data.sessions);
  return (
    <>
      <div className="memory-toolbar">
        <select className="field" value={bucket} onChange={(e) => setBucket(e.target.value)} aria-label="scope">
          <option value="all">All · {data.records.length}</option>
          {data.buckets.map((b) => (
            <option key={`${b.scope}:${b.scope_key}`} value={`${b.scope}:${b.scope_key}`}>
              {bucketLabel(b.scope, b.scope_key)} · {b.count}
            </option>
          ))}
        </select>
        <input className="field" placeholder="search" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="search memories" />
        <button className="btn primary" onClick={() => setAdding((v) => !v)}>{adding ? "Cancel" : "+ Remember"}</button>
      </div>
      {adding && (
        <div className="card">
          <div className="grid2">
            <div>
              <label className="field">Scope</label>
              <select className="field" value={draft.scope === "global" ? "global" : `session:${draft.scope_key}`} onChange={(e) => (e.target.value === "global" ? setDraft({ ...draft, scope: "global", scope_key: "" }) : setDraft({ ...draft, scope: "session", scope_key: e.target.value.slice(8) }))}>
                <option value="global">Global — every session</option>
                {sessions.map(([id, title]) => (
                  <option key={id} value={`session:${id}`}>{title}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="field">Kind</label>
              <select className="field" value={draft.kind} onChange={(e) => setDraft({ ...draft, kind: e.target.value })}>
                {KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
              </select>
            </div>
          </div>
          <label className="field">Text</label>
          <textarea className="field" rows={3} value={draft.text} onChange={(e) => setDraft({ ...draft, text: e.target.value })} placeholder="what the agent should keep in mind" />
          <div className="btnrow">
            <button className="btn primary" disabled={busy || !draft.text.trim()} onClick={add}>Remember</button>
          </div>
        </div>
      )}
      {records.length > 0 && (
        <div className="memory-bulk">
          <label className="toggle-row" style={{ margin: 0 }}>
            <input type="checkbox" checked={allShown} onChange={toggleAll} aria-label="select all shown" />
            <span className="sub">{selected.size ? `${selected.size} selected` : `${records.length} shown`}</span>
          </label>
          {selected.size > 0 && (
            <button className="btn small danger" disabled={busy} onClick={() => removeMany([...selected])}>
              <Icon name="trash" size={14} /> Forget {selected.size}
            </button>
          )}
        </div>
      )}
      {records.length === 0 && <div className="empty">{data.records.length === 0 ? "Nothing remembered yet. The agent writes here with Remember; you can add a note above." : "Nothing matches."}</div>}
      {records.map((r) => (
        <div key={r.id} className={`card memory-row ${selected.has(r.id) ? "selected" : ""}`}>
          <input type="checkbox" className="memory-check" checked={selected.has(r.id)} onChange={() => toggle(r.id)} aria-label="select" />
          <div className="grow" style={{ minWidth: 0 }}>
            <div className="memory-meta sub">
              <span className={`badge kind-${r.kind}`}>{r.kind}</span>
              {r.scope === "global" ? (
                <span className="badge" title="every session sees it">global</span>
              ) : r.scope === "session" ? (
                data.sessions[r.scope_key] ? (
                  <button className="linkbtn sub" onClick={() => onOpen(r.scope_key)} title="open the session">{data.sessions[r.scope_key]}</button>
                ) : (
                  <span title={r.scope_key}>deleted session</span>
                )
              ) : (
                <span>{r.scope}: {r.scope_key}</span>
              )}
              {r.created_at && <span>· {timeAgo(r.created_at)}</span>}
              {r.version > 1 && <span>· v{r.version}</span>}
            </div>
            {editing?.id === r.id ? (
              <>
                <textarea className="field" rows={3} value={editing.text} onChange={(e) => setEditing({ ...editing, text: e.target.value })} autoFocus />
                <div className="btnrow" style={{ alignItems: "center" }}>
                  <select className="field" style={{ width: "auto" }} value={editing.kind} onChange={(e) => setEditing({ ...editing, kind: e.target.value })}>
                    {[...new Set([...KINDS, editing.kind])].map((k) => <option key={k} value={k}>{k}</option>)}
                  </select>
                  <button className="btn small primary" disabled={busy || !editing.text.trim()} onClick={saveEdit}>Save</button>
                  <button className="btn small" onClick={() => setEditing(null)}>Cancel</button>
                </div>
              </>
            ) : (
              <div className="memory-text">{r.text}</div>
            )}
          </div>
          {editing?.id !== r.id && (
            <div className="memory-actions">
              <button className="iconbtn small" onClick={() => setEditing({ id: r.id, text: r.text, kind: r.kind })} title="Edit" aria-label="edit"><Icon name="pen" size={15} /></button>
              <button className="iconbtn small" onClick={() => removeMany([r.id])} title="Forget" aria-label="forget"><Icon name="trash" size={15} /></button>
            </div>
          )}
        </div>
      ))}
    </>
  );
}
