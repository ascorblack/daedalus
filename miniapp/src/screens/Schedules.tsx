import { useCallback, useEffect, useState } from "react";
import { api, Schedule } from "../api";
import { timeAgo } from "../components";

export function SchedulesScreen({ toast, onOpen }: { toast: (t: string) => void; onOpen: (id: string) => void }) {
  const [items, setItems] = useState<Schedule[] | null>(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ name: "", prompt: "", cron: "", run_at: "", kind: "agent" as "agent" | "message" | "lazy" });

  const load = useCallback(async () => {
    try {
      setItems(await api.get<Schedule[]>("/api/schedules"));
    } catch (e) {
      toast((e as Error).message);
    }
  }, [toast]);
  useEffect(() => {
    load();
  }, [load]);

  async function create() {
    try {
      await api.post("/api/schedules", {
        name: form.name,
        prompt: form.prompt,
        cron: form.cron.trim() || null,
        run_at: form.run_at.trim() || null,
        kind: form.kind,
      });
      setCreating(false);
      setForm({ name: "", prompt: "", cron: "", run_at: "", kind: "agent" });
      load();
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function remove(id: string) {
    await api.delete(`/api/schedules/${id}`);
    load();
  }

  async function runNow(id: string) {
    try {
      const r = await api.post<{ session_id: string }>(`/api/schedules/${id}/run`);
      onOpen(r.session_id);
    } catch (e) {
      toast((e as Error).message);
    }
  }

  return (
    <>
      <div className="btnrow" style={{ marginTop: 0, marginBottom: 12 }}>
        <button className="btn primary" onClick={() => setCreating((v) => !v)}>
          {creating ? "Cancel" : "+ New task"}
        </button>
      </div>
      {creating && (
        <div className="card">
          <label className="field">Name</label>
          <input className="field" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <label className="field">Kind</label>
          <div className="btnrow" style={{ marginTop: 0 }}>
            {(["agent", "message", "lazy"] as const).map((k) => (
              <button key={k} className={`btn small ${form.kind === k ? "primary" : ""}`} onClick={() => setForm({ ...form, kind: k })}>
                {k === "agent" ? "agent task" : k === "message" ? "plain reminder" : "lazy note"}
              </button>
            ))}
          </div>
          <div className="sub">
            {form.kind === "agent" && "runs the prompt as a task in its own session and workspace"}
            {form.kind === "message" && "delivers the text to you as a reminder; no model call"}
            {form.kind === "lazy" && "shown together with your next message in the session it was created from; becomes a task after a day"}
          </div>
          <label className="field">{form.kind === "agent" ? "Prompt for the future session" : "Reminder text"}</label>
          <textarea className="field" rows={4} value={form.prompt} onChange={(e) => setForm({ ...form, prompt: e.target.value })} />
          <label className="field">Cron (UTC, 5 fields) — for a recurring task</label>
          <input className="field" placeholder="0 7 * * 1-5" value={form.cron} onChange={(e) => setForm({ ...form, cron: e.target.value })} />
          <label className="field">Run once at (ISO 8601, UTC)</label>
          <input className="field" placeholder="2026-01-31T09:00:00Z" value={form.run_at} onChange={(e) => setForm({ ...form, run_at: e.target.value })} />
          <div className="btnrow">
            <button className="btn primary" disabled={!form.name || !form.prompt || !!form.cron === !!form.run_at} onClick={create}>
              Create
            </button>
          </div>
        </div>
      )}
      {items === null && <div className="empty">Loading…</div>}
      {items?.length === 0 && <div className="empty">No scheduled tasks.</div>}
      {items?.map((s) => (
        <div key={s.id} className="card">
          <div className="row">
            <div className="grow">
              <div className="title">
                {s.enabled ? "" : "⏸ "}
                {s.name} <span className="badge">{s.kind === "message" ? "reminder" : s.kind === "lazy" ? "lazy" : "agent"}</span>
              </div>
              <div className="sub">
                {s.cron ? `cron ${s.cron}` : `once ${s.run_at}`} · next {s.next_run_at ? new Date(s.next_run_at).toLocaleString() : "—"}
                {s.last_run_at && ` · last ${timeAgo(s.last_run_at)}`}
                {s.failure_count > 0 && <span style={{ color: "var(--bad)" }}> · {s.failure_count} failure{s.failure_count === 1 ? "" : "s"}{s.enabled ? "" : " — switched off"}</span>}
              </div>
            </div>
          </div>
          <details style={{ marginTop: 6 }}>
            <summary className="sub">prompt & last summary</summary>
            <pre className="diff">{s.prompt}</pre>
            {s.last_summary && <pre className="diff">{s.last_summary}</pre>}
          </details>
          <div className="btnrow">
            <button className="btn small" onClick={() => runNow(s.id)}>
              run now
            </button>
            <button className="btn small danger" onClick={() => remove(s.id)}>
              delete
            </button>
          </div>
        </div>
      ))}
    </>
  );
}
