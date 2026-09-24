import { useState } from "react";
import { api, Notification, NotificationPage, NotificationSummary, Proposal } from "../api";
import { Skeleton } from "../components";
import { OverflowMenu, Sheet, deleteWithUndo } from "../dialogs";
import { absTime, dayLabel, relTime } from "../format";
import { Icon, IconName } from "../icons";
import { navigate, pathFor } from "../router";
import { PageHeader, screenTitle } from "../shell";
import { hold, invalidate, prime, release, useQuery } from "../store";
import { errorText } from "../ui";
import { plural, t } from "../i18n";

type Filter = "all" | "unseen" | "problems";

const SUMMARY = "/api/notifications/summary";

const KIND_ICON: Record<string, IconName> = { rebuild: "wrench", run_failed: "stop", run_cap: "stop", schedule: "clock", schedule_run: "clock", reminder: "clock", service: "globe", loop: "loop", loop_paused: "pause", heartbeat: "dot", inbound: "inbox", board_stale: "board", webhook_failed: "globe", learning_digest: "bulb", boot_guard: "wrench", change_proposal: "changes" };

function kindIcon(kind: string): IconName {
  if (KIND_ICON[kind]) return KIND_ICON[kind];
  if (kind.includes("fail") || kind.includes("error")) return "stop";
  if (kind.includes("schedule")) return "clock";
  if (kind.includes("service")) return "globe";
  return "inbox";
}

const kindLabel = (kind: string) => kind.replace(/_/g, " ");

/** The colour of the icon: the entry's tone, except that a quiet record stays grey whatever it says. */
const toneClass = (e: Notification) => (e.level === "quiet" && e.tone === "info" ? "quiet" : e.tone);

