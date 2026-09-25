// A staff member's session seen from inside the project: a header that says who this is, what runs
// it, where it works and on what, with the three controls the team runtime offers; and, above the
// composer, the messages sent to it with how far each one got. The command-line staff view reuses
// both, so neither assumes a Daedalus session underneath.

import type { ReactNode } from "react";
import { api, type StaffMessage } from "../api";
import { clock } from "../format";
import { t } from "../i18n";
import { applyMessageEvent, stripRows } from "../staff/model";
import { invalidate, peek, prime, useQuery } from "../store";
import { HarnessBadge, StaffAvatar } from "../team/parts";
import { HARNESS_NAMES, type Staff } from "../team/team";
import { confirmAsync, errorText } from "../ui";
import { useFocus } from "./data";
import { firstWait, staffTone, waitKey } from "./focus";
import { useEvent } from "../events";

const memberKey = (id: string) => `/api/staff/${encodeURIComponent(id)}`;
export const messagesKey = (id: string, n = 3) => `/api/staff/${encodeURIComponent(id)}/messages?limit=${n}`;

/** One member, kept current by its events; `null` asks nothing (a session that is nobody's work). */
export function useMember(staffId: string | null) {
  const member = useQuery<Staff>(staffId ? memberKey(staffId) : null, { pollMs: 15000, staleMs: 3000 });
  useEvent(["staff."], (event) => {
    if (staffId && event.staff_id === staffId) invalidate(memberKey(staffId));
  }, [staffId]);
  return member;
}

/**
 * `facts` goes after the status in the pill ("turn 4 · 18 min"); `details` replaces the line under the
 * name with what the caller knows better (a command-line member's version, worktree and task).
 */
export function StaffHeader({ projectId, staffId, toast, facts, details, children }: { projectId: string; staffId: string; toast: (text: string) => void; facts?: string; details?: ReactNode; children?: ReactNode }) {
  const { data: member } = useMember(staffId);
  const { board } = useFocus(projectId);
  if (!member) return null;
  const live = member.live;
  const tone = staffTone(member);
  const task = live?.task_id ? board?.tasks.find((x) => x.id === live.task_id) : undefined;
  const wait = firstWait(member);
  const runs = [HARNESS_NAMES[member.harness], member.model, member.permission_mode].filter(Boolean).join(" · ");
  async function act(what: "interrupt" | "pause" | "release") {
    if (!member) return;
    if (what === "release" && !(await confirmAsync(t("focus.staff.release.title", { name: member.name }), { body: t("focus.staff.release.body"), action: t("focus.staff.release") }))) return;
    try {
      await api.post(`/api/staff/${encodeURIComponent(member.id)}/${what}`, what === "release" ? { keep_worktree: true } : undefined);
      toast(t(what === "interrupt" ? "focus.staff.interrupted" : what === "pause" ? "focus.staff.pausing" : "focus.staff.released", { name: member.name }));
      invalidate(memberKey(member.id));
      invalidate(`/api/projects/${encodeURIComponent(projectId)}/staff`);
    } catch (e) {
      toast(errorText(e));
    }
  }
  return (
    <div className="staff-head">
      <div className="staff-head-row">
        <StaffAvatar name={member.name} color={member.color} />
        <div className="staff-head-who">
          <div className="staff-head-name truncate">{member.name}{member.role && <span className="staff-head-role"> · {member.role}</span>}</div>
          {details ?? (
            <div className="staff-head-meta truncate">
              <HarnessBadge harness={member.harness} />
              <span>{runs}</span>
              {live?.branch && <span className="mono">{t("focus.staff.branch", { branch: live.branch })}</span>}
              {task && <span>{t("focus.staff.task", { title: task.title })}</span>}
            </div>
          )}
        </div>
        <span className={`focus-pill tone-${tone}`} title={wait?.detail || live?.waiting_for || undefined} data-status={member.status}>
          <span className={`focus-dot tone-${tone}`} aria-hidden />
          {wait ? t("focus.wait.line", { reason: t(waitKey(wait.reason)), n: wait.position }) : live?.pause_requested ? t("focus.staff.paused") : t(`team.status.${member.status}`)}
          {facts && !wait && <span className="staff-head-facts"> · {facts}</span>}
        </span>
        {live && (
          <div className="staff-head-actions">
            <button className="btn small" onClick={() => void act("interrupt")}>{t("focus.staff.interrupt")}</button>
            <button className="btn small" onClick={() => void act("pause")} disabled={live.pause_requested}>{t("focus.staff.pause")}</button>
            <button className="btn small ghost" onClick={() => void act("release")}>{t("focus.staff.release")}</button>
          </div>
        )}
      </div>
      {children}
    </div>
  );
}

/**
 * The last messages sent to the member, newest last, each with its delivery receipt. A receipt moves
 * the moment its `staff.message` event arrives (the list is read again after, for anything the event
 * does not carry); a message that failed can be sent again from here, as a new message.
 */
export function StaffMessages({ staffId, n = 2, toast }: { staffId: string; n?: number; toast?: (text: string) => void }) {
  const { data: member } = useMember(staffId);
  const key = messagesKey(staffId, Math.max(3, n));
  const { data } = useQuery<StaffMessage[]>(key, { pollMs: 15000, staleMs: 3000 });
  useEvent(["staff.message"], (event) => {
    if (event.staff_id !== staffId) return;
    const held = peek<StaffMessage[]>(key);
    if (Array.isArray(held)) prime(key, applyMessageEvent(held, event.payload ?? {}));
    invalidate(`/api/staff/${encodeURIComponent(staffId)}/messages`);
  }, [staffId, key]);
  const rows = stripRows(Array.isArray(data) ? data : [], n);
  if (!member || rows.length === 0) return null;
  async function retry(m: StaffMessage) {
    try {
      await api.post(`/api/staff/${encodeURIComponent(staffId)}/messages`, { text: m.text, mode: m.mode === "steer" || m.mode === "interrupt" ? m.mode : "queue" });
      invalidate(`/api/staff/${encodeURIComponent(staffId)}/messages`);
    } catch (e) {
      toast?.(errorText(e));
    }
  }
  return (
    <div className="staff-messages" aria-label={t("focus.staff.messages", { name: member.name })}>
      {rows.map((m) => (
        <div key={m.id} className="staff-message" data-message={m.id} data-state={m.state}>
          <span className="staff-message-from">{t(`focus.msg.from.${m.origin}`, { name: member.name })} · {clock(m.created_at)}</span>
          <span className={`pill msg-state ${m.state}`} title={m.error || undefined}>{t(`focus.msg.state.${m.state}`)}</span>
          <span className="staff-message-text truncate">{t("focus.msg.quote", { text: m.text })}</span>
          {m.state === "failed" && m.origin === "operator" && <button className="btn small ghost staff-message-retry" onClick={() => void retry(m)}>{t("delivery.retry")}</button>}
        </div>
      ))}
    </div>
  );
}
