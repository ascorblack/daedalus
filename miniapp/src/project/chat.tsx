// The orchestrator's chat, where it differs from any other: the project's events it was woken with are
// cards, not a system note to unfold; what it did to the project is a line per step, in the open,
// because that is what the operator reads the chat for; and a question it put to the operator is a
// card answered right there — one of its options, or words of the operator's own.

import { createContext, useContext, useState } from "react";
import { api, ApiError, type Ask } from "../api";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { invalidate } from "../store";
import type { ToolItem } from "../turns";
import { parseEvents } from "../turns";
import { errorText } from "../ui";
import { askIdOf, isOrchestratorStep, stepDetail, stepKey } from "./focus";

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

/** The orchestrator's steps in a turn, one line each, with a card under each question it asked. */
export function StepLines({ items }: { items: ToolItem[] }) {
  const focus = useFocusChat();
  const steps = items.filter((item) => isOrchestratorStep(item.name));
  if (!focus || steps.length === 0) return null;
  return (
    <div className="step-lines">
      {steps.map((item) => {
        const short = askIdOf(item.result);
        const ask = short ? focus.asks.get(short) : undefined;
        return (
          <div key={item.id} className="step-wrap">
            <div className={`step-line ${item.error ? "failed" : ""} ${item.running ? "running" : ""}`}>
              <span className="step-verb">{t(`focus.step.${stepKey(item.name, item.args)}`)}</span>
              {stepDetail(item.name, item.args) && <span className="step-detail mono truncate">{stepDetail(item.name, item.args)}</span>}
              {item.error && !item.running && <span className="step-failed">{t("focus.step.failed")}</span>}
            </div>
            {ask && ask.origin === "orchestrator" && <AskCard ask={ask} />}
          </div>
        );
      })}
    </div>
  );
}

/** Who answered, in words. */
function answeredBy(ask: Ask): string {
  const r = ask.resolution ?? {};
  const text = r.text || (r.selected ?? []).join(", ") || (r.allow === true ? t("focus.ask.yes") : r.allow === false ? t("focus.ask.no") : "");
  const who = ask.resolved_by === "operator" || ask.resolved_by === "orchestrator" || ask.resolved_by === "system" ? ask.resolved_by : "system";
  return who === "operator" ? t("focus.ask.answered", { text }) : t("focus.ask.answered.by", { who: t(`focus.ask.who.${who}`), text });
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
      await api.post(`/api/asks/${encodeURIComponent(shown.id)}/answer`, body);
      setLocal({ ...shown, resolved_at: new Date().toISOString(), resolved_by: "operator", resolution: { ...body, text: body.text ?? label } });
      focus?.toast(t("focus.ask.sent"));
    } catch (e) {
      focus?.toast(e instanceof ApiError && e.status === 409 ? t("focus.ask.conflict") : errorText(e));
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
          {shown.kind === "folder" && options.length === 0 ? (
            <div className="ask-options">
              <button className="btn small primary" disabled={busy} onClick={() => void send({ allow: true }, t("focus.ask.yes"))}>{t("focus.ask.yes")}</button>
              <button className="btn small" disabled={busy} onClick={() => void send({ allow: false }, t("focus.ask.no"))}>{t("focus.ask.no")}</button>
            </div>
          ) : (
            <form className="ask-own" onSubmit={(e) => { e.preventDefault(); if (text.trim()) void send({ text: text.trim() }, text.trim()); }}>
              <input className="field" value={text} maxLength={4000} placeholder={t("focus.ask.placeholder")} aria-label={t("focus.ask.placeholder")} onChange={(e) => setText(e.target.value)} />
              <button className="btn small primary" type="submit" disabled={busy || !text.trim()}>{t("focus.ask.send")}</button>
            </form>
          )}
        </>
      ) : (
        <div className="ask-answer">{answeredBy(shown)}</div>
      )}
    </div>
  );
}
