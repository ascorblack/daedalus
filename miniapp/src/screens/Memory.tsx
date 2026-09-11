import { useMemo, useState } from "react";
import { api, MemoryListing, MemoryRecord } from "../api";
import { Skeleton } from "../components";
import { Sheet, deleteWithUndo } from "../dialogs";
import { absTime, relTime } from "../format";
import { Icon } from "../icons";
import { PageHeader } from "../shell";
import { prime, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";

const KINDS = ["fact", "decision", "preference", "reflection", "skill", "note"];

/** What the agent remembered — global and per session — to read, correct, add to and prune. */
export function MemoryScreen({ toast, onOpen }: { toast: (t: string) => void; onOpen: (id: string) => void }) {
  const key = "/api/memory";
  const { data, error, loading, refresh } = useQuery<MemoryListing>(key, { staleMs: 10000 });
  const [bucket, setBucket] = useState<string>("all");
  const [kind, setKind] = useState<string>("all");
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [editing, setEditing] = useState<MemoryRecord | null>(null);
  const [adding, setAdding] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const q = query.trim().toLowerCase();
  const all = data?.records ?? [];
  const kinds = useMemo(() => {
    const counts = new Map<string, number>();
    for (const r of all) counts.set(r.kind, (counts.get(r.kind) ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [all]);
  const records = useMemo(
    () => all.filter((r) => (bucket === "all" || `${r.scope}:${r.scope_key}` === bucket) && (kind === "all" || r.kind === kind) && (!q || r.text.toLowerCase().includes(q))),
    [all, bucket, kind, q],
  );
  const bucketLabel = (scope: string, k: string) => (scope === "global" ? "Global" : scope === "session" ? data?.sessions[k] ?? `deleted session ${k}` : `${scope}: ${k}`);

  function toggle(id: string) {
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  }

  function forget(ids: string[]) {
    if (!ids.length || !data) return;
    const before = data;
    prime(key, { ...data, records: data.records.filter((r) => !ids.includes(r.id)) });
    setSelected(new Set());
    setEditing(null);
    deleteWithUndo(
      ids.length === 1 ? "Memory forgotten" : `${ids.length} memories forgotten`,
      async () => {
        await api.post("/api/memory/delete", { ids });
        refresh();
      },
      () => prime(key, before),
      (e) => toast(errorText(e)),
    );
  }

  async function forgetMany() {
    const ids = [...selected];
    if (!(await confirmAsync(`Forget ${ids.length} memories?`, { body: "The agent will not recall them again.", action: "Forget" }))) return;
    forget(ids);
    setSelecting(false);
  }

  const sessions = Object.entries(data?.sessions ?? {});
  return (
    <>
      <PageHeader
        title="Memory"
        subtitle={data ? `${records.length === all.length ? all.length : `${records.length} of ${all.length}`} memories` : undefined}
        actions={
          <>
            <button className={`iconbtn ${searching ? "on" : ""}`} onClick={() => { setSearching((v) => !v); if (searching) setQuery(""); }} title="Search" aria-label="Search" aria-pressed={searching}><Icon name="search" /></button>
            <button className={`iconbtn ${selecting ? "on" : ""}`} onClick={() => { setSelecting((v) => !v); setSelected(new Set()); }} title={selecting ? "Done selecting" : "Select"} aria-label={selecting ? "Done selecting" : "Select"} aria-pressed={selecting}><Icon name="check" /></button>
            <button className="iconbtn primary" onClick={() => setAdding(true)} title="Remember something" aria-label="Remember something"><Icon name="plus" /></button>
          </>
        }
      >
        {searching && <input className="field search" autoFocus placeholder="Search memories…" value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => { if (e.key === "Escape") { setQuery(""); setSearching(false); } }} aria-label="Search memories" />}
        <div className="chips">
          <button className="chip select" aria-pressed={kind === "all"} onClick={() => setKind("all")}>All · {all.length}</button>
          {kinds.map(([k, n]) => (
            <button key={k} className="chip select" aria-pressed={kind === k} onClick={() => setKind(k)}>{k} · {n}</button>
          ))}
        </div>
        {(data?.buckets.length ?? 0) > 1 && (
          <select className="field scope" value={bucket} onChange={(e) => setBucket(e.target.value)} aria-label="Scope">
            <option value="all">Every scope</option>
            {(data?.buckets ?? []).map((b) => (
              <option key={`${b.scope}:${b.scope_key}`} value={`${b.scope}:${b.scope_key}`}>{bucketLabel(b.scope, b.scope_key)} · {b.count}</option>
            ))}
          </select>
        )}
      </PageHeader>
      <div className="screen narrow memory">
        {loading && !error && <Skeleton rows={5} />}
        {error && !data && <div className="empty"><b>Could not load the memory</b><div>{error}</div><button className="btn" onClick={refresh}>Retry</button></div>}
        {data && records.length === 0 && (
          <div className="empty">
            <b>{all.length === 0 ? "Nothing remembered yet" : "Nothing matches"}</b>
            {all.length === 0 && <div>The agent writes here with Remember; you can add a note too.</div>}
          </div>
        )}
        <div className="memory-grid">
          {records.map((r) => {
            const long = r.text.length > 320 || r.text.split("\n").length > 5;
            const open = expanded.has(r.id);
            return (
              <div key={r.id} className={`erow memory-card ${selecting ? "selecting" : ""} ${selected.has(r.id) ? "selected" : ""}`} role={selecting ? "checkbox" : "button"} aria-checked={selecting ? selected.has(r.id) : undefined} tabIndex={0} onClick={() => (selecting ? toggle(r.id) : setEditing(r))} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selecting ? toggle(r.id) : setEditing(r); } }}>
                {selecting && <input type="checkbox" className="memory-check" checked={selected.has(r.id)} onChange={() => toggle(r.id)} onClick={(e) => e.stopPropagation()} aria-label="select" />}
                <div className="erow-main">
                  <div className="erow-meta">
                    <span className={`chip kind-${r.kind}`}>{r.kind}</span>
                    {r.scope === "global" ? <span>global</span> : r.scope === "session" ? (data?.sessions[r.scope_key] ? <button className="linkbtn" onClick={(e) => { e.stopPropagation(); onOpen(r.scope_key); }}>{data.sessions[r.scope_key]}</button> : <span>deleted session</span>) : <span>{r.scope}: {r.scope_key}</span>}
                    {r.created_at && <span className="sep">·</span>}
                    {r.created_at && <span className="faint" title={absTime(r.created_at)}>{relTime(r.created_at)}</span>}
                  </div>
                  <div className={`memory-text ${long && !open ? "clamp-4" : ""}`}>{r.text}</div>
                  {long && (
                    <button className="linkbtn more" onClick={(e) => { e.stopPropagation(); setExpanded((s) => { const n = new Set(s); if (n.has(r.id)) n.delete(r.id); else n.add(r.id); return n; }); }}>
                      {open ? "Less" : "More"}
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>
      {selecting && (
        <div className="bulkbar">
          <button className="btn ghost small" onClick={() => setSelected(new Set(records.every((r) => selected.has(r.id)) ? [] : records.map((r) => r.id)))}>
            {records.every((r) => selected.has(r.id)) ? "None" : "All shown"}
          </button>
          <span className="sub">{selected.size} selected</span>
          <span className="grow" />
          <button className="btn small danger" disabled={selected.size === 0} onClick={forgetMany}><Icon name="trash" size={14} /> Forget {selected.size || ""}</button>
        </div>
      )}
      {editing && <MemorySheet r={editing} sessions={sessions} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); refresh(); }} onForget={() => forget([editing.id])} toast={toast} />}
      {adding && <MemorySheet sessions={sessions} onClose={() => setAdding(false)} onSaved={() => { setAdding(false); refresh(); }} toast={toast} />}
    </>
  );
}

function MemorySheet({ r, sessions, onClose, onSaved, onForget, toast }: { r?: MemoryRecord; sessions: [string, string][]; onClose: () => void; onSaved: () => void; onForget?: () => void; toast: (t: string) => void }) {
  const [text, setText] = useState(r?.text ?? "");
  const [kind, setKind] = useState(r?.kind ?? "fact");
  const [scope, setScope] = useState(r ? (r.scope === "global" ? "global" : `session:${r.scope_key}`) : "global");
  const [busy, setBusy] = useState(false);
  async function save() {
    setBusy(true);
    try {
      if (r) {
        await api.patch(`/api/memory/${encodeURIComponent(r.id)}`, { text: text.trim(), kind });
        toast("memory updated");
      } else {
        const res = await api.post<{ decision: string; record: MemoryRecord }>("/api/memory", { scope: scope === "global" ? "global" : "session", scope_key: scope === "global" ? "" : scope.slice(8), kind, text: text.trim() });
        toast(res.decision.toLowerCase() === "create" ? "remembered" : `${res.decision.toLowerCase()}: a similar memory existed`);
      }
      onSaved();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet title={r ? "Memory" : "Remember"} onClose={onClose}>
      <label className="field">Text</label>
      <textarea className="field" rows={6} autoFocus value={text} onChange={(e) => setText(e.target.value)} placeholder="What the agent should keep in mind" />
      <div className="grid2">
        <div>
          <label className="field">Kind</label>
          <select className="field" value={kind} onChange={(e) => setKind(e.target.value)}>
            {[...new Set([...KINDS, kind])].map((k) => <option key={k} value={k}>{k}</option>)}
          </select>
        </div>
        <div>
          <label className="field">Scope</label>
          <select className="field" value={scope} disabled={!!r} onChange={(e) => setScope(e.target.value)}>
            <option value="global">Global — every session</option>
            {sessions.map(([id, title]) => (
              <option key={id} value={`session:${id}`}>{title}</option>
            ))}
          </select>
        </div>
      </div>
      {r && (
        <div className="sub" style={{ marginTop: 8 }}>
          {r.created_at ? `Created ${absTime(r.created_at)}` : ""}{r.version > 1 ? ` · version ${r.version}` : ""}{r.last_accessed_at ? ` · last recalled ${relTime(r.last_accessed_at)}` : ""}
        </div>
      )}
      <div className="sheet-foot">
        {r && onForget && <button className="btn danger" onClick={onForget}><Icon name="trash" size={14} /> Forget</button>}
        <span className="grow" />
        <button className="btn ghost" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={busy || !text.trim()} onClick={save}>{r ? "Save" : "Remember"}</button>
      </div>
    </Sheet>
  );
}
