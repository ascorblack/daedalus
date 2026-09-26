// A project's watches decided without a browser: a watch as a sentence in the reader's language, and
// the form's draft as the request the host takes — only the fields that kind of watch has, and null
// until the required ones are there.

import type { MessageTiming, WatchThen, WatchWhen } from "../api";
import { t } from "../i18n";

export const WATCH_KINDS = ["staff_finished", "staff_question", "staff_permission", "staff_crashed", "staff_silent", "task_moved", "terminal_output", "git_commit", "pr", "ci", "webhook"] as const;
export type WatchKind = (typeof WATCH_KINDS)[number];
export const WATCH_ACTIONS = ["wake", "notify", "tell"] as const;
export type WatchAction = (typeof WATCH_ACTIONS)[number];
export const TASK_STATUSES = ["todo", "doing", "review", "done", "blocked", "dropped"] as const;
export const NOTIFY_LEVELS = ["quiet", "normal", "urgent"] as const;
/** When a watch's message goes in, as Tell names it: into the running turn, after it, or stopping it. */
export const TELL_TIMINGS = ["now", "after_turn", "interrupt"] as const;

/** Every field the form has; each kind reads the ones it needs. */
export type WatchDraft = {
  kind: WatchKind;
  staff: string;
  minutes: string;
  task: string;
  to: string;
  terminal: string;
  regex: string;
  folder: string;
  branch: string;
  provider: string;
  repo: string;
  conclusion: string;
  action: WatchAction;
  wakeNote: string;
  tellStaff: string;
  tellText: string;
  tellWhen: MessageTiming;
  title: string;
  text: string;
  level: string;
  cooldown: string;
  once: boolean;
  note: string;
};

export function emptyWatch(): WatchDraft {
  return {
    kind: "staff_finished", staff: "", minutes: "15", task: "", to: "", terminal: "", regex: "", folder: "", branch: "", provider: "", repo: "", conclusion: "",
    action: "wake", wakeNote: "", tellStaff: "", tellText: "", tellWhen: "now", title: "", text: "", level: "normal", cooldown: "10", once: false, note: "",
  };
}

export type WatchBody = { when: WatchWhen; then: WatchThen; cooldown_minutes: number; once: boolean; note: string };

const STAFF_KINDS: WatchKind[] = ["staff_finished", "staff_question", "staff_permission", "staff_crashed", "staff_silent"];

/** The request for a draft, or null while a required field is missing. */
export function watchBody(d: WatchDraft): WatchBody | null {
  const when: WatchWhen = { event: d.kind };
  const put = (key: keyof WatchWhen, value: string) => {
    if (value.trim()) (when as Record<string, unknown>)[key] = value.trim();
  };
  if (STAFF_KINDS.includes(d.kind)) {
    put("staff", d.staff);
    if (d.kind === "staff_silent") {
      const minutes = Number(d.minutes);
      if (!d.staff.trim() || !Number.isInteger(minutes) || minutes < 5) return null;
      when.minutes = minutes;
    }
  } else if (d.kind === "task_moved") {
    put("task", d.task);
    put("to", d.to);
  } else if (d.kind === "terminal_output") {
    if (!d.regex.trim() || !d.terminal.trim()) return null;
    // The picker lists terminals and command-line staff in one list; a member is marked so.
    if (d.terminal.startsWith("staff:")) when.staff = d.terminal.slice(6);
    else when.terminal = d.terminal;
    put("regex", d.regex);
  } else if (d.kind === "git_commit") {
    put("folder", d.folder);
    put("branch", d.branch);
  } else {
    if (!d.provider.trim()) return null;
    put("provider", d.provider);
    if (d.kind === "webhook") put("regex", d.regex);
    else {
      put("repo", d.repo);
      put("conclusion", d.conclusion);
    }
  }
  let then: WatchThen;
  if (d.action === "wake") then = d.wakeNote.trim() ? { action: "wake", note: d.wakeNote.trim() } : { action: "wake" };
  else if (d.action === "tell") {
    if (!d.tellStaff.trim() || !d.tellText.trim()) return null;
    then = { action: "tell", staff: d.tellStaff.trim(), text: d.tellText.trim(), when: d.tellWhen };
  } else {
    if (!d.title.trim()) return null;
    then = { action: "notify", title: d.title.trim(), text: d.text.trim(), level: d.level };
  }
  const cooldown = Number(d.cooldown);
  if (!Number.isFinite(cooldown) || cooldown < 1) return null;
  return { when, then, cooldown_minutes: cooldown, once: d.once, note: d.note.trim() };
}

/** "When Max finishes a turn", in the reader's language, starting with a capital whatever the member is called. */
export function watchWhenText(w: WatchWhen): string {
  const text = whenText(w);
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function whenText(w: WatchWhen): string {
  const who = w.staff || t("focus.watch.anyone");
  switch (w.event) {
    case "staff_finished":
    case "staff_question":
    case "staff_permission":
    case "staff_crashed":
      return t(`focus.watch.when.${w.event}`, { who });
    case "staff_silent":
      return t("focus.watch.when.staff_silent", { who, n: w.minutes ?? 0 });
    case "task_moved":
      return t(w.to ? "focus.watch.when.task_moved.to" : "focus.watch.when.task_moved", { task: w.task || t("focus.watch.anytask"), to: w.to ? t(`board.col.${w.to}`) : "" });
    case "terminal_output":
      return t("focus.watch.when.terminal_output", { where: w.terminal_title || w.staff || w.terminal || "", regex: w.regex ?? "" });
    case "git_commit":
      return t(w.branch ? "focus.watch.when.git_commit.branch" : "focus.watch.when.git_commit", { folder: w.folder_label || w.folder || "", branch: w.branch ?? "" });
    case "pr":
      return t("focus.watch.when.pr", { provider: w.provider ?? "", repo: w.repo ? ` ${w.repo}` : "", conclusion: w.conclusion ? ` · ${w.conclusion}` : "" });
    case "ci":
      return t("focus.watch.when.ci", { provider: w.provider ?? "", repo: w.repo ? ` ${w.repo}` : "", conclusion: w.conclusion ? ` · ${w.conclusion}` : "" });
    case "webhook":
      return t(w.regex ? "focus.watch.when.webhook.regex" : "focus.watch.when.webhook", { provider: w.provider ?? "", regex: w.regex ?? "" });
    default:
      return w.event;
  }
}

/** "wake the orchestrator", "tell Max: …", "notify you: …". */
export function watchThenText(a: WatchThen): string {
  if (a.action === "tell") return t("focus.watch.then.tell", { who: a.staff ?? "", text: a.text ?? "" });
  if (a.action === "notify") return t("focus.watch.then.notify", { title: a.title ?? "" });
  return t("focus.watch.then.wake");
}