export function InboxScreen({ toast, onOpen }: { toast: (t: string) => void; onOpen: (id: string) => void }) {
  const [filter, setFilter] = useState<Filter>("all");
  const key = `/api/notifications?view=${filter}&limit=200`;
  const { data, error, loading, refresh } = useQuery<NotificationPage>(key, { pollMs: 15000, staleMs: 5000 });
  const proposals = useQuery<Proposal[]>("/api/proposals", { pollMs: 60000, staleMs: 30000 });
  const [open, setOpen] = useState<number | null>(null);
  const unseen = data?.summary.unseen ?? 0;
  const entries = data?.entries ?? [];
  const pending = (proposals.data ?? []).filter((p) => p.status === "pending");

  /** Show a change before the server has confirmed it; the badge follows from the same summary. */
  function patch(fn: (list: Notification[]) => Notification[], summary?: NotificationSummary) {
    if (!data) return;
    const next = summary ?? data.summary;
    prime(key, { ...data, entries: fn(data.entries), summary: next });
    prime(SUMMARY, next);
  }

  async function markAll() {
    try {
      const r = await api.post<{ marked: number; summary: NotificationSummary }>("/api/notifications/seen", { all: true });
      patch((l) => l.map((e) => ({ ...e, seen: true })), r.summary);
      refresh();
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function markSeen(entry: Notification) {
    if (entry.seen) return;
    patch((l) => l.map((e) => (e.id === entry.id ? { ...e, seen: true } : e)));
    try {
      const r = await api.post<{ marked: number; summary: NotificationSummary }>("/api/notifications/seen", { ids: [entry.id] });
      prime(SUMMARY, r.summary);
    } catch (e) {
      toast(errorText(e));
    }
  }

  function toggle(entry: Notification) {
    const next = open === entry.id ? null : entry.id;
    setOpen(next);
    if (next !== null) void markSeen(entry);
  }

  function remove(entry: Notification) {
    const before = data;
    hold(key);
    patch((l) => l.filter((e) => e.id !== entry.id));
    setOpen(null);
    deleteWithUndo(
      plural("inbox.deleted", 1),
      async () => {
        try {
          await api.delete(`/api/notifications/${entry.id}`);
        } finally {
          release(key);
          refresh();
          invalidate(SUMMARY);
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
        title={screenTitle("inbox")}
        subtitle={unseen > 0 ? plural("inbox.unread", unseen) : undefined}
        actions={<button className="iconbtn" onClick={markAll} disabled={unseen === 0} title={t("inbox.markall")} aria-label={t("inbox.markall")}><Icon name="check" /></button>}
      >
        <div className="chips">
          <button className="chip select" aria-pressed={filter === "all"} onClick={() => setFilter("all")}>{t("common.all")}</button>
          <button className="chip select" aria-pressed={filter === "unseen"} onClick={() => setFilter("unseen")}>{t("inbox.filter.unread")}{unseen > 0 ? ` · ${unseen}` : ""}</button>
          <button className="chip select" aria-pressed={filter === "problems"} onClick={() => setFilter("problems")}>{t("inbox.filter.problems")}</button>
        </div>
      </PageHeader>
      <div className="screen narrow">
        {pending.length > 0 && filter !== "problems" && (
          <section>
            <div className="section-title">{t("inbox.waiting")} <span className="n">{pending.length}</span></div>
            {pending.map((p) => (
              <ProposalCard key={p.id} p={p} toast={toast} onDone={() => { proposals.refresh(); invalidate("/api/proposals"); }} />
            ))}
          </section>
        )}
        {loading && !error && <Skeleton rows={6} />}
        {error && !data && <div className="empty"><b>{t("inbox.error")}</b><div>{error}</div><button className="btn" onClick={refresh}>{t("common.retry")}</button></div>}
        {data && entries.length === 0 && pending.length === 0 && (
          <div className="empty">
            <b>{t(filter === "unseen" ? "inbox.empty.unread" : filter === "problems" ? "inbox.empty.problems" : "inbox.empty")}</b>
          </div>
        )}
        {entries.map((entry) => {
          const day = dayLabel(entry.updated_at);
          const heading = day !== lastDay ? day : null;
          lastDay = day;
          return (
            <div key={entry.id}>
              {heading && <div className="section-title">{heading}</div>}
              <InboxRow entry={entry} open={open === entry.id} onToggle={() => toggle(entry)} onOpen={onOpen} onRemove={() => remove(entry)} onUnseen={() => patch((l) => l.map((e) => (e.id === entry.id ? { ...e, seen: false } : e)))} />
            </div>
          );
        })}
      </div>
    </>
  );
}

function InboxRow({ entry, open, onToggle, onOpen, onRemove, onUnseen }: { entry: Notification; open: boolean; onToggle: () => void; onOpen: (id: string) => void; onRemove: () => void; onUnseen: () => void }) {
  const tone = toneClass(entry);
  return (
    <div className={`erow inbox ${entry.seen ? "" : "unread"}`} role="button" tabIndex={0} aria-expanded={open} onClick={onToggle} onKeyDown={(e) => { if (e.target !== e.currentTarget) return; if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onToggle(); } }}>
      <span className={`kind ${tone}`} aria-label={entry.tone}>
        <Icon name={kindIcon(entry.kind)} size={16} />
      </span>
      <div className="erow-main">
        <div className="erow-head">
          <span className={`erow-title ${open ? "" : "clamp-2"}`}>{entry.count > 1 ? `${entry.count} × ${entry.title}` : entry.title}</span>
          <span className="erow-time num" title={absTime(entry.updated_at)}>{relTime(entry.updated_at)}</span>
        </div>
        <div className="erow-meta">
          <span>{kindLabel(entry.kind)}</span>
        </div>
        {open && (
          <div className="inbox-body" onClick={(e) => e.stopPropagation()}>
            {entry.body && <pre className="inbox-text">{entry.body}</pre>}
            <div className="btnrow">
              {entry.session_id && (
                <button className="btn small" onClick={() => onOpen(entry.session_id!)}>
                  <Icon name="bots" size={14} /> {t("inbox.open.session")}
                </button>
              )}
              <span className="grow" />
              <OverflowMenu
                small
                label={t("inbox.actions")}
                items={[
                  { label: t("inbox.markunread"), icon: "inbox", onSelect: onUnseen },
                  "-",
                  { label: t("common.delete"), icon: "trash", danger: true, onSelect: onRemove },
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
          <button className="btn small primary" disabled={busy} onClick={() => decide("approve")}>{t("inbox.approve")}</button>
          <button className="btn small" disabled={busy} onClick={() => setRejecting(true)}>{t("inbox.reject")}</button>
          <button className="btn small ghost" onClick={() => navigate(pathFor("changes", p.id))}>{t("inbox.diff")}</button>
        </div>
      </div>
      {rejecting && (
        <Sheet title={t("inbox.reject.title")} onClose={() => setRejecting(false)} size="narrow">
          <div className="sub">{p.title}</div>
          <label className="field">{t("inbox.reason")}</label>
          <textarea className="field" rows={3} autoFocus value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("inbox.reason.placeholder")} />
          <div className="sheet-foot">
            <button className="btn ghost" onClick={() => setRejecting(false)}>{t("common.cancel")}</button>
            <button className="btn danger solid" disabled={busy} onClick={() => decide("reject")}>{t("inbox.reject.do")}</button>
          </div>
        </Sheet>
      )}
    </div>
  );
}
