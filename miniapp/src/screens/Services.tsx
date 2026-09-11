import { useCallback, useEffect, useState } from "react";
import { api, ServiceView } from "../api";
import { ServiceRow, timeAgo } from "../components";
import { errorText } from "../ui";
import { PageHeader } from "../shell";

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
  const groups = new Map<string, Row[]>();
  for (const r of rows ?? []) groups.set(r.session_id, [...(groups.get(r.session_id) ?? []), r]);
  const running = (rows ?? []).filter((r) => r.status === "running").length;
  return (
    <>
      <PageHeader title="Services" subtitle={rows ? `${running} running · ${rows.length} total` : undefined} />
      <div className="screen narrow">
      {error && rows === null && <div className="empty"><b>Could not load services</b><div>{error}</div><button className="btn" onClick={load}>Retry</button></div>}
      {rows === null && !error && <div className="empty">Loading…</div>}
      {rows?.length === 0 && <div className="empty"><b>No running services</b><div>Agents can host local web apps, workers and dev servers; they appear here with the address to open them at.</div></div>}
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
      </div>
    </>
  );
}
