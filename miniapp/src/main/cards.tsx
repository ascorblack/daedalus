// The main chat's own parts: the questions the projects put to the operator, as cards answered where
// they are shown, and the dispatches under way as cards that say where each piece of work stands.
// Both sit in the conversation's flow, after its latest turn.
//
// A card is the same request row the project's chat and "Needs you" show; the first answer to reach
// the host wins. The project's chat then shows the one line of who answered where, and here the card
// leaves the flow. A request that acts
// on the host is answered here, where the whole of it is shown — never from Telegram or a lock screen.

import { useState } from "react";
import { api, ApiError, type Dispatch, type DispatchMessage, type MainAsk, type MainView } from "../api";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { projectHome } from "../router";
import { go } from "../shell";
import { invalidate, useQuery } from "../store";
import { relTime } from "../format";
import { confirmAsync, errorText } from "../ui";
import { MAIN_KEY } from "./data";
import { dispatchState, goingDispatches, groupAsks, takesWords } from "./model";

type Toast = (text: string) => void;

/**
 * What waits in the main chat, in the conversation's own flow after its latest turn: the questions
 * still open, the projects being set up, and the work under way. Nothing here is a strip above the
 * chat any more: the operator found the strip of answered and withdrawn lines and "N finished
 * dispatches" useless, and on a phone it took the screen. What is resolved leaves the flow — the
 * reports that closed a dispatch are already lines of the chat, and an answered card is gone.
 */
export function MainFlow({ view, toast }: { view: MainView; toast: Toast }) {
  const groups = groupAsks(view.asks);
  const going = goingDispatches(view.dispatches);
  if (!groups.length && !going.length && !view.setup.length) return null;
  return (
    <section className="main-flow" aria-label={t("main.board.label")}>
      {groups.map((group) => (
        <div key={group.key} className="main-ask-group" aria-label={plural("main.questions", group.asks.length)}>
          <div className="main-group-head">
            <Icon name="question" size={13} />
            <span className="truncate">{group.name}</span>
          </div>
          {group.asks.map((ask) => <MainAskCard key={ask.id} ask={ask} toast={toast} />)}
        </div>
      ))}
      {view.setup.map((p) => <SetupLine key={p.project_id} projectId={p.project_id} name={p.name} toast={toast} />)}
      {going.length > 0 && (
        <div className="main-dispatches">
          {going.map((d) => <DispatchCard key={d.id} dispatch={d} toast={toast} />)}
        </div>
      )}
    </section>
  );
}

/** A project that the main orchestrator is setting up, and the button that ends the setup by hand. */
export function SetupLine({ projectId, name, toast }: { projectId: string; name: string; toast: Toast }) {
  const [busy, setBusy] = useState(false);
  async function finish() {
    if (busy) return;
    setBusy(true);
    try {
      await api.post(`/api/projects/${encodeURIComponent(projectId)}/setup/finish`, {});
      toast(t("main.setup.finished", { name }));
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
      invalidate(MAIN_KEY);
      invalidate("/api/projects");
    }
  }
  return (
    <div className="main-setup" data-setup={projectId}>
      <Icon name="conductor" size={14} />
      <span className="grow">{t("main.setup.line", { name })}</span>
      <button className="btn small" disabled={busy} onClick={() => void finish()}>{t("main.setup.finish")}</button>
    </div>
  );
}

/**
 * One request, answered here: its options as buttons, Allow and Deny for a permission, and — for a
 * question — a field for words of the operator's own. A loss to an answer given elsewhere says who
 * was first; the listing read again then shows that answer's line.
 */
