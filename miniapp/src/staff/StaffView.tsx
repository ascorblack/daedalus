// A command-line staff member from inside its project (M5): the real TUI in a terminal the operator
// can type into, or the same transcript as a Feed; a banner while the operator's own typing holds the
// orchestrator's messages back; its open requests answered in place; and a composer that says whether
// a message waits for the turn or goes now.
//
// The messages sent to it, with how far each got, are listed in the Session tab beside the terminal,
// not under it. A strip of them between the terminal and the composer took the terminal's rows and,
// on a phone, most of the screen; the header shows a small marker only while one is on its way or
// failed, and the marker opens the list. The Feed also shows those same messages after its last turn,
// where they will appear once the member takes them.
//
// Nothing here knows which CLI runs the member. What "now" means, whether "Always" is offered and
// what the status channel is called come from the capability table the host sends with the session,
// so Claude Code, Codex, OpenCode, pi and Grok Build are drawn by the same code.

import { FormEvent, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, type Ask, type HarnessCapabilities, type StaffChanges, type StaffEventRow, type StaffMessage, type StaffSessionView, type TerminalView as TerminalRow } from "../api";
import { Sheet } from "../dialogs";
import { useEvent } from "../events";
import { relTime, tokens, usd } from "../format";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { AskAnswers } from "../project/phone";
import { useFocus } from "../project/data";
import { MessageRow, StaffHeader, useMember, useStaffMessages } from "../project/staff";
import { navigate, pathFor, projectPagePath } from "../router";
import { invalidate, useQuery } from "../store";
import { HarnessBadge } from "../team/parts";
import { HARNESS_NAMES } from "../team/team";
import { TerminalView } from "../terminal/view";
import type { TerminalState } from "../terminal/instance";
import { errorText } from "../ui";
import { FeedView, useTurns } from "./FeedView";
import { HealthLine } from "./health";
import { attention, canAlways, channelWords, defaultMode, keyboardBlocks, listRows, nowChoice, openRequests, outboxRows, turnFacts, type StaffViewMode } from "./model";

const enc = encodeURIComponent;
const MODE_KEY = "daedalus.staff.view";
const SIDE_KEY = "daedalus.staff.side";
/** How many of the member's messages the Session tab lists, newest first. */
const MESSAGES_N = 20;

function saved(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function save(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* a webview without site data: the choice lasts this load */
  }
}

/** The member's session as its runtime knows it, kept current by the member's events. */
function useStaffSession(staffId: string) {
  const key = `/api/staff/${enc(staffId)}/session`;
  const query = useQuery<StaffSessionView>(key, { pollMs: 10000, staleMs: 2000 });
  useEvent(["staff.", "permission.", "ask."], (event) => {
    if (event.staff_id === staffId) invalidate(key);
  }, [staffId]);
  return query;
}

/** The member's open requests, oldest first, whoever they are routed to: the first answer wins. */
function useRequests(projectId: string, staffId: string): Ask[] {
  const key = `/api/asks?project=${enc(projectId)}`;
  const { data } = useQuery<{ asks: Ask[] }>(key, { pollMs: 10000, staleMs: 2000 });
  useEvent(["ask.", "permission."], (event) => {
    if (event.project_id === projectId) invalidate(key);
  }, [projectId]);
  return openRequests(Array.isArray(data?.asks) ? data!.asks : [], staffId);
}

