import { useMemo, useState } from "react";
import { api, Schedule } from "../api";
import { Pill, Skeleton } from "../components";
import { OverflowMenu, Sheet } from "../dialogs";
import { absTime, cronFor, describeCron, describeSchedule, relTime, untilShort } from "../format";
import { Icon } from "../icons";
import { navigate, pathFor } from "../router";
import { PageHeader } from "../shell";
import { invalidate, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";
import { useSessionTitles } from "./Sessions";

type Group = "upcoming" | "paused" | "done";

function groupOf(s: Schedule): Group {
  if (s.enabled && s.next_run_at) return "upcoming";
  if (!s.enabled && (s.cron || (s.run_at && !s.last_run_at))) return "paused";
  return "done";
}

function lastOutcome(s: Schedule): { status: string; word: string } | null {
  if (!s.last_run_at) return null;
  if (s.failure_count > 0) return { status: "failed", word: `${s.failure_count} failed` };
  return { status: "done", word: "ran" };
}

const KIND_WORD: Record<Schedule["kind"], string> = { agent: "Agent task", message: "Reminder", lazy: "Lazy note" };

export function SchedulesScreen({ toast, onOpen, selected }: { toast: (t: string) => void; onOpen: (id: string) => void; selected?: string | null }) {
  const { data: items, error, loading, refresh } = useQuery<Schedule[]>("/api/schedules", { pollMs: 30000, staleMs: 5000 });
  const titles = useSessionTitles();
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Schedule | null>(null);
  const reload = () => {
    refresh();
    invalidate("/api/schedules");
  };
  const groups = useMemo(() => {
    const all = items ?? [];
    const by: Record<Group, Schedule[]> = { upcoming: [], paused: [], done: [] };
    for (const s of all) by[groupOf(s)].push(s);
    by.upcoming.sort((a, b) => Date.parse(a.next_run_at ?? "") - Date.parse(b.next_run_at ?? ""));
    by.done.sort((a, b) => Date.parse(b.last_run_at ?? "") - Date.parse(a.last_run_at ?? ""));
    return by;
  }, [items]);
  const open = selected ? (items ?? []).find((s) => s.id === selected) ?? null : null;

  async function remove(s: Schedule) {
    if (!(await confirmAsync(`Delete "${s.name}"?`, { body: s.cron ? "It will not run again. Sessions it started are not affected." : "The one-off is removed.", action: "Delete schedule" }))) return;
    try {
      await api.delete(`/api/schedules/${s.id}`);
      if (selected === s.id) navigate(pathFor("schedules"), { replace: true });
      reload();
    } catch (e) {
      toast(errorText(e));
    }
  }
  async function runNow(s: Schedule) {
    try {
      const r = await api.post<{ session_id: string }>(`/api/schedules/${s.id}/run`);
      toast("started");
      reload();
      if (r.session_id) onOpen(r.session_id);
    } catch (e) {
      toast(errorText(e));
    }
  }
  async function setEnabled(s: Schedule, enabled: boolean) {
    try {
      await api.patch(`/api/schedules/${s.id}`, { enabled });
      toast(enabled ? "resumed" : "paused");
      reload();
    } catch (e) {
      toast(errorText(e));
    }
  }

  const row = (s: Schedule) => {
    const outcome = lastOutcome(s);
    const target = s.target_session ? titles[s.target_session] : undefined;
    return (
      <div key={s.id} className="erow schedule" role="link" tabIndex={0} onClick={() => navigate(pathFor("schedules", s.id))} onKeyDown={(e) => { if (e.target !== e.currentTarget) return; if (e.key === "Enter" || e.key === " ") { e.preventDefault(); navigate(pathFor("schedules", s.id)); } }}>
        <span className={`kind ${s.enabled ? "" : "muted"}`}><Icon name={s.kind === "message" ? "inbox" : s.kind === "lazy" ? "bulb" : "clock"} size={16} /></span>
        <div className="erow-main">
          <div className="erow-head">
            <span className="erow-title clamp-2">{s.name}</span>
            {s.enabled && s.next_run_at ? <span className="erow-time num">{untilShort(s.next_run_at)}</span> : outcome ? <Pill status={outcome.status}>{outcome.word}</Pill> : null}
          </div>
          <div className="erow-meta">
            <span>{describeSchedule(s)}</span>
            {!s.enabled && s.cron && <span className="sep">·</span>}
            {!s.enabled && s.cron && <span className="word">paused</span>}
          </div>
          <div className="erow-meta">
            <span>{KIND_WORD[s.kind] ?? s.kind}</span>
            {s.run_in === "self" && target && <span className="sep">·</span>}
            {s.run_in === "self" && target && <span>in {target}</span>}
            {s.last_run_at && <span className="sep">·</span>}
            {s.last_run_at && <span title={absTime(s.last_run_at)}>last {relTime(s.last_run_at)}</span>}
            {s.active_session_id && <span className="sep">·</span>}
            {s.active_session_id && <span className="word running">running now</span>}
          </div>
        </div>
      </div>
    );
  };

  return (
    <>
      <PageHeader
        title="Schedules"
        subtitle={items ? `${groups.upcoming.length} upcoming${groups.paused.length ? ` · ${groups.paused.length} paused` : ""}` : undefined}
        actions={<button className="iconbtn primary" onClick={() => setCreating(true)} title="New schedule" aria-label="New schedule"><Icon name="plus" /></button>}
      />
      <div className="screen narrow">
        {loading && !error && <Skeleton rows={4} />}
        {error && !items && <div className="empty"><b>Could not load the schedules</b><div>{error}</div><button className="btn" onClick={refresh}>Retry</button></div>}
        {items && items.length === 0 && (
          <div className="empty">
            <b>No schedules yet</b>
            <div>A task the agent runs at a set time, or a reminder for you.</div>
            <button className="btn primary" onClick={() => setCreating(true)}>New schedule</button>
          </div>
        )}
        {(["upcoming", "paused", "done"] as Group[]).map((g) =>
          groups[g].length === 0 ? null : (
            <section key={g}>
              <div className="section-title">
                {g === "upcoming" ? "Upcoming" : g === "paused" ? "Paused" : "Done"} <span className="n">{groups[g].length}</span>
              </div>
              {groups[g].map(row)}
            </section>
          ),
        )}
      </div>
      {creating && <ScheduleForm onClose={() => setCreating(false)} onSaved={() => { setCreating(false); reload(); }} toast={toast} />}
      {editing && <ScheduleForm existing={editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); reload(); }} toast={toast} />}
      {open && !editing && (
        <Sheet
          title={open.name}
          onClose={() => navigate(pathFor("schedules"), { replace: true })}
          head={
            <OverflowMenu
              small
              label="Schedule actions"
              items={[
                { label: "Edit", icon: "pen", onSelect: () => setEditing(open) },
                open.enabled ? { label: "Pause", icon: "pause", onSelect: () => setEnabled(open, false) } : { label: "Resume", icon: "play", onSelect: () => setEnabled(open, true), disabled: !open.cron && !!open.last_run_at },
                "-",
                { label: "Delete…", icon: "trash", danger: true, onSelect: () => remove(open) },
              ]}
            />
          }
        >
          <div className="kv"><span>When</span><b>{describeSchedule(open)}</b></div>
          {open.cron && <div className="kv"><span>Cron (UTC)</span><b className="mono">{open.cron}</b></div>}
          <div className="kv"><span>Next run</span><b>{open.enabled && open.next_run_at ? `${absTime(open.next_run_at)} · ${untilShort(open.next_run_at)}` : open.enabled ? "—" : "paused"}</b></div>
          {open.last_run_at && <div className="kv"><span>Last run</span><b>{absTime(open.last_run_at)}{open.failure_count ? ` · ${open.failure_count} failed` : ""}</b></div>}
          <div className="kv"><span>Kind</span><b>{KIND_WORD[open.kind] ?? open.kind}{open.run_in === "self" ? ` · in ${(open.target_session && titles[open.target_session]) || "its session"}` : open.kind === "agent" ? " · own session each run" : ""}</b></div>
          {open.last_error && <div className="kv"><span>Last error</span><b style={{ color: "var(--bad)" }}>{open.last_error}</b></div>}
          <section className="sheet-section">
            <div className="sheet-section-title">{open.kind === "agent" ? "Instruction" : "Text"}</div>
            <div className="proposal-text">{open.prompt}</div>
          </section>
          {open.last_summary && (
            <section className="sheet-section">
              <div className="sheet-section-title">Last result</div>
              <div className="proposal-text">{open.last_summary}</div>
            </section>
          )}
          <div className="sheet-foot">
            {open.active_session_id && <button className="btn" onClick={() => onOpen(open.active_session_id!)}>Open the running session</button>}
            <button className="btn primary" onClick={() => runNow(open)}><Icon name="play" size={14} /> Run now</button>
          </div>
        </Sheet>
      )}
    </>
  );
}

