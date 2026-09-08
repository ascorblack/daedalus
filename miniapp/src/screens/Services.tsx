import { useCallback, useEffect, useState } from "react";
import { api, ServiceView } from "../api";
import { ServiceRow, timeAgo } from "../components";
import { errorText } from "../ui";

type Row = ServiceView & { session_id: string; session_title: string };

/** Every service the agents host, grouped by the session that runs it. */
export function ServicesScreen({ onOpen, toast }: { onOpen: (id: string) => void; toast: (t: string) => void }) {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [log, setLog] = useState<{ title: string; text: string } | null>(null);
  const load = useCallback(async () => {
    try {
      setRows(await api.get<Row[]>("/api/services"));
      setError(null);
    } catch (e) {
      setError(errorText(e));
    }
  }, []);
  useEffect(() => {
    load();
    const id = setInterval(load, 10000);
    return () => clearInterval(id);
  }, [load]);
  if (error && rows === null) return <div className="empty">could not load services: {error} <button className="btn small" onClick={load}>retry</button></div>;
  if (rows === null) return <div className="empty">Loading…</div>;
  if (rows.length === 0) return <div className="empty">No services. An agent starts one with ServiceStart (a demo site, a dev server, a worker); it appears here with the address you open it at.</div>;
  const groups = new Map<string, Row[]>();
  for (const r of rows) groups.set(r.session_id, [...(groups.get(r.session_id) ?? []), r]);
  const running = rows.filter((r) => r.status === "running").length;
  return (
    <>
      <div className="section-title">{running} running · {rows.length} total</div>
      {[...groups.entries()].map(([sid, items]) => (
        <div key={sid} className="card">
          <div className="row" style={{ marginBottom: 4 }}>
            <button className="linkbtn title grow" onClick={() => onOpen(sid)} title="open the session">{items[0].session_title}</button>
            <span className="sub">{sid} · started {timeAgo(items[0].started_at)}</span>
          </div>
          {items.map((s) => (
            <ServiceRow key={s.name} s={s} sessionId={sid} onChange={load} toast={toast} onLogs={(text) => setLog({ title: `${items[0].session_title} · ${s.name}`, text })} />
          ))}
        </div>
      ))}
      {log && (
        <div className="sheet-backdrop" onClick={() => setLog(null)}>
          <div className="sheet" onClick={(e) => e.stopPropagation()}>
            <div className="grip" />
            <h3 className="mono">{log.title}</h3>
            <div className="sheet-body">
              <pre className="filetext">{log.text}</pre>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