export function StaffView({ projectId, staffId, wide, toast, onBack }: { projectId: string; staffId: string; wide: boolean; toast: (text: string) => void; onBack: () => void }) {
  const { data: member } = useMember(staffId);
  const { data: view } = useStaffSession(staffId);
  const { board } = useFocus(projectId);
  const session = view?.session ?? null;
  const caps = view?.capabilities ?? null;
  const terminalId = session?.terminal_id ?? member?.live?.terminal_id ?? null;
  const [mode, setMode] = useState<StaffViewMode>(() => defaultMode(wide ? 1440 : 390, wide ? saved(MODE_KEY) : null, true));
  const [side, setSide] = useState(() => saved(SIDE_KEY) !== "closed");
  const [sheet, setSheet] = useState(false);
  const [tab, setTab] = useState<SideTab>("session");
  const [reveal, setReveal] = useState(0);
  const [terminalState, setTerminalState] = useState<TerminalState | null>(null);
  const messages = useStaffMessages(staffId, MESSAGES_N);
  const need = attention(messages);
  const live = !!session;
  const feed = useTurns(staffId, live);
  const turns = feed.turns;
  // A phone shows the Feed; its terminal is the full-screen one with the row of keys, a tap away.
  const shown: StaffViewMode = wide && terminalId ? mode : "feed";

  // Opening the view is reading the finished turn: the member stops being "finished, unread".
  const seenFor = useRef("");
  useEffect(() => {
    if (!session || session.status !== "turn_done_unseen" || seenFor.current === session.status_at) return;
    if (document.visibilityState !== "visible") return;
    seenFor.current = session.status_at;
    void api.post(`/api/staff/${enc(staffId)}/seen`).then(() => invalidate(`/api/staff/${enc(staffId)}`), () => undefined);
  }, [session, staffId]);

  if (!member) return <div className="chat in-project staff-cli"><div className="empty">{t("common.loading")}</div></div>;
  const facts = turnFacts(turns, session?.status_at);
  const factsText = facts.turn > 0 ? t("staff.facts", { turn: facts.turn, minutes: facts.minutes ?? 0 }) : undefined;
  const task = session?.task_id ? board?.tasks.find((x) => x.id === session.task_id) : undefined;
  const launch = view?.launch;
  const version = launch?.version ? `${HARNESS_NAMES[member.harness]} ${launch.version}` : HARNESS_NAMES[member.harness];
  const runs = [version, launch?.model || member.model, launch?.permission_mode || member.permission_mode].filter(Boolean).join(" · ");
  const branch = launch?.branch || session?.branch;

  function pick(next: StaffViewMode) {
    if (next === "terminal" && !wide) {
      if (terminalId) navigate(pathFor("terminals", terminalId));
      return;
    }
    setMode(next);
    save(MODE_KEY, next);
  }
  function toggleSide() {
    if (!wide) return setSheet(true);
    setSide((open) => {
      save(SIDE_KEY, open ? "closed" : "open");
      return !open;
    });
  }
  // The header's marker: the Session tab, open, with its Messages section in view.
  function showMessages() {
    setTab("session");
    setReveal((n) => n + 1);
    if (!wide) return setSheet(true);
    if (!side) {
      save(SIDE_KEY, "open");
      setSide(true);
    }
  }
  const flagged = need.failed.length > 0 ? need.failed.length : need.pending.length;
  const flaggedText = need.failed.length > 0 ? plural("staff.attention.failed", need.failed.length) : plural("staff.attention.pending", need.pending.length);
  const panel = (
    <StaffPanel projectId={projectId} staffId={staffId} name={member.name} view={view ?? null} notes={member.notes} instructions={member.instructions} taskId={session?.task_id ?? null}
      tab={tab} onTab={setTab} messages={messages} reveal={reveal} toast={toast} />
  );
  const outbox = outboxRows(messages);

  return (
    <div className="chat in-project staff-cli" data-staff={staffId} data-mode={shown}>
      <div className="chat-head">
        {!wide && (
          <button className="iconbtn" onClick={onBack} aria-label={t("shell.back")} title={t("shell.back")}>
            <Icon name="back" />
          </button>
        )}
        <div className="grow chat-identity" style={{ minWidth: 0 }}>
          <span className="chat-title truncate">{member.name}</span>
        </div>
        <div className="segmented inline staff-mode" role="group" aria-label={t("staff.mode")}>
          <button className={shown === "feed" ? "on" : ""} aria-pressed={shown === "feed"} data-mode="feed" onClick={() => pick("feed")}>{t("staff.mode.feed")}</button>
          <button className={shown === "terminal" ? "on" : ""} aria-pressed={shown === "terminal"} data-mode="terminal" disabled={!terminalId} title={terminalId ? undefined : t("staff.mode.noterminal")} onClick={() => pick("terminal")}>{t("staff.mode.terminal")}</button>
        </div>
        <div className="head-actions">
          {flagged > 0 && (
            <button
              className={`staff-attention ${need.failed.length > 0 ? "failed" : "pending"}`}
              data-attention={need.failed.length > 0 ? "failed" : "pending"}
              onClick={showMessages}
              aria-label={`${flaggedText} · ${t("staff.attention.open")}`}
              title={flaggedText}
            >
              <Icon name={need.failed.length > 0 ? "alert" : "send"} size={14} />
              <span className="staff-attention-n">{flagged}</span>
            </button>
          )}
          <button className={`iconbtn ${wide && side ? "on" : ""}`} onClick={toggleSide} aria-label={t("staff.side")} title={t("staff.side")} aria-pressed={wide ? side : sheet}>
            <Icon name="panel" />
          </button>
        </div>
      </div>
      <StaffHeader
        projectId={projectId}
        staffId={staffId}
        toast={toast}
        facts={factsText}
        details={
          <div className="staff-head-meta truncate">
            <HarnessBadge harness={member.harness} />
            <span className="staff-head-runs">{runs}</span>
            {branch && <span className="mono">{t("staff.worktree", { branch })}</span>}
            {task && <span>{t("focus.staff.task", { title: task.title })}</span>}
          </div>
        }
      >
        {/* A phone's header keeps only what is wrong; the whole line is in the sheet beside. */}
        <HealthLine health={view?.health ?? member.health ?? null} compact={!wide} />
      </StaffHeader>
      <div className={`chat-body ${wide && side ? "with-staff-aside" : ""}`}>
        <div className="chat-main">
          {shown === "terminal" && terminalId ? (
            <div className="staff-term">
              <TerminalView id={terminalId} visible env={launch?.env ?? undefined} onState={(_, state) => setTerminalState(state)} />
            </div>
          ) : (
            <FeedView
              name={member.name}
              live={live}
              feed={feed}
              tailKey={outbox.map((m) => `${m.id}:${m.state}`).join(",")}
              tail={outbox.length > 0 ? <FeedOutbox staffId={staffId} name={member.name} rows={outbox} toast={toast} /> : null}
            />
          )}
          <div className="staff-foot">
            <KeyboardBanner terminalId={terminalId} messages={messages} watching={shown === "terminal"} state={terminalState} toast={toast} />
            <PermissionBar projectId={projectId} staffId={staffId} caps={caps} toast={toast} />
            <StaffComposer staffId={staffId} name={member.name} caps={caps} live={live} toast={toast} />
          </div>
        </div>
        {wide && side && (
          <aside className="staff-aside" aria-label={t("staff.side")}>
            {panel}
          </aside>
        )}
      </div>
      {!wide && sheet && (
        <Sheet title={member.name} onClose={() => setSheet(false)} className="staff-sheet">
          {panel}
        </Sheet>
      )}
    </div>
  );
}

