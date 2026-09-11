import { useMemo, useState } from "react";
import { api, Proposal } from "../api";
import { Skeleton } from "../components";
import { OverflowMenu, Sheet, deleteWithUndo } from "../dialogs";
import { absTime, dayLabel, relTime } from "../format";
import { Icon, IconName } from "../icons";
import { navigate, pathFor } from "../router";
import { PageHeader } from "../shell";
import { hold, invalidate, prime, release, useQuery } from "../store";
import { errorText } from "../ui";

type Entry = {
  id: number;
  at: string;
  kind: string;
  severity: "info" | "notice" | "warning" | "error";
  title: string;
  body: string;
  session_id: string | null;
  run_id: string | null;
  read: number;
};
type Listing = { entries: Entry[]; unread: number };
type Filter = "all" | "unread" | "problems";

const KIND_ICON: Record<string, IconName> = { rebuild: "wrench", run_failed: "stop", schedule: "clock", schedule_run: "clock", service: "globe", loop: "loop", loop_paused: "pause", heartbeat: "dot", inbound: "inbox", board_stale: "board", webhook_failed: "globe", learning_digest: "bulb", boot_guard: "wrench", proposal: "changes" };

function kindIcon(kind: string): IconName {
  if (KIND_ICON[kind]) return KIND_ICON[kind];
  if (kind.includes("fail") || kind.includes("error")) return "stop";
  if (kind.includes("schedule")) return "clock";
  if (kind.includes("service")) return "globe";
  return "inbox";
}

const kindLabel = (kind: string) => kind.replace(/_/g, " ");

/** Same kind, same title shape, within ten minutes: one card with a count, not five. */
type Group = { key: string; entries: Entry[]; kind: string; severity: Entry["severity"]; title: string; at: string; unread: number; session_id: string | null };

function groupEntries(entries: Entry[]): Group[] {
  const out: Group[] = [];
  for (const e of entries) {
    const shape = e.title.replace(/'[^']*'/g, "'…'").replace(/#\d+/g, "#…").replace(/\d+/g, "N");
    const last = out[out.length - 1];
    const within = last && Math.abs(Date.parse(last.entries[last.entries.length - 1].at) - Date.parse(e.at)) < 10 * 60000;
    if (last && last.kind === e.kind && last.key === `${e.kind}|${shape}` && within) {
      last.entries.push(e);
      last.unread += e.read ? 0 : 1;
      if (SEV[e.severity] > SEV[last.severity]) last.severity = e.severity;
      continue;
    }
    out.push({ key: `${e.kind}|${shape}`, entries: [e], kind: e.kind, severity: e.severity, title: e.title, at: e.at, unread: e.read ? 0 : 1, session_id: e.session_id });
  }
  return out;
}
const SEV: Record<Entry["severity"], number> = { info: 0, notice: 1, warning: 2, error: 3 };

