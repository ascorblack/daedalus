// The orchestrator's chat, where it differs from any other: the project's events it was woken with are
// cards, not a system note to unfold; what it did to the project is named in the project's words
// ("Task created · Photos"), inside the turn's worked group like any other agent's steps; and a
// question it put to the operator is a card answered right there, outside that group — one of its
// options, or words of the operator's own.

import { createContext, useContext, useState } from "react";
import { api, ApiError, type Ask } from "../api";
import { plural, t } from "../i18n";
import { Icon, type IconName } from "../icons";
import { invalidate } from "../store";
import type { ToolItem } from "../turns";
import { parseEvents } from "../turns";
import { errorText } from "../ui";
import { isOrchestratorStep, requestsOf, stepDetail, stepKey } from "./focus";
import { answeredLine } from "../main/model";

/** What a turn in focus mode needs beyond its own session: the project, and its requests by short id. */
export type FocusChat = { projectId: string; orchestrator: boolean; asks: Map<string, Ask>; toast: (text: string) => void };

export const FocusChatContext = createContext<FocusChat | null>(null);

export function useFocusChat(): FocusChat | null {
  return useContext(FocusChatContext);
}

/** A batch of the project's events, one line each, coloured by what the line says happened. */
export function EventCard({ text }: { text: string }) {
  const batch = parseEvents(text);
  if (!batch) return null;
  return (
    <section className="event-card" aria-label={t("focus.events.label")}>
      <div className="event-head">
        <Icon name="bolt" size={14} />
        <span>{plural("focus.events.head", batch.count, { time: batch.since })}</span>
      </div>
      <ul className="event-lines">
        {batch.lines.map((line, i) => (
          <li key={i} className={`event-line ${line.tone}`}>
            <span className="event-time num">{line.time}</span>
            <span className="event-text">{line.text}</span>
          </li>
        ))}
        {batch.more > 0 && <li className="event-line info more">{t("focus.events.more", { n: batch.more })}</li>}
      </ul>
    </section>
  );
}

const STEP_ICONS: Record<string, IconName> = {
  Folders: "folder", Tasks: "board", Journal: "journal", Brief: "pen", Team: "bots", Hire: "bots", StaffEdit: "bots", Dismiss: "bots",
  Assign: "forward", Tell: "forward", ReadStaff: "file", Answer: "check", Interrupt: "stop", Pause: "stop", Release: "stop",
  Peek: "eye", WakeMe: "clock", Watch: "eye", Unwatch: "eye", AskOperator: "question", ProjectReport: "journal",
};

/**
 * How one of the orchestrator's own tool calls reads in its turn's worked group: the step in the
 * project's words and what it acted on. It replaces the tool's name and arguments there rather than
 * adding a second line elsewhere: the steps were once drawn both inside the group (as raw calls) and
 * again under it (as these lines), and the operator read every action twice.
 */
export function stepDescription(item: ToolItem): { verb: string; detail: string; icon: IconName } | null {
  if (!isOrchestratorStep(item.name)) return null;
  return { verb: t(`focus.step.${stepKey(item.name, item.args)}`), detail: stepDetail(item.name, item.args), icon: STEP_ICONS[item.name] ?? "conductor" };
}

/** The requests the orchestrator put to the operator in a turn — questions and folders alike — each a
 *  card to answer. They stay outside the folded worked group: a question behind a fold is a question
 *  nobody answers. */
export function AskCards({ items }: { items: ToolItem[] }) {
  const focus = useFocusChat();
  if (!focus) return null;
  const asks = requestsOf(items, focus.asks);
  if (asks.length === 0) return null;
  return (
    <div className="step-asks">
      {asks.map((ask) => <AskCard key={ask.id} ask={ask} />)}
    </div>
  );
}

/** Who answered, and where: the same line in every window that showed the request. */
function answeredBy(ask: Ask): string {
  return answeredLine(ask);
}

/**
 * A request of the orchestrator's own, answered where it was asked: its options as buttons, and a
 * field for an answer of the operator's own. The first answer to reach the host wins; one that loses
 * to an answer given elsewhere (the phone, Telegram) is told so, and the card shows the winner.
 */
export function AskCard({ ask }: { ask: Ask }) {
  const focus = useFocusChat();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [local, setLocal] = useState<Ask | null>(null);
  const shown = local ?? ask;
  const open = !shown.resolved_at;
  const options = Array.isArray(shown.detail?.options) ? shown.detail.options : [];
  async function send(body: { selected?: string[]; text?: string; allow?: boolean }, label: string) {
    if (busy) return;
    setBusy(true);
    try {
      const result = await api.post<{ delivered?: boolean; error?: string } | null>(`/api/asks/${encodeURIComponent(shown.id)}/answer`, { ...body, window: "project" });
      // An approval can be taken and still fail to be carried out (a folder the host refuses): the
      // reason is said here, where the operator pressed, and kept on the card.
      const failed = result && result.delivered === false && result.error ? result.error : "";
      setLocal({ ...shown, resolved_at: new Date().toISOString(), resolved_by: "operator", resolution: { ...body, text: body.text ?? label, via: "project", ...(failed ? { error: failed } : {}) } });
      focus?.toast(failed ? t("focus.ask.failed", { reason: failed }) : t("focus.ask.sent"));
    } catch (e) {
      focus?.toast(e instanceof ApiError && e.status === 409 ? t("focus.ask.conflict") : errorText(e));
      if (e instanceof ApiError && e.status === 409) invalidate("/api/main");
    } finally {
      setBusy(false);
      if (focus) invalidate(`/api/asks?project=${encodeURIComponent(focus.projectId)}`);
    }
  }
  return (
    <div className={`ask-card ${open ? "open" : "answered"}`} data-ask={shown.short_id}>
      <div className="ask-head">
        <Icon name="question" size={14} />
        <span className="grow">{t(shown.kind === "folder" ? "focus.ask.folder" : "focus.ask.title")}</span>
        <code className="ask-short">{shown.short_id}</code>
      </div>
      <div className="ask-text">{shown.text}</div>
      {open ? (
        <>
          {options.length > 0 && (
            <div className="ask-options">
              {options.map((option) => (
                <button key={option} className="btn small" disabled={busy} onClick={() => void send({ selected: [option] }, option)}>{option}</button>
              ))}
            </div>
          )}
          {shown.kind === "folder" ? (
            // A folder is added or not: words of the operator's own would be taken as neither, and
            // the request would close declined without anyone meaning it.
            options.length === 0 && (
              <div className="ask-options">
                <button className="btn small primary" disabled={busy} onClick={() => void send({ allow: true }, t("focus.ask.yes"))}>{t("focus.ask.yes")}</button>
                <button className="btn small" disabled={busy} onClick={() => void send({ allow: false }, t("focus.ask.no"))}>{t("focus.ask.no")}</button>
              </div>
            )
          ) : (
            <form className="ask-own" onSubmit={(e) => { e.preventDefault(); if (text.trim()) void send({ text: text.trim() }, text.trim()); }}>
              <input className="field" value={text} maxLength={4000} placeholder={t("focus.ask.placeholder")} aria-label={t("focus.ask.placeholder")} onChange={(e) => setText(e.target.value)} />
              <button className="btn small primary" type="submit" disabled={busy || !text.trim()}>{t("focus.ask.send")}</button>
            </form>
          )}
        </>
      ) : (
        <>
          <div className="ask-answer">{answeredBy(shown)}</div>
          {shown.resolution?.error && <div className="ask-failed">{t("focus.ask.notdone", { reason: shown.resolution.error })}</div>}
        </>
      )}
    </div>
  );
}
