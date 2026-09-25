// The orchestrator's chat, where it differs from any other: the project's events it was woken with are
// cards, not a system note to unfold; and what it did to the project is named in the project's words
// ("Task created · Photos"), inside the turn's worked group like any other agent's steps. The
// questions it put to the operator are not drawn here: they wait in the panel's Questions tab, and
// the chat carries one line that opens it.

import { createContext, useContext } from "react";
import { plural, t } from "../i18n";
import { Icon, type IconName } from "../icons";
import type { ToolItem } from "../turns";
import { parseEvents } from "../turns";
import { isOrchestratorStep, stepDetail, stepKey } from "./focus";

/** What a turn in focus mode needs beyond its own session: the project, and whether this is its orchestrator. */
export type FocusChat = { projectId: string; orchestrator: boolean; toast: (text: string) => void };

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
  Peek: "eye", WakeMe: "clock", Watch: "eye", Unwatch: "eye", AskOperator: "question", WithdrawQuestions: "undo", ProjectReport: "journal",
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
