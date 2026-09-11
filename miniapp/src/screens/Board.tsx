import { useEffect, useState } from "react";
import { api } from "../api";
import { Skeleton, copyText } from "../components";
import { OverflowMenu, Sheet } from "../dialogs";
import { absTime, relTime } from "../format";
import { Icon } from "../icons";
import { navigate, pathFor } from "../router";
import { PageHeader } from "../shell";
import { invalidate, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";
import { useSessionTitles } from "./Sessions";

type Status = "todo" | "doing" | "review" | "done" | "blocked" | "dropped";
type Task = {
  id: string;
  title: string;
  status: Status;
  priority: number;
  acceptance: string;
  checklist: { text: string; done: boolean }[];
  depends_on: string[];
  session_id: string | null;
  notes: string;
  created_at: string;
  updated_at: string;
};

const COLUMNS: { id: Status; label: string }[] = [
  { id: "todo", label: "To do" },
  { id: "doing", label: "Doing" },
  { id: "review", label: "Review" },
  { id: "blocked", label: "Blocked" },
];
const FINISHED: Status[] = ["done", "dropped"];
const STATUS_LABEL: Record<Status, string> = { todo: "To do", doing: "Doing", review: "Review", done: "Done", blocked: "Blocked", dropped: "Dropped" };
const NEXT: Record<Status, Status[]> = { todo: ["doing", "blocked", "dropped"], doing: ["review", "done", "blocked", "todo"], review: ["done", "doing"], blocked: ["todo", "doing"], done: ["todo"], dropped: ["todo"] };

export function BoardScreen({ toast, onOpen, selected }: { toast: (t: string) => void; onOpen: (id: string) => void; selected?: string | null }) {
  const [showDone, setShowDone] = useState(false);
  const key = `/api/board?include_done=${showDone ? 1 : 0}`;
  const { data: tasks, error, loading, refresh } = useQuery<Task[]>(key, { pollMs: 20000, staleMs: 5000 });
  const titles = useSessionTitles();
  const [creating, setCreating] = useState(false);
  const [dragging, setDragging] = useState<string | null>(null);
  const [over, setOver] = useState<Status | null>(null);

  const reload = () => {
    refresh();
    invalidate("/api/board");
  };
  async function move(t: Task, status: Status) {
    if (t.status === status) return;
    try {
      await api.put(`/api/board/${t.id}`, { status });
      reload();
    } catch (e) {
      toast(errorText(e));
    }
  }
  async function check(t: Task, i: number) {
    // The board refuses a checklist edit that would leave a finished task with an item open; the
    // refusal shows as a toast, not as a dead checkbox.
    try {
      await api.put(`/api/board/${t.id}`, t.checklist[i].done ? { uncheck: [i] } : { check: [i] });
      reload();
    } catch (e) {
      toast(errorText(e));
    }
  }
  async function remove(t: Task) {
    if (!(await confirmAsync(`Delete "${t.title}"?`, { body: "The task leaves the board. Sessions that worked on it are not affected.", action: "Delete task" }))) return;
    try {
      await api.delete(`/api/board/${t.id}`);
      if (selected === t.id) navigate(pathFor("board"), { replace: true });
      reload();
    } catch (e) {
      toast(errorText(e));
    }
  }

  const all = tasks ?? [];
  const finished = all.filter((t) => FINISHED.includes(t.status));
  const openCount = all.filter((t) => !FINISHED.includes(t.status)).length;
  const open = selected ? all.find((t) => t.id === selected) ?? null : null;
  // A link to a finished task widens the filter so the task can be shown.
  useEffect(() => {
    if (selected && tasks && !open && !showDone) setShowDone(true);
  }, [selected, tasks, open, showDone]);
  const column = (status: Status) => all.filter((t) => t.status === status).sort((a, b) => a.priority - b.priority || Date.parse(b.updated_at) - Date.parse(a.updated_at));
  const card = (t: Task) => (
    <TaskRow key={t.id} t={t} owner={t.session_id ? titles[t.session_id] : undefined} onOpen={() => navigate(pathFor("board", t.id))} onDragStart={() => setDragging(t.id)} onDragEnd={() => { setDragging(null); setOver(null); }} dragging={dragging === t.id} />
  );
  const dropProps = (status: Status) => ({
    onDragOver: (e: React.DragEvent) => {
      if (!dragging) return;
      e.preventDefault();
      if (over !== status) setOver(status);
    },
    onDragLeave: (e: React.DragEvent) => {
      if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
      if (over === status) setOver(null);
    },
    onDrop: (e: React.DragEvent) => {
      e.preventDefault();
      const id = dragging ?? e.dataTransfer.getData("text/plain");
      const t = all.find((x) => x.id === id);
      setDragging(null);
      setOver(null);
      if (t) void move(t, status);
    },
  });

  return (
    <>
      <PageHeader
        title="Board"
        subtitle={tasks ? `${openCount} open${finished.length && showDone ? ` · ${finished.length} finished` : ""}` : undefined}
        actions={<button className="iconbtn primary" onClick={() => setCreating(true)} title="New task" aria-label="New task"><Icon name="plus" /></button>}
      >
        <div className="chips">
          <button className="chip select" aria-pressed={!showDone} onClick={() => setShowDone(false)}>Open</button>
          <button className="chip select" aria-pressed={showDone} onClick={() => setShowDone(true)}>With finished</button>
        </div>
      </PageHeader>
      <div className="screen wide board">
        {loading && !error && <Skeleton rows={4} />}
        {error && !tasks && <div className="empty"><b>Could not load the board</b><div>{error}</div><button className="btn" onClick={refresh}>Retry</button></div>}
        {tasks && tasks.length === 0 && (
          <div className="empty">
            <b>No tasks yet</b>
            <div>The agent keeps this board itself; a task you add here is picked up on its next run.</div>
            <button className="btn primary" onClick={() => setCreating(true)}>Add task</button>
          </div>
        )}
        {tasks && tasks.length > 0 && (
          <div className={`kanban ${showDone ? "five" : ""}`}>
            {COLUMNS.map((col) => {
              const items = column(col.id);
              return (
                <section key={col.id} className={`kanban-col ${over === col.id ? "over" : ""} ${items.length === 0 ? "is-empty" : ""}`} {...dropProps(col.id)}>
                  <div className="section-title">
                    {col.label} <span className="n">{items.length}</span>
                  </div>
                  {items.map(card)}
                  {items.length === 0 && <div className="kanban-empty">—</div>}
                </section>
              );
            })}
            {showDone && (
              <section className={`kanban-col ${over === "done" ? "over" : ""}`} {...dropProps("done")}>
                <div className="section-title">
                  Finished <span className="n">{finished.length}</span>
                </div>
                {finished.map(card)}
              </section>
            )}
          </div>
        )}
      </div>
      {creating && <NewTaskSheet onClose={() => setCreating(false)} onCreated={() => { setCreating(false); reload(); }} toast={toast} />}
      {open && <TaskSheet t={open} owner={open.session_id ? titles[open.session_id] : undefined} onClose={() => navigate(pathFor("board"), { replace: true })} onMove={move} onCheck={check} onRemove={remove} onOpenSession={onOpen} toast={toast} />}
    </>
  );
}

function TaskRow({ t, owner, onOpen, onDragStart, onDragEnd, dragging }: { t: Task; owner?: string; onOpen: () => void; onDragStart: () => void; onDragEnd: () => void; dragging: boolean }) {
  const done = t.checklist.filter((c) => c.done).length;
  const next = t.checklist.find((c) => !c.done);
  const pct = t.checklist.length ? Math.round((100 * done) / t.checklist.length) : 0;
  return (
    <div className={`erow task p${Math.min(t.priority, 4)} ${dragging ? "dragging" : ""}`} role="link" tabIndex={0} draggable onDragStart={(e) => { e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", t.id); onDragStart(); }} onDragEnd={onDragEnd} onClick={onOpen} onKeyDown={(e) => { if (e.target !== e.currentTarget) return; if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(); } }}>
      <div className="erow-main">
        <div className="erow-head">
          <span className="erow-title clamp-3">{t.title}</span>
          <span className="erow-time num" title={absTime(t.updated_at)}>{relTime(t.updated_at)}</span>
        </div>
        {t.checklist.length > 0 && (
          <div className="task-check">
            <div className={`bar ${pct === 100 ? "ok" : ""}`} style={{ ["--v" as string]: pct }}><i /></div>
            <span className="num sub">{done}/{t.checklist.length}</span>
          </div>
        )}
        {next && <div className="erow-meta"><span className="faint">next:</span><span>{next.text}</span></div>}
        <div className="erow-meta">
          {t.priority <= 2 && <span className={`chip ${t.priority === 1 ? "bad" : "attn"}`}>P{t.priority}</span>}
          {owner && <span>{owner}</span>}
          {t.depends_on.length > 0 && owner && <span className="sep">·</span>}
          {t.depends_on.length > 0 && <span>after {t.depends_on.length} task{t.depends_on.length === 1 ? "" : "s"}</span>}
        </div>
      </div>
    </div>
  );
}

function TaskSheet({ t, owner, onClose, onMove, onCheck, onRemove, onOpenSession, toast }: { t: Task; owner?: string; onClose: () => void; onMove: (t: Task, s: Status) => void; onCheck: (t: Task, i: number) => void; onRemove: (t: Task) => void; onOpenSession: (id: string) => void; toast: (t: string) => void }) {
  const done = t.checklist.filter((c) => c.done).length;
  return (
    <Sheet
      title={t.title}
      onClose={onClose}
      head={
        <OverflowMenu
          small
          label="Task actions"
          items={[
            ...(t.session_id ? [{ label: owner ? `Open ${owner}` : "Open session", icon: "bots" as const, onSelect: () => onOpenSession(t.session_id!) }] : []),
            { label: "Copy task id", icon: "copy", onSelect: async () => toast((await copyText(t.id)) ? "task id copied" : t.id) },
            "-",
            { label: "Delete task…", icon: "trash", danger: true, onSelect: () => onRemove(t) },
          ]}
        />
      }
    >
      <div className="erow-meta" style={{ marginBottom: 10 }}>
        <span className="chip">{STATUS_LABEL[t.status]}</span>
        <span className={`chip ${t.priority === 1 ? "bad" : t.priority === 2 ? "attn" : ""}`}>P{t.priority}</span>
        {owner && <span className="sep">·</span>}
        {owner && <button className="linkbtn" onClick={() => onOpenSession(t.session_id!)}>{owner}</button>}
        <span className="sep">·</span>
        <span title={absTime(t.updated_at)}>updated {relTime(t.updated_at)}</span>
      </div>
      {t.acceptance && (
        <section className="sheet-section">
          <div className="sheet-section-title">Acceptance</div>
          <div className="proposal-text">{t.acceptance}</div>
        </section>
      )}
      {t.checklist.length > 0 && (
        <section className="sheet-section">
          <div className="sheet-section-title">Checklist <span className="sub">{done}/{t.checklist.length}</span></div>
          {t.checklist.map((c, i) => (
            <label key={i} className="toggle-row check-row">
              <input type="checkbox" checked={c.done} onChange={() => onCheck(t, i)} />
              <span className={c.done ? "done" : ""}>{c.text}</span>
            </label>
          ))}
        </section>
      )}
      {t.depends_on.length > 0 && (
        <section className="sheet-section">
          <div className="sheet-section-title">Depends on</div>
          <div className="sub mono">{t.depends_on.join(", ")}</div>
        </section>
      )}
      {t.notes && (
        <section className="sheet-section">
          <div className="sheet-section-title">Notes</div>
          <pre className="inbox-text">{t.notes}</pre>
        </section>
      )}
      <section className="sheet-section">
        <div className="sheet-section-title">Move to</div>
        <div className="btnrow" style={{ marginTop: 0 }}>
          {NEXT[t.status].map((s) => (
            <button key={s} className={`btn small ${s === "done" ? "primary" : ""}`} onClick={() => onMove(t, s)}>
              {STATUS_LABEL[s]}
            </button>
          ))}
        </div>
      </section>
    </Sheet>
  );
}

function NewTaskSheet({ onClose, onCreated, toast }: { onClose: () => void; onCreated: () => void; toast: (t: string) => void }) {
  const [form, setForm] = useState({ title: "", acceptance: "", checklist: "", priority: 3 });
  const [busy, setBusy] = useState(false);
  async function create() {
    setBusy(true);
    try {
      await api.post("/api/board", {
        title: form.title,
        acceptance: form.acceptance,
        priority: form.priority,
        checklist: form.checklist.split("\n").map((l) => l.trim()).filter(Boolean),
      });
      onCreated();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet title="New task" onClose={onClose}>
      <label className="field">Title</label>
      <input className="field" autoFocus value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} />
      <label className="field">Acceptance (how anyone can tell it is done)</label>
      <textarea className="field" rows={2} value={form.acceptance} onChange={(e) => setForm({ ...form, acceptance: e.target.value })} />
      <label className="field">Checklist (one item per line)</label>
      <textarea className="field" rows={3} value={form.checklist} onChange={(e) => setForm({ ...form, checklist: e.target.value })} />
      <label className="field">Priority</label>
      <div className="segmented inline" role="radiogroup">
        {[1, 2, 3, 4, 5].map((p) => (
          <button key={p} role="radio" aria-checked={form.priority === p} className={form.priority === p ? "on" : ""} onClick={() => setForm({ ...form, priority: p })}>P{p}</button>
        ))}
      </div>
      <div className="sub" style={{ marginTop: 4 }}>P1 is the most urgent.</div>
      <div className="sheet-foot">
        <button className="btn ghost" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={busy || !form.title.trim()} onClick={create}>Create</button>
      </div>
    </Sheet>
  );
}
