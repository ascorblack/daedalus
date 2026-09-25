// What the main chat draws, decided without React: which cards are open and in what order, how an
// answered request reads in one line, and what a dispatch's state is called. Kept pure so the rules
// the operator relies on — one card per request, grouped by project, oldest first; "answered in
// <where>" everywhere a request was shown — are tested without a browser.

import type { Ask, Dispatch, MainAsk, Preset } from "../api";
import { t } from "../i18n";

/** The main chat is orchestration mode's home. */
export const MAIN_PATH = "/app/orchestration";

/** The requests still waiting, one group per project, the groups and the cards oldest first. */
export function groupAsks(asks: MainAsk[]): { key: string; name: string; asks: MainAsk[] }[] {
  const groups = new Map<string, { key: string; name: string; asks: MainAsk[] }>();
  const open = asks.filter((a) => !a.resolved_at).sort((a, b) => a.created_at.localeCompare(b.created_at));
  for (const ask of open) {
    const key = ask.project_id ?? `new:${ask.id}`;
    const group = groups.get(key) ?? { key, name: ask.project_name || t("main.ask.newproject"), asks: [] };
    group.asks.push(ask);
    groups.set(key, group);
  }
  return [...groups.values()];
}

/** Where a request was answered, in the reader's words. */
export function answeredWhere(via: string | undefined): string {
  const known = ["main", "project", "telegram", "notification", "push", "orchestrator", "dispatcher", "app"];
  return t(`main.via.${via && known.includes(via) ? (via === "push" ? "notification" : via) : "app"}`);
}

/** The answer itself, as the one line a card collapses to: "answered in the main chat: Postgres". */
export function answeredLine(ask: Ask): string {
  const r = ask.resolution ?? {};
  if (ask.resolved_by === "system") return t("main.ask.withdrawn");
  const answer =
    r.allow === true && (ask.kind === "permission" || ask.kind === "folder") ? t("focus.ask.yes")
    : r.allow === false && (ask.kind === "permission" || ask.kind === "folder") ? t("focus.ask.no")
    : (r.selected ?? []).join(", ") || r.text || t("main.ask.answered.plain");
  const by = ask.resolved_by === "orchestrator" ? t("main.ask.by.orchestrator") : "";
  return t("main.ask.answered", { where: answeredWhere(r.via), answer }) + by;
}

/** A dispatch's state as a word and a tone: stalled is shown on an open one that went quiet. */
export function dispatchState(d: Dispatch): { word: string; tone: "ok" | "warn" | "bad" | "info" | "faint" } {
  if (d.status === "open" && d.stalled_at) return { word: t("main.dispatch.stalled"), tone: "warn" };
  if (d.status === "open") return { word: t("main.dispatch.open"), tone: "info" };
  if (d.status === "done") return { word: t("main.dispatch.done"), tone: "ok" };
  if (d.status === "blocked") return { word: t("main.dispatch.blocked"), tone: "bad" };
  return { word: t("main.dispatch.cancelled"), tone: "faint" };
}

/** The dispatches drawn as cards: the open and blocked ones, the one waiting longest first. A closed
 *  dispatch is not a card: the report that closed it is already a line in the chat's own flow. */
export function goingDispatches(list: Dispatch[]): Dispatch[] {
  return list.filter((d) => d.status === "open" || d.status === "blocked").sort((a, b) => a.created_at.localeCompare(b.created_at));
}

/** Whether a request is answered in words as well as with its buttons. */
export function takesWords(ask: Ask): boolean {
  return ask.kind === "question";
}

/**
 * The preset the main orchestrator runs unless Settings names one: the chosen one, or, while none
 * is (or the chosen one was removed), the host's mid-tier pick. The select shows what runs.
 */
export function mainPreset(presets: Record<string, Preset>, chosen: string | undefined, middle: string | undefined): string {
  if (chosen && chosen in presets) return chosen;
  if (middle && middle in presets) return middle;
  return Object.keys(presets)[0] ?? "";
}
