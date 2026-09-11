import { useState } from "react";
import { ServiceView } from "../api";
import { ServiceRow, Skeleton } from "../components";
import { Sheet } from "../dialogs";
import { relTime } from "../format";
import { PageHeader } from "../shell";
import { invalidate, useQuery } from "../store";

type Row = ServiceView & { session_id: string; session_title: string };

/** Every service the agents host, grouped by the session that runs it. */
export function ServicesScreen({ onOpen, toast }: { onOpen: (id: string) => void; toast: (t: string) => void }) {
  const { data: rows, error, loading, refresh } = useQuery<Row[]>("/api/services", { pollMs: 10000, staleMs: 3000 });
  const [log, setLog] = useState<{ title: string; text: string } | null>(null);
  const [showStopped, setShowStopped] = useState(false);
  const reload = () => {
    refresh();
    invalidate("/api/services");
  };
  const all = rows ?? [];
  const running = all.filter((r) => r.status === "running");
  const stopped = all.filter((r) => r.status !== "running");
  const shown = showStopped ? all : running;
  const groups = new Map<string, Row[]>();
  for (const r of shown) groups.set(r.session_id, [...(groups.get(r.session_id) ?? []), r]);
  return (
    <>
      <PageHeader title="Services" subtitle={rows ? `${running.length} running${stopped.length ? ` · ${stopped.length} stopped` : ""}` : undefined}>
        {stopped.length > 0 && (
          <div className="chips">
            <button className="chip select" aria-pressed={!showStopped} onClick={() => setShowStopped(false)}>Running · {running.length}</button>
            <button className="chip select" aria-pressed={showStopped} onClick={() => setShowStopped(true)}>All · {all.length}</button>
          </div>
        )}
      </PageHeader>
      <div className="screen narrow">
        {loading && !error && <Skeleton rows={3} />}
        {error && !rows && <div className="empty"><b>Could not load the services</b><div>{error}</div><button className="btn" onClick={refresh}>Retry</button></div>}
        {rows && rows.length === 0 && (
          <div className="empty">
            <b>No services</b>
            <div>Agents can host local web apps, workers and dev servers; they appear here with the address to open them at.</div>
          </div>
        )}
        {rows && rows.length > 0 && shown.length === 0 && <div className="empty"><b>Nothing running</b><div>{stopped.length} stopped service{stopped.length === 1 ? "" : "s"} in the list.</div></div>}
        {[...groups.entries()].map(([sid, items]) => (
          <section key={sid} className="service-group">
            <div className="section-title">
              <button className="linkbtn" onClick={() => onOpen(sid)} title="open the session">{items[0].session_title}</button>
              <span className="n">started {relTime(items[0].started_at)}</span>
            </div>
            {items.map((s) => (
              <ServiceRow key={s.name} s={s} sessionId={sid} onChange={reload} toast={toast} onLogs={(text) => setLog({ title: `${items[0].session_title} · ${s.name}`, text })} card />
            ))}
          </section>
        ))}
      </div>
      {log && (
        <Sheet title={log.title} onClose={() => setLog(null)} size="wide">
          <pre className="filetext">{log.text}</pre>
        </Sheet>
      )}
    </>
  );
}