/** The messages the Feed shows after its last turn: on their way, or failed with nothing taken since. */
function FeedOutbox({ staffId, name, rows, toast }: { staffId: string; name: string; rows: StaffMessage[]; toast: (text: string) => void }) {
  return (
    <div className="feed-outbox" aria-label={t("focus.staff.messages", { name })}>
      {rows.map((m) => <MessageRow key={m.id} staffId={staffId} name={name} message={m} toast={toast} />)}
    </div>
  );
}

/** A person typing in the terminal holds the orchestrator's messages back; this says so, and lets go. */
function KeyboardBanner({ terminalId, messages, watching, state, toast }: { terminalId: string | null; messages: StaffMessage[]; watching: boolean; state: TerminalState | null; toast: (text: string) => void }) {
  // While the terminal is on screen its own connection says who holds the keyboard; otherwise the
  // listing is asked, less often.
  const { data: row } = useQuery<TerminalRow>(terminalId && !watching ? `/api/terminals/${enc(terminalId)}` : null, { pollMs: 5000, staleMs: 2000 });
  const raw = watching ? state?.keyboard : row?.live?.keyboard;
  const until = raw?.until == null ? null : typeof raw.until === "number" ? raw.until : Date.parse(String(raw.until));
  const keyboard = raw ? { owner: String(raw.owner), until: Number.isFinite(until) ? until : null } : null;
  if (!terminalId || !keyboardBlocks(keyboard, messages)) return null;
  async function release() {
    try {
      await api.post(`/api/terminals/${enc(terminalId!)}/keyboard`, { owner: "auto" });
      invalidate(`/api/terminals/${enc(terminalId!)}`);
    } catch (e) {
      toast(errorText(e));
    }
  }
  return (
    <div className="staff-keyboard" role="status">
      <Icon name="lock" size={14} />
      <span className="grow">{t("staff.keyboard.held")}</span>
      <button className="btn small" onClick={() => void release()}>{t("staff.keyboard.release")}</button>
    </div>
  );
}

