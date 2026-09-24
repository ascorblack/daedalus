// The orchestrator's wake-ups: the alarms it set itself and the ones the operator left it, with a
// way to cancel any of them and to leave a new one with a note. One component serves the centre of
// focus mode and the right panel's tab (`compact`), like every page of a project.

import { useState } from "react";
import { api, type Wakeup } from "../api";
import { Skeleton } from "../components";
import { Sheet } from "../dialogs";
import { absTime, describeCron, relTime, untilShort } from "../format";
import { t } from "../i18n";
import { Icon } from "../icons";
import { PageHeader } from "../shell";
import { invalidate, useQuery } from "../store";
import { errorText } from "../ui";
import { useProject, wakeupsKey } from "./data";
import { WAKEUP_WHENS, wakeupBody, wakeupWhen, type WakeupDraft, type WakeupWhen } from "./wakeupmodel";

const enc = encodeURIComponent;

type Props = { projectId: string; back?: string | null; compact?: boolean; toast?: (text: string) => void };

export function WakeupsPage({ projectId, back, compact, toast }: Props) {
  const { project } = useProject(projectId);
  const { data, error, refresh } = useQuery<{ wakeups: Wakeup[]; max: number }>(wakeupsKey(projectId), { pollMs: 30000, staleMs: 5000 });
  const [adding, setAdding] = useState(false);
  const say = toast ?? (() => undefined);
  const enabled = !!project?.settings.orchestrator?.enabled;
  const list = data?.wakeups ?? [];

  async function cancel(w: Wakeup) {
    try {
      await api.delete(`${wakeupsKey(projectId)}/${enc(w.id)}`);
      say(t("focus.wakeups.cancelled"));
      invalidate(wakeupsKey(projectId));
    } catch (e) {
      say(errorText(e));
    }
  }

  const body = (
    <div className="wakeups">
      <section className="wakeup-section" aria-label={t("focus.wakeups.title")}>
        <div className="wakeup-section-head">
          <span className="wakeup-section-title">{t("focus.wakeups.title")}</span>
          <span className="grow" />
          {enabled && <button className="btn small" onClick={() => setAdding(true)}><Icon name="plus" size={14} /> {t("focus.wakeups.add")}</button>}
        </div>
        {!data && !error && <Skeleton rows={2} />}
        {error && !data && <div className="empty"><div>{error}</div><button className="btn" onClick={refresh}>{t("common.retry")}</button></div>}
        {data && list.length === 0 && <div className="empty calm">{t(enabled ? "focus.wakeups.empty" : "focus.wakeups.off")}</div>}
        {list.map((w) => (
          <div key={w.id} className={`wakeup-row ${w.enabled ? "" : "off"}`}>
            <Icon name="clock" size={16} />
            <div className="wakeup-main">
              <div className="wakeup-name">{w.note}</div>
              <div className="wakeup-meta sub">
                <span>{wakeupWhen(w)}</span>
                {w.next_run_at && <span title={absTime(w.next_run_at)}> · {t("focus.wakeups.next", { when: untilShort(w.next_run_at) })}</span>}
              </div>
              <div className="wakeup-meta sub faint">
                {t(w.set_by === "operator" ? "focus.wakeups.by.operator" : "focus.wakeups.by.orchestrator")} · {relTime(w.created_at)}
                {w.cron && <span className="mono"> · {w.cron} UTC</span>}
              </div>
            </div>
            <button className="iconbtn small quiet" onClick={() => void cancel(w)} title={t("focus.wakeups.cancel")} aria-label={t("focus.wakeups.cancel")}>
              <Icon name="close" size={16} />
            </button>
          </div>
        ))}
      </section>
      {adding && <WakeupSheet projectId={projectId} onClose={() => setAdding(false)} toast={say} />}
    </div>
  );
  if (compact) return body;
  return (
    <>
      <PageHeader title={project ? t("focus.wakeups.page", { name: project.name }) : t("focus.wakeups.title")} back={back ?? undefined} />
      <div className="screen narrow">{body}</div>
    </>
  );
}