export function InboxScreen({ toast, onOpen }: { toast: (t: string) => void; onOpen: (id: string) => void }) {
  const [filter, setFilter] = useState<Filter>("all");
  const key = `/api/inbox?unread=${filter === "unread" ? 1 : 0}&limit=200`;
  const { data, error, loading, refresh } = useQuery<Listing>(key, { pollMs: 15000, staleMs: 5000 });
  const proposals = useQuery<Proposal[]>("/api/proposals", { pollMs: 60000, staleMs: 30000 });
  const [open, setOpen] = useState<string | null>(null);
  const unread = data?.unread ?? 0;

  const entries = useMemo(() => {
    const all = data?.entries ?? [];
    if (filter === "problems") return all.filter((e) => e.severity === "error" || e.severity === "warning");
    return all;
  }, [data, filter]);
  const groups = useMemo(() => groupEntries(entries), [entries]);
  const openKey = (g: Group) => `${g.key}|${g.entries[0].id}`;
  const pending = (proposals.data ?? []).filter((p) => p.status === "pending");

  function patch(fn: (list: Entry[]) => Entry[], unreadDelta = 0) {
    if (!data) return;
    prime(key, { entries: fn(data.entries), unread: Math.max(0, data.unread + unreadDelta) });
    invalidate("/api/inbox/unread");
  }

  async function markAll() {
    try {
      await api.post("/api/inbox/read", {});
      patch((l) => l.map((e) => ({ ...e, read: 1 })), -unread);
      refresh();
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function markRead(ids: number[]) {
    const fresh = ids.filter((id) => data?.entries.find((e) => e.id === id && !e.read));
    if (!fresh.length) return;
    patch((l) => l.map((e) => (fresh.includes(e.id) ? { ...e, read: 1 } : e)), -fresh.length);
    try {
      await api.post("/api/inbox/read", { ids: fresh });
    } catch (e) {
      toast(errorText(e));
    }
  }

  function toggle(g: Group) {
    const next = open === openKey(g) ? null : openKey(g);
    setOpen(next);
    if (next) void markRead(g.entries.map((e) => e.id));
  }

  function remove(g: Group) {
    const ids = g.entries.map((e) => e.id);
    const before = data;
    hold(key);
    patch((l) => l.filter((e) => !ids.includes(e.id)), -g.unread);
    setOpen(null);
    deleteWithUndo(
      ids.length === 1 ? "Entry deleted" : `${ids.length} entries deleted`,
      async () => {
        try {
          for (const id of ids) await api.delete(`/api/inbox/${id}`);
        } finally {
          release(key);
          refresh();
        }
      },
      () => {
        release(key);
        if (before) prime(key, before);
      },
      (e) => toast(errorText(e)),
    );
  }

  // Day headings between the cards.
  let lastDay = "";
  return (
    <>
      <PageHeader
        title="Inbox"
        subtitle={unread > 0 ? `${unread} unread` : undefined}
        actions={<button className="iconbtn" onClick={markAll} disabled={unread === 0} title="Mark all read" aria-label="Mark all read"><Icon name="check" /></button>}
      >
        <div className="chips">
          <button className="chip select" aria-pressed={filter === "all"} onClick={() => setFilter("all")}>All</button>
          <button className="chip select" aria-pressed={filter === "unread"} onClick={() => setFilter("unread")}>Unread{unread > 0 ? ` · ${unread}` : ""}</button>
          <button className="chip select" aria-pressed={filter === "problems"} onClick={() => setFilter("problems")}>Problems</button>
        </div>
      </PageHeader>
      <div className="screen narrow">
        {pending.length > 0 && filter !== "problems" && (
          <section>
            <div className="section-title">Waiting for you <span className="n">{pending.length}</span></div>
            {pending.map((p) => (
              <ProposalCard key={p.id} p={p} toast={toast} onDone={() => { proposals.refresh(); invalidate("/api/proposals"); }} />
            ))}
          </section>
        )}
        {loading && !error && <Skeleton rows={6} />}
        {error && !data && <div className="empty"><b>Could not load the inbox</b><div>{error}</div><button className="btn" onClick={refresh}>Retry</button></div>}
        {data && groups.length === 0 && pending.length === 0 && (
          <div className="empty">
            <b>{filter === "unread" ? "Nothing unread" : filter === "problems" ? "No problems" : "Nothing happened while you were away"}</b>
          </div>
        )}
        {groups.map((g) => {
          const day = dayLabel(g.at);
          const heading = day !== lastDay ? day : null;
          lastDay = day;
          return (
            <div key={g.key + g.entries[0].id}>
              {heading && <div className="section-title">{heading}</div>}
              <InboxRow g={g} open={open === openKey(g)} onToggle={() => toggle(g)} onOpen={onOpen} onRemove={() => remove(g)} onUnread={() => patch((l) => l.map((e) => (g.entries.some((x) => x.id === e.id) ? { ...e, read: 0 } : e)), g.entries.length)} />
            </div>
          );
        })}
      </div>
    </>
  );
}

function InboxRow({ g, open, onToggle, onOpen, onRemove, onUnread }: { g: Group; open: boolean; onToggle: () => void; onOpen: (id: string) => void; onRemove: () => void; onUnread: () => void }) {
  const many = g.entries.length > 1;
  const names = many ? g.entries.map((e) => /'([^']*)'/.exec(e.title)?.[1] ?? "").filter(Boolean) : [];
  const title = many ? `${g.entries.length} × ${g.title.replace(/'[^']*'/, "'…'")}` : g.title;
  return (
    <div className={`erow inbox ${g.unread ? "unread" : ""} sev-${g.severity}`} role="button" tabIndex={0} aria-expanded={open} onClick={onToggle} onKeyDown={(e) => { if (e.target !== e.currentTarget) return; if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onToggle(); } }}>
      <span className={`kind ${g.severity}`} aria-label={g.severity}>
        <Icon name={kindIcon(g.kind)} size={16} />
      </span>
      <div className="erow-main">
        <div className="erow-head">
          <span className={`erow-title ${open ? "" : "clamp-2"}`}>{title}</span>
          <span className="erow-time num" title={absTime(g.at)}>{relTime(g.at)}</span>
        </div>
        <div className="erow-meta">
          <span>{kindLabel(g.kind)}</span>
          {names.length > 0 && !open && <span className="sep">·</span>}
          {names.length > 0 && !open && <span>{names.join(", ")}</span>}
        </div>
        {open && (
          <div className="inbox-body" onClick={(e) => e.stopPropagation()}>
            {g.entries.map((e) => (
              <div key={e.id} className="inbox-entry">
                {many && <div className="inbox-entry-title">{e.title} <span className="sub faint">{absTime(e.at)}</span></div>}
                {e.body && <pre className="inbox-text">{e.body}</pre>}
              </div>
            ))}
            <div className="btnrow">
              {g.session_id && (
                <button className="btn small" onClick={() => onOpen(g.session_id!)}>
                  <Icon name="bots" size={14} /> Open session
                </button>
              )}
              <span className="grow" />
              <OverflowMenu
                small
                label="Entry actions"
                items={[
                  { label: "Mark unread", icon: "inbox", onSelect: onUnread },
                  "-",
                  { label: many ? `Delete ${g.entries.length} entries` : "Delete", icon: "trash", danger: true, onSelect: onRemove },
                ]}
              />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/** A change waiting for a decision: approve here, reject with a reason, or read the diff on Changes. */
export function ProposalCard({ p, toast, onDone }: { p: Proposal; toast: (t: string) => void; onDone: () => void }) {
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  async function decide(decision: "approve" | "reject") {
    setBusy(true);
    try {
      const r = await api.post<{ result: string }>(`/api/proposals/${p.id}/decide`, { decision, reason });
      toast(r.result);
      setRejecting(false);
      onDone();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="erow action">
      <span className="kind warning"><Icon name="changes" size={16} /></span>
      <div className="erow-main">
        <div className="erow-head">
          <span className="erow-title clamp-2">{p.title}</span>
          <span className="erow-time num" title={absTime(p.created_at)}>{relTime(p.created_at)}</span>
        </div>
        <div className="erow-meta">
          <span className="mono">{p.branch}</span>
          {p.pr_number && <span className="sep">·</span>}
          {p.pr_number && <span className="nowrap">PR #{p.pr_number}</span>}
        </div>
        {p.summary && <div className="sub clamp-3" style={{ marginTop: 4 }}>{p.summary}</div>}
        <div className="btnrow">
          <button className="btn small primary" disabled={busy} onClick={() => decide("approve")}>Approve</button>
          <button className="btn small" disabled={busy} onClick={() => setRejecting(true)}>Reject…</button>
          <button className="btn small ghost" onClick={() => navigate(pathFor("changes", p.id))}>Diff</button>
        </div>
      </div>
      {rejecting && (
        <Sheet title="Reject this change" onClose={() => setRejecting(false)} size="narrow">
          <div className="sub">{p.title}</div>
          <label className="field">Reason (sent to the agent)</label>
          <textarea className="field" rows={3} autoFocus value={reason} onChange={(e) => setReason(e.target.value)} placeholder="What is wrong, or what to do instead" />
          <div className="sheet-foot">
            <button className="btn ghost" onClick={() => setRejecting(false)}>Cancel</button>
            <button className="btn danger solid" disabled={busy} onClick={() => decide("reject")}>Reject</button>
          </div>
        </Sheet>
      )}
    </div>
  );
}