/** The member's open requests, each answered in place: Allow, Always where the CLI has it, No with a reason. */
function PermissionBar({ projectId, staffId, caps, toast }: { projectId: string; staffId: string; caps: HarnessCapabilities | null; toast: (text: string) => void }) {
  const open = useRequests(projectId, staffId);
  if (open.length === 0) return null;
  return (
    <div className="staff-requests" aria-label={t("perm.label")}>
      {open.map((ask) => (
        <section key={ask.id} className={`staff-request ${ask.kind}`} data-ask={ask.short_id}>
          <div className="staff-request-head">
            <Icon name={ask.kind === "permission" ? "shield" : "question"} size={14} />
            <span className="staff-request-text truncate" title={ask.text}>{ask.text}</span>
            <span className="staff-request-id mono">#{ask.short_id}</span>
          </div>
          {ask.routed_to === "orchestrator" && <div className="staff-request-routed">{t("perm.routed.orchestrator")}</div>}
          <AskAnswers ask={ask} projectId={projectId} toast={toast} always={canAlways(ask, caps)} />
        </section>
      ))}
    </div>
  );
}

/** "Write to Ira…", with the choice of when it goes in: after the turn, or now where the CLI allows it. */
function StaffComposer({ staffId, name, caps, live, toast }: { staffId: string; name: string; caps: HarnessCapabilities | null; live: boolean; toast: (text: string) => void }) {
  const [text, setText] = useState("");
  const [mode, setMode] = useState<"queue" | "steer">("queue");
  const [busy, setBusy] = useState(false);
  const now = nowChoice(caps);
  const chosen = now.enabled ? mode : "queue";
  async function send(e?: FormEvent) {
    e?.preventDefault();
    const words = text.trim();
    if (!words || busy) return;
    setBusy(true);
    try {
      await api.post(`/api/staff/${enc(staffId)}/messages`, { text: words, mode: chosen });
      setText("");
      toast(t("phone.told", { name }));
      invalidate(`/api/staff/${enc(staffId)}/messages`);
    } catch (err) {
      toast(errorText(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="staff-compose" onSubmit={(e) => void send(e)}>
      <textarea
        className="staff-compose-field"
        rows={1}
        value={text}
        maxLength={64000}
        placeholder={t("focus.composer.staff", { name })}
        aria-label={t("focus.composer.staff", { name })}
        disabled={!live}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing && window.matchMedia("(hover: hover)").matches) {
            e.preventDefault();
            void send();
          }
        }}
      />
      <div className="segmented inline staff-when" role="group" aria-label={t("staff.when")}>
        <button type="button" className={chosen === "queue" ? "on" : ""} aria-pressed={chosen === "queue"} data-when="queue" onClick={() => setMode("queue")}>{t("staff.when.queue")}</button>
        <button type="button" className={chosen === "steer" ? "on" : ""} aria-pressed={chosen === "steer"} data-when="steer" disabled={!now.enabled} title={now.hint ? t(now.hint, { cli: caps?.label ?? "" }) : undefined} onClick={() => setMode("steer")}>{t("staff.when.now")}</button>
      </div>
      <button className="iconbtn primary staff-compose-send" type="submit" disabled={busy || !text.trim() || !live} aria-label={t("staff.send")} title={t("staff.send")}>
        <Icon name="send" />
      </button>
    </form>
  );
}