function WakeupSheet({ projectId, onClose, toast }: { projectId: string; onClose: () => void; toast: (text: string) => void }) {
  const soon = new Date(Date.now() + 3600000);
  const [draft, setDraft] = useState<WakeupDraft>({ note: "", when: "in", minutes: "30", date: soon.toLocaleDateString("en-CA"), time: soon.toTimeString().slice(0, 5), cron: "" });
  const [busy, setBusy] = useState(false);
  const set = (patch: Partial<WakeupDraft>) => setDraft((d) => ({ ...d, ...patch }));
  const request = wakeupBody(draft);
  // The moment is previewed before the note is written, so a time that will not do shows at once.
  const timing = wakeupBody({ ...draft, note: "·" });
  const preview = timing?.cron ? describeCron(timing.cron) : timing?.at ? t("fmt.once", { when: absTime(timing.at) }) : timing?.in_minutes ? untilShort(Date.now() + timing.in_minutes * 60000) : "";

  async function save() {
    if (!request) return;
    setBusy(true);
    try {
      await api.post(wakeupsKey(projectId), request);
      toast(t("focus.wakeups.added"));
      invalidate(wakeupsKey(projectId));
      onClose();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet title={t("focus.wakeups.new")} onClose={onClose} size="narrow" className="wakeup-sheet">
      <label className="field" htmlFor="wakeup-note">{t("focus.wakeups.note")}</label>
      <textarea id="wakeup-note" className="field" rows={3} maxLength={500} autoFocus value={draft.note} placeholder={t("focus.wakeups.note.hint")} onChange={(e) => set({ note: e.target.value })} />
      <label className="field">{t("sched.when")}</label>
      <div className="chips" aria-label={t("sched.when")}>
        {WAKEUP_WHENS.map((w: WakeupWhen) => (
          <button key={w} className="chip select" aria-pressed={draft.when === w} onClick={() => set({ when: w })}>{t(w === "cron" ? "sched.when.cron" : `focus.wakeups.when.${w}`)}</button>
        ))}
      </div>
      {draft.when === "in" && (
        <>
          <label className="field" htmlFor="wakeup-minutes">{t("focus.wakeups.minutes")}</label>
          <input id="wakeup-minutes" className="field" type="number" inputMode="numeric" min={1} value={draft.minutes} onChange={(e) => set({ minutes: e.target.value })} />
        </>
      )}
      {draft.when === "once" && (
        <div className="grid2">
          <div><label className="field" htmlFor="wakeup-date">{t("sched.date")}</label><input id="wakeup-date" className="field" type="date" value={draft.date} onChange={(e) => set({ date: e.target.value })} /></div>
          <div><label className="field" htmlFor="wakeup-time">{t("sched.time")}</label><input id="wakeup-time" className="field" type="time" value={draft.time} onChange={(e) => set({ time: e.target.value })} /></div>
        </div>
      )}
      {draft.when === "daily" && (
        <>
          <label className="field" htmlFor="wakeup-daily">{t("sched.time")}</label>
          <input id="wakeup-daily" className="field" type="time" value={draft.time} onChange={(e) => set({ time: e.target.value })} />
        </>
      )}
      {draft.when === "cron" && (
        <>
          <label className="field" htmlFor="wakeup-cron">{t("sched.cron.label")}</label>
          <input id="wakeup-cron" className="field mono" placeholder="0 9 * * 1-5" value={draft.cron} onChange={(e) => set({ cron: e.target.value })} />
        </>
      )}
      <div className="sub preview-line">{preview ? t("focus.wakeups.preview", { when: preview }) : t("sched.preview.none")}</div>
      <div className="sub form-hint">{t("focus.wakeups.hint")}</div>
      <div className="sheet-foot">
        <button className="btn ghost" onClick={onClose}>{t("common.cancel")}</button>
        <button className="btn primary" disabled={busy || !request} onClick={() => void save()}>{t("focus.wakeups.set")}</button>
      </div>
    </Sheet>
  );
}
