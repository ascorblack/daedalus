import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { confirmAsync } from "../ui";
import { timeAgo } from "../components";

type Task = {
  id: string;
  title: string;
  status: "todo" | "doing" | "review" | "done" | "blocked" | "dropped";
  priority: number;
  acceptance: string;
  checklist: { text: string; done: boolean }[];
  depends_on: string[];
  session_id: string | null;
  notes: string;
  created_at: string;
  updated_at: string;
};

const COLUMNS: { id: Task["status"]; label: string }[] = [
  { id: "doing", label: "Doing" },
  { id: "review", label: "Review" },
  { id: "todo", label: "To do" },
  { id: "blocked", label: "Blocked" },
];

export function BoardScreen({ toast, onOpen }: { toast: (t: string) => void; onOpen: (id: string) => void }) {
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [showDone, setShowDone] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ title: "", acceptance: "", checklist: "", priority: 3 });

  const load = useCallback(async () => {
    try {
      setTasks(await api.get<Task[]>(`/api/board?include_done=${showDone ? 1 : 0}`));
    } catch (e) {
      toast((e as Error).message);
    }
  }, [toast, showDone]);
  useEffect(() => {
    load();
    const id = setInterval(load, 20000);
    return () => clearInterval(id);
  }, [load]);

  async function move(t: Task, status: Task["status"]) {
    try {
      await api.put(`/api/board/${t.id}`, { status });
      load();
    } catch (e) {
      toast((e as Error).message);
    }
  }
  async function toggle(t: Task, i: number) {
    const done = t.checklist[i].done;
    await api.put(`/api/board/${t.id}`, done ? { uncheck: [i] } : { check: [i] });
    load();
  }
  async function create() {
    try {
      await api.post("/api/board", {
        title: form.title,
        acceptance: form.acceptance,
        priority: form.priority,
        checklist: form.checklist.split("\n").map((l) => l.trim()).filter(Boolean),
      });
      setCreating(false);
      setForm({ title: "", acceptance: "", checklist: "", priority: 3 });
      load();
    } catch (e) {
      toast((e as Error).message);
    }
  }
  async function remove(t: Task) {
    if (!(await confirmAsync(`Delete task ${t.id} "${t.title}"?`))) return;
    await api.delete(`/api/board/${t.id}`);
    load();
  }

  if (!tasks) return <div className="empty">Loading…</div>;
  const finished = tasks.filter((t) => t.status === "done" || t.status === "dropped");
  return (
    <>
      <div className="btnrow" style={{ marginTop: 0, marginBottom: 12 }}>
        <button className="btn primary" onClick={() => setCreating((v) => !v)}>
          {creating ? "Cancel" : "+ Task"}
        </button>
        <button className={`btn small ${showDone ? "primary" : ""}`} onClick={() => setShowDone((v) => !v)}>
          finished
        </button>
      </div>
      {creating && (
        <div className="card">
          <label className="field">Title</label>
          <input className="field" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} />
          <label className="field">Acceptance (how anyone can tell it is done)</label>
          <textarea className="field" rows={2} value={form.acceptance} onChange={(e) => setForm({ ...form, acceptance: e.target.value })} />
          <label className="field">Checklist (one item per line)</label>
          <textarea className="field" rows={3} value={form.checklist} onChange={(e) => setForm({ ...form, checklist: e.target.value })} />
          <label className="field">Priority (1 = highest)</label>
          <input className="field" type="number" min={1} max={5} value={form.priority} onChange={(e) => setForm({ ...form, priority: Number(e.target.value) })} />
          <div className="btnrow">
            <button className="btn primary" disabled={!form.title.trim()} onClick={create}>
              Create
            </button>
          </div>
        </div>
      )}
      {tasks.length === 0 && <div className="empty">The board is empty. The agent adds tasks with BoardAdd; you can add one above.</div>}
      {COLUMNS.map((col) => {
        const items = tasks.filter((t) => t.status === col.id);
        if (!items.length) return null;
        return (
          <div key={col.id}>
            <div className="section-title">
              {col.label} <span className="badge">{items.length}</span>
            </div>
            {items.map((t) => (
              <TaskCard key={t.id} t={t} open={open === t.id} onToggleOpen={() => setOpen(open === t.id ? null : t.id)} onMove={move} onCheck={toggle} onRemove={remove} onOpenSession={onOpen} />
            ))}
          </div>
        );
      })}
      {showDone && finished.length > 0 && (
        <div>
          <div className="section-title">
            Finished <span className="badge">{finished.length}</span>
          </div>
          {finished.map((t) => (
            <TaskCard key={t.id} t={t} open={open === t.id} onToggleOpen={() => setOpen(open === t.id ? null : t.id)} onMove={move} onCheck={toggle} onRemove={remove} onOpenSession={onOpen} />
          ))}
        </div>
      )}
    </>
  );
}

function TaskCard({
  t,
  open,
  onToggleOpen,
  onMove,
  onCheck,
  onRemove,
  onOpenSession,
}: {
  t: Task;
  open: boolean;
  onToggleOpen: () => void;
  onMove: (t: Task, s: Task["status"]) => void;
  onCheck: (t: Task, i: number) => void;
  onRemove: (t: Task) => void;
  onOpenSession: (id: string) => void;
}) {
  const done = t.checklist.filter((c) => c.done).length;
  const next: Partial<Record<Task["status"], Task["status"][]>> = {
    todo: ["doing", "dropped"],
    doing: ["review", "done", "todo"],
    review: ["done", "doing"],
    blocked: ["todo"],
    done: ["todo"],
    dropped: ["todo"],
  };
  return (
    <div className="card pressable" onClick={onToggleOpen}>
      <div className="row">
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="title">
            <span className="badge">p{t.priority}</span> {t.title}
          </div>
          <div className="sub">
            {t.id}
            {t.checklist.length > 0 && ` · ${done}/${t.checklist.length}`}
            {t.depends_on.length > 0 && ` · after ${t.depends_on.join(", ")}`}
            {` · ${timeAgo(t.updated_at)}`}
            {t.session_id && (
              <>
                {" · "}
                <a
                  onClick={(e) => {
                    e.stopPropagation();
                    onOpenSession(t.session_id!);
                  }}
                >
                  session
                </a>
              </>
            )}
          </div>
        </div>
      </div>
      {open && (
        <div onClick={(e) => e.stopPropagation()} style={{ marginTop: 8 }}>
          {t.acceptance && (
            <div className="sub" style={{ marginBottom: 6 }}>
              <b>acceptance:</b> {t.acceptance}
            </div>
          )}
          {t.checklist.map((c, i) => (
            <label key={i} className="sub" style={{ display: "block", cursor: "pointer" }}>
              <input type="checkbox" checked={c.done} onChange={() => onCheck(t, i)} /> {c.text}
            </label>
          ))}
          {t.notes && <pre className="diff" style={{ whiteSpace: "pre-wrap", marginTop: 6 }}>{t.notes}</pre>}
          <div className="btnrow">
            {(next[t.status] ?? []).map((s) => (
              <button key={s} className="btn small" onClick={() => onMove(t, s)}>
                → {s}
              </button>
            ))}
            <button className="btn small danger" onClick={() => onRemove(t)}>
              delete
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