type SideTab = "session" | "changes" | "notes";

type PanelProps = {
  projectId: string;
  staffId: string;
  name: string;
  view: StaffSessionView | null;
  notes: string;
  instructions: string;
  taskId: string | null;
  tab: SideTab;
  onTab: (tab: SideTab) => void;
  messages: StaffMessage[];
  /** Bumped by the header's marker: the Messages section is scrolled into view. */
  reveal: number;
  toast: (text: string) => void;
};

/** The column beside the member (a sheet on a phone): its session and messages, what it changed, its notes. */
function StaffPanel({ projectId, staffId, name, view, notes, instructions, taskId, tab, onTab: setTab, messages, reveal, toast }: PanelProps) {
  return (
    <div className="staff-panel">
      <div className="panel-tabs" role="tablist">
        {(["session", "changes", "notes"] as SideTab[]).map((name) => (
          <button key={name} role="tab" className={`panel-tab ${tab === name ? "on" : ""}`} aria-selected={tab === name} data-tab={name} onClick={() => setTab(name)}>
            <span>{t(`staff.tab.${name}`)}</span>
          </button>
        ))}
      </div>
      <div className="panel-body staff-panel-body">
        {tab === "session" && <SessionTab staffId={staffId} view={view} messages={<MessagesSection staffId={staffId} name={name} messages={messages} reveal={reveal} toast={toast} />} />}
        {tab === "changes" && <ChangesTab projectId={projectId} staffId={staffId} taskId={taskId} />}
        {tab === "notes" && (
          <div className="staff-notes">
            <div className="staff-aside-label">{t("staff.notes")}</div>
            <div className="staff-notes-text">{notes || t("staff.notes.none")}</div>
            {instructions && (
              <>
                <div className="staff-aside-label">{t("staff.instructions")}</div>
                <div className="staff-notes-text">{instructions}</div>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function eventLine(event: StaffEventRow): string {
  const p = event.payload;
  const str = (v: unknown) => (typeof v === "string" ? v : "");
  switch (event.type) {
    case "staff.status":
      return [t(`team.status.${str(p.status) || "working"}`), str(p.waiting_for)].filter(Boolean).join(" · ");
    case "staff.message":
      return t("staff.ev.message", { state: t(`focus.msg.state.${str(p.state) || "queued"}`) });
    case "staff.report":
      return [t("staff.ev.report", { kind: str(p.kind) }), str(p.text) || str(p.note)].filter(Boolean).join(" · ");
    case "staff.channel":
      return t(`staff.health.tools.${str(p.team_tools) || "waiting"}`);
    case "permission.pending":
    case "ask.pending":
      return [t("staff.ev.asked"), str(p.summary) || str(p.text)].filter(Boolean).join(" · ");
    case "permission.resolved":
    case "ask.answered":
      return t("staff.ev.answered", { who: str(p.by) || "—" });
    default:
      return event.type;
  }
}

/** Every message sent to the member, newest first, each with its receipt; Retry on the operator's failed ones. */
function MessagesSection({ staffId, name, messages, reveal, toast }: { staffId: string; name: string; messages: StaffMessage[]; reveal: number; toast: (text: string) => void }) {
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    if (reveal > 0) ref.current?.scrollIntoView({ block: "start" });
  }, [reveal]);
  const rows = listRows(messages);
  return (
    <section ref={ref} className="staff-aside-section staff-messages-section" data-section="messages">
      <div className="staff-aside-label">{t("staff.messages")}</div>
      {rows.length === 0 ? (
        <div className="staff-aside-line sub">{t("staff.messages.none")}</div>
      ) : (
        <div className="staff-message-list" aria-label={t("focus.staff.messages", { name })}>
          {rows.map((m) => <MessageRow key={m.id} staffId={staffId} name={name} message={m} toast={toast} />)}
        </div>
      )}
    </section>
  );
}

function SessionTab({ staffId, view, messages }: { staffId: string; view: StaffSessionView | null; messages: ReactNode }) {
  const key = `/api/staff/${enc(staffId)}/events?limit=40`;
  const { data } = useQuery<{ events: StaffEventRow[] }>(view?.session ? key : null, { pollMs: 15000, staleMs: 3000 });
  useEvent(["staff."], (event) => {
    if (event.staff_id === staffId) invalidate(`/api/staff/${enc(staffId)}/events`);
  }, [staffId]);
  const usage = view?.usage;
  const spend = usage
    ? [usage.input_tokens ? `${tokens(usage.input_tokens)}↑` : "", usage.output_tokens ? `${tokens(usage.output_tokens)}↓` : "", usage.cost_usd ? usd(usage.cost_usd) : "", usage.window_used_pct != null ? t("staff.usage.window", { pct: Math.round(usage.window_used_pct) }) : ""].filter(Boolean).join(" · ")
    : "";
  const events = Array.isArray(data?.events) ? data!.events : [];
  const channel = channelWords(view?.capabilities);
  return (
    <div className="staff-session">
      {view?.health && (
        <section className="staff-aside-section">
          <div className="staff-aside-label">{t("staff.channel")}</div>
          {channel && <div className="staff-aside-line">{channel}</div>}
          <HealthLine health={view.health} />
        </section>
      )}
      {messages}
      <section className="staff-aside-section">
        <div className="staff-aside-label">{t("staff.transcript")}</div>
        <div className="staff-aside-line mono truncate">{view?.session?.cli_session_id || "—"}</div>
        <div className="staff-aside-line sub">{t("staff.transcript.why")}</div>
      </section>
      <section className="staff-aside-section">
        <div className="staff-aside-label">{t("staff.usage")}</div>
        <div className="staff-aside-line">{spend || "—"}</div>
      </section>
      <section className="staff-aside-section">
        <div className="staff-aside-label">{t("staff.events")}</div>
        {events.length === 0 && <div className="staff-aside-line sub">—</div>}
        <ul className="staff-events">
          {events.slice(0, 30).map((event) => (
            <li key={event.seq} className="staff-event" data-type={event.type}>
              <span className="staff-event-at mono">{relTime(event.at)}</span>
              <span className="staff-event-text truncate">{eventLine(event)}</span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

function ChangesTab({ projectId, staffId, taskId }: { projectId: string; staffId: string; taskId: string | null }) {
  const { data, error } = useQuery<StaffChanges>(`/api/staff/${enc(staffId)}/changes`, { pollMs: 30000, staleMs: 5000 });
  const files = Array.isArray(data?.files) ? data!.files : [];
  const summary = useMemo(() => (data ? t("staff.changes.summary", { added: data.added, removed: data.removed, files: plural("staff.changes.files", files.length) }) : ""), [data, files.length]);
  if (error && !data) return <div className="staff-aside-line sub">{error}</div>;
  if (!data) return <div className="staff-aside-line sub">{t("common.loading")}</div>;
  return (
    <div className="staff-changes">
      <div className="staff-changes-head">
        <span className="staff-changes-summary">{data.detail && files.length === 0 ? t("staff.changes.none") : summary}</span>
        {taskId && <button className="btn small" onClick={() => navigate(projectPagePath(projectId, "board", { task: taskId }))}>{t("staff.changes.diff")}</button>}
      </div>
      <ul className="staff-files">
        {files.map((f) => (
          <li key={f.path} className="staff-file">
            <span className="staff-file-path mono truncate">{f.path}</span>
            <span className="staff-file-added">{f.added == null ? "" : `+${f.added}`}</span>
            <span className="staff-file-removed">{f.removed == null ? "" : `−${f.removed}`}</span>
          </li>
        ))}
        {(data.untracked ?? []).map((path) => (
          <li key={`u-${path}`} className="staff-file untracked">
            <span className="staff-file-path mono truncate">{path}</span>
            <span className="staff-file-added">{t("staff.changes.new")}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