export function MainAskCard({ ask, toast }: { ask: MainAsk; toast: Toast }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const options = Array.isArray(ask.detail?.options) ? (ask.detail.options as string[]) : [];
  async function send(body: { selected?: string[]; text?: string; allow?: boolean }) {
    if (busy) return;
    setBusy(true);
    try {
      await api.post(`/api/asks/${encodeURIComponent(ask.id)}/answer`, { ...body, window: "main" });
      toast(t("main.ask.sent"));
      setText("");
    } catch (e) {
      toast(e instanceof ApiError && e.status === 409 ? t("main.ask.conflict") : errorText(e));
    } finally {
      setBusy(false);
      invalidate(MAIN_KEY);
      if (ask.project_id) invalidate(`/api/asks?project=${encodeURIComponent(ask.project_id)}`);
    }
  }
  const head = ask.origin === "dispatcher" ? t("main.ask.confirm") : ask.kind === "permission" ? t("main.ask.permission", { who: ask.asker }) : ask.kind === "folder" ? t("main.ask.folder") : ask.asker === "orchestrator" ? t("main.ask.orchestrator") : t("main.ask.staff", { who: ask.asker });
  const yesNo = ask.kind === "permission" || (ask.kind === "folder" && options.length === 0);
  return (
    <div className={`ask-card main-ask open ${ask.host ? "host" : ""}`} data-ask={ask.short_id}>
      <div className="ask-head">
        <Icon name={ask.host ? "lock" : "question"} size={14} />
        <span className="grow">{head}</span>
        <code className="ask-short">{ask.short_id}</code>
      </div>
      <div className="ask-text">{ask.text}</div>
      {ask.suggestion && <div className="ask-suggestion">{t("main.ask.suggests", { text: ask.suggestion })}</div>}
      {ask.host && <div className="ask-host">{t("main.ask.host")}</div>}
      {yesNo ? (
        <div className="ask-options">
          <button className="btn small primary" disabled={busy} onClick={() => void send({ allow: true })}>{t("main.ask.allow")}</button>
          <button className="btn small" disabled={busy} onClick={() => void send({ allow: false })}>{t("main.ask.deny")}</button>
        </div>
      ) : options.length > 0 && (
        <div className="ask-options">
          {options.map((option, i) => (
            <button key={option} className={`btn small ${ask.kind === "project" && i === 0 ? "primary" : ""}`} disabled={busy} onClick={() => void send({ selected: [option] })}>{option}</button>
          ))}
        </div>
      )}
      {takesWords(ask) && (
        <form className="ask-own" onSubmit={(e) => { e.preventDefault(); if (text.trim()) void send({ text: text.trim() }); }}>
          <input className="field" value={text} maxLength={4000} placeholder={t("focus.ask.placeholder")} aria-label={t("focus.ask.placeholder")} onChange={(e) => setText(e.target.value)} />
          <button className="btn small primary" type="submit" disabled={busy || !text.trim()}>{t("focus.ask.send")}</button>
        </form>
      )}
    </div>
  );
}

/** One piece of work handed to a project: whose, what, where it stands, and what the project last said. */
export function DispatchCard({ dispatch, toast }: { dispatch: Dispatch; toast: Toast }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const state = dispatchState(dispatch);
  const going = dispatch.status === "open" || dispatch.status === "blocked";
  const said = dispatch.last;
  async function cancel() {
    if (busy || !(await confirmAsync(t("main.dispatch.cancel.confirm", { title: dispatch.title || `#${dispatch.seq}` }), { action: t("main.dispatch.cancel"), danger: true }))) return;
    setBusy(true);
    try {
      await api.post(`/api/dispatches/${encodeURIComponent(dispatch.id)}/cancel`, {});
      toast(t("main.dispatch.cancelled.toast"));
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
      invalidate(MAIN_KEY);
    }
  }
  const home = projectHome(dispatch.project_id);
  return (
    <article className={`dispatch-card tone-${state.tone} ${going ? "going" : "over"}`} data-dispatch={dispatch.id}>
      <div className="dispatch-head">
        <a className="dispatch-project truncate" href={home} onClick={(e) => go(e, home)}>{dispatch.project_name || dispatch.project_id}</a>
        <span className="dispatch-seq num">#{dispatch.seq}</span>
        <span className={`dispatch-state tone-${state.tone}`}>{state.word}</span>
        <span className="dispatch-age faint">{relTime(dispatch.closed_at ?? dispatch.created_at)}</span>
      </div>
      <button className="dispatch-body" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <span className="dispatch-title">{dispatch.title || dispatch.text.split("\n")[0]}</span>
        {said && <span className="dispatch-last truncate">{lastLine(said)}</span>}
      </button>
      {open && <DispatchDetail dispatch={dispatch} />}
      {going && (
        <div className="dispatch-actions">
          <button className="btn small ghost" disabled={busy} onClick={() => void cancel()}>{t("main.dispatch.cancel")}</button>
        </div>
      )}
    </article>
  );
}

function lastLine(message: DispatchMessage): string {
  return `${t(`main.author.${message.author}`)} · ${t(`main.kind.${["progress", "done", "blocked", "decision", "cancelled", "message"].includes(message.kind) ? message.kind : "message"}`)}: ${message.text.split("\n")[0]}`;
}

function DispatchDetail({ dispatch }: { dispatch: Dispatch }) {
  const { data } = useQuery<Dispatch & { messages: DispatchMessage[] }>(`/api/dispatches/${encodeURIComponent(dispatch.id)}`, { staleMs: 2000 });
  return (
    <div className="dispatch-detail">
      <div className="dispatch-asked">{dispatch.text}</div>
      {(data?.messages ?? []).map((m) => (
        <div key={m.id} className={`dispatch-message author-${m.author}`}>
          <span className="dispatch-message-head">{t(`main.author.${m.author}`)} · {relTime(m.at)}</span>
          <span className="dispatch-message-text">{m.text}</span>
        </div>
      ))}
      {!data && <div className="faint">{t("common.loading")}</div>}
    </div>
  );
}