type When = "once" | "daily" | "weekdays" | "weekly" | "hours" | "cron";
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** When, in the reader's own clock: a moment, a daily or weekly time, or every few hours; cron stays for the rest. */
function ScheduleForm({ existing, onClose, onSaved, toast }: { existing?: Schedule; onClose: () => void; onSaved: () => void; toast: (t: string) => void }) {
  const [name, setName] = useState(existing?.name ?? "");
  const [kind, setKind] = useState<"agent" | "message">(existing?.kind === "message" ? "message" : "agent");
  const [prompt, setPrompt] = useState(existing?.prompt ?? "");
  const [when, setWhen] = useState<When>(existing?.cron ? "cron" : "once");
  const [date, setDate] = useState(() => (existing?.run_at ? new Date(existing.run_at) : new Date(Date.now() + 3600000)).toLocaleDateString("en-CA"));
  const [time, setTime] = useState(() => (existing?.run_at ? new Date(existing.run_at) : new Date(Date.now() + 3600000)).toTimeString().slice(0, 5));
  const [days, setDays] = useState<number[]>([1, 3, 5]);
  const [every, setEvery] = useState("2");
  const [cron, setCron] = useState(existing?.cron ?? "");
  const [busy, setBusy] = useState(false);
  const [h, m] = time.split(":").map(Number);

  const built = (() => {
    if (when === "once") {
      const d = new Date(`${date}T${time}`);
      return { run_at: Number.isNaN(d.getTime()) ? null : d.toISOString(), cron: null };
    }
    if (when === "cron") return { run_at: null, cron: cron.trim() || null };
    if (when === "weekly") return { run_at: null, cron: cronFor("weekly", h, m, days.map((d) => (d + 1) % 7)) };
    if (when === "hours") return { run_at: null, cron: cronFor("hours", h, m, [], Number(every) || 1) };
    return { run_at: null, cron: cronFor(when, h, m) };
  })();
  const preview = built.cron ? describeCron(built.cron) : built.run_at ? `Once · ${absTime(built.run_at)}` : "";
  const valid = name.trim() && prompt.trim() && (built.cron || built.run_at) && (when !== "weekly" || days.length > 0);

  async function save() {
    setBusy(true);
    try {
      if (existing) await api.patch(`/api/schedules/${existing.id}`, { name: name.trim(), prompt: prompt.trim(), cron: built.cron, run_at: built.run_at });
      else await api.post("/api/schedules", { name: name.trim(), prompt: prompt.trim(), cron: built.cron, run_at: built.run_at, kind });
      toast(existing ? "schedule updated" : "schedule created");
      onSaved();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet title={existing ? "Edit schedule" : "New schedule"} onClose={onClose}>
      <label className="field">Name</label>
      <input className="field" autoFocus value={name} onChange={(e) => setName(e.target.value)} />
      {!existing && (
        <>
          <label className="field">Type</label>
          <div className="segmented inline" role="radiogroup">
            <button role="radio" aria-checked={kind === "agent"} className={kind === "agent" ? "on" : ""} onClick={() => setKind("agent")}>Agent task</button>
            <button role="radio" aria-checked={kind === "message"} className={kind === "message" ? "on" : ""} onClick={() => setKind("message")}>Reminder</button>
          </div>
          <div className="sub" style={{ marginTop: 4 }}>{kind === "agent" ? "Runs the instruction as a task in a session of its own." : "Delivers the text to you; no model call."}</div>
        </>
      )}
      <label className="field">{kind === "agent" ? "Instruction" : "Reminder text"}</label>
      <textarea className="field" rows={4} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
      <label className="field">When</label>
      <div className="chips">
        {(["once", "daily", "weekdays", "weekly", "hours", "cron"] as When[]).map((w) => (
          <button key={w} className="chip select" aria-pressed={when === w} onClick={() => setWhen(w)}>
            {w === "once" ? "Once" : w === "daily" ? "Every day" : w === "weekdays" ? "Weekdays" : w === "weekly" ? "Weekly" : w === "hours" ? "Every N hours" : "Cron"}
          </button>
        ))}
      </div>
      {when === "once" && (
        <div className="grid2">
          <div><label className="field">Date</label><input className="field" type="date" value={date} onChange={(e) => setDate(e.target.value)} /></div>
          <div><label className="field">Time</label><input className="field" type="time" value={time} onChange={(e) => setTime(e.target.value)} /></div>
        </div>
      )}
      {(when === "daily" || when === "weekdays" || when === "weekly" || when === "hours") && (
        <div className="grid2">
          <div><label className="field">{when === "hours" ? "Starting at minute" : "Time"}</label><input className="field" type="time" value={time} onChange={(e) => setTime(e.target.value)} /></div>
          {when === "hours" && <div><label className="field">Every (hours)</label><input className="field" type="number" min={1} max={24} value={every} onChange={(e) => setEvery(e.target.value)} /></div>}
        </div>
      )}
      {when === "weekly" && (
        <>
          <label className="field">Days</label>
          <div className="chips">
            {DAYS.map((d, i) => (
              <button key={d} className="chip select" aria-pressed={days.includes(i)} onClick={() => setDays((cur) => (cur.includes(i) ? cur.filter((x) => x !== i) : [...cur, i].sort()))}>{d}</button>
            ))}
          </div>
        </>
      )}
      {when === "cron" && (
        <>
          <label className="field">Cron expression (UTC, 5 fields)</label>
          <input className="field mono" placeholder="0 4 * * 1-5" value={cron} onChange={(e) => setCron(e.target.value)} />
        </>
      )}
      <div className="sub preview-line">{preview ? `Runs: ${preview}` : "Pick a valid moment."}{built.cron && when !== "cron" ? <span className="faint"> · cron {built.cron}</span> : null}</div>
      <div className="sheet-foot">
        <button className="btn ghost" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={busy || !valid} onClick={save}>{existing ? "Save" : "Create"}</button>
      </div>
    </Sheet>
  );
}
