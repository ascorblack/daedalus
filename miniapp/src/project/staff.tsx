// A staff member's session seen from inside the project: a header that says who this is, what runs
// it, where it works and on what, with the three controls the team runtime offers; and, above the
// composer, the messages sent to it with how far each one got. The command-line staff view reuses
// both, so neither assumes a Daedalus session underneath.

import { api, type StaffMessage } from "../api";
import { clock } from "../format";
import { t } from "../i18n";
import { invalidate, useQuery } from "../store";
import { HarnessBadge, StaffAvatar } from "../team/parts";
import { HARNESS_NAMES, type Staff } from "../team/team";
import { confirmAsync, errorText } from "../ui";
import { useFocus } from "./data";
import { firstWait, staffTone, waitKey } from "./focus";
import { useEvent } from "../events";

const memberKey = (id: string) => `/api/staff/${encodeURIComponent(id)}`;
const messagesKey = (id: string) => `/api/staff/${encodeURIComponent(id)}/messages?limit=3`;

/** One member, kept current by its events; `null` asks nothing (a session that is nobody's work). */
export function useMember(staffId: string | null) {
  const member = useQuery<Staff>(staffId ? memberKey(staffId) : null, { pollMs: 15000, staleMs: 3000 });
  useEvent(["staff."], (event) => {
    if (staffId && event.staff_id === staffId) invalidate(memberKey(staffId));
  }, [staffId]);
  return member;
}

export function StaffHeader({ projectId, staffId, toast }: { projectId: string; staffId: string; toast: (text: string) => void }) {
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
          <div className="staff-head-meta truncate">
            <HarnessBadge harness={member.harness} />
            <span>{runs}</span>
            {live?.branch && <span className="mono">{t("focus.staff.branch", { branch: live.branch })}</span>}
            {task && <span>{t("focus.staff.task", { title: task.title })}</span>}
          </div>
        </div>
        <span className={`focus-pill tone-${tone}`} title={wait?.detail || live?.waiting_for || undefined}>
          <span className={`focus-dot tone-${tone}`} aria-hidden />
          {wait ? t("focus.wait.line", { reason: t(waitKey(wait.reason)), n: wait.position }) : live?.pause_requested ? t("focus.staff.paused") : t(`team.status.${member.status}`)}
        </span>
        {live && (
          <div className="staff-head-actions">
            <button className="btn small" onClick={() => void act("interrupt")}>{t("focus.staff.interrupt")}</button>
            <button className="btn small" onClick={() => void act("pause")} disabled={live.pause_requested}>{t("focus.staff.pause")}</button>
            <button className="btn small ghost" onClick={() => void act("release")}>{t("focus.staff.release")}</button>
          </div>
        )}
      </div>
    </div>
  );
}

/** The last messages sent to the member, newest last, each with its delivery receipt. */
export function StaffMessages({ staffId }: { staffId: string }) {
  const { data: member } = useMember(staffId);
  const { data } = useQuery<StaffMessage[]>(messagesKey(staffId), { pollMs: 15000, staleMs: 3000 });
  useEvent(["staff.message"], (event) => {
    if (event.staff_id === staffId) invalidate(`/api/staff/${encodeURIComponent(staffId)}/messages`);
  }, [staffId]);
  const rows = (Array.isArray(data) ? data : []).slice(0, 2).reverse();
  if (!member || rows.length === 0) return null;
  return (
    <div className="staff-messages" aria-label={t("focus.staff.messages", { name: member.name })}>
      {rows.map((m) => (
        <div key={m.id} className="staff-message">
          <span className="staff-message-from">{t(`focus.msg.from.${m.origin}`, { name: member.name })} · {clock(m.created_at)}</span>
          <span className={`pill msg-state ${m.state}`} title={m.error || undefined}>{t(`focus.msg.state.${m.state}`)}</span>
          <span className="staff-message-text truncate">{t("focus.msg.quote", { text: m.text })}</span>
        </div>
      ))}
    </div>
  );
}
