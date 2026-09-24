// A project's board as the app draws it: the shapes the host sends, and the decisions the board makes
// from them — which column a task stands in, what order a column is read in, which chips a phone gets,
// what a card's status line says. Pure, so every one is tested without a browser.

import type { Harness, StaffStatus } from "../team/team";

export type TaskStatus = "todo" | "doing" | "review" | "done" | "blocked" | "dropped";

/** The board's columns, left to right: what waits on the operator, then the work in the order it flows. */
export const COLUMNS = ["needs", "doing", "review", "queue", "done"] as const;
export type Column = (typeof COLUMNS)[number];

export const BRIEF_FIELDS = ["objective", "deliverable", "boundaries", "done_when"] as const;
export type BriefField = (typeof BRIEF_FIELDS)[number];
export type Brief = Record<BriefField, string>;

export type Assignee = {
  id: string;
  name: string;
  color: string;
  harness: Harness;
  archived_at: string | null;
  /** The member's live status, or "off" without a live session. */
  status: StaffStatus;
  /** Whether that live session works on this task; otherwise the status is about something else. */
  on_task: boolean;
  waiting_for: string;
  status_at: string | null;
  session_id: string | null;
};

export type ProjectTask = {
  id: string;
  title: string;
  status: TaskStatus;
  priority: number;
  acceptance: string;
  checklist: { text: string; done: boolean }[];
  depends_on: string[];
  session_id: string | null;
  notes: string;
  created_at: string;
  updated_at: string;
  project_id: string | null;
  assignee_staff_id: string | null;
  brief: Brief;
  branch: string | null;
  merge_state: "" | "proposed" | "merged" | "conflict" | "rejected";
  assignee: Assignee | null;
};

export type NeedsYou = {
  id: string;
  short_id: string;
  origin: "staff" | "orchestrator";
  kind: "question" | "permission" | "folder";
  text: string;
  suggestion: string;
  created_at: string;
  task_id: string | null;
  task_title: string | null;
  staff: { id: string; name: string; color: string; harness: Harness } | null;
  /** Where the request can be answered: the staff member's session or the orchestrator's. */
  session_id: string | null;
};

export type TeamMember = { id: string; name: string; color: string; harness: Harness };

export type ProjectBoardData = {
  tasks: ProjectTask[];
  needs_you: NeedsYou[];
  counts: Record<TaskStatus | "needs_you", number>;
  staff: TeamMember[];
};

export type Arranged = { needs: NeedsYou[]; doing: ProjectTask[]; review: ProjectTask[]; queue: ProjectTask[]; done: ProjectTask[] };

/** The column a task stands in. A blocked task is still queued work: it waits for another one, not for a person. */
export function columnOf(status: TaskStatus): Exclude<Column, "needs"> {
  if (status === "doing") return "doing";
  if (status === "review") return "review";
  if (status === "done" || status === "dropped") return "done";
  return "queue";
}

const time = (iso: string) => Date.parse(iso) || 0;

/** The tasks by column, each column in the order it is read: the most urgent first, and in the queue
 *  what can start now before what waits for another task. */
export function arrange(data: Pick<ProjectBoardData, "tasks" | "needs_you">): Arranged {
  const out: Arranged = { needs: [...data.needs_you].sort((a, b) => time(a.created_at) - time(b.created_at)), doing: [], review: [], queue: [], done: [] };
  for (const task of data.tasks) out[columnOf(task.status)].push(task);
  const urgent = (a: ProjectTask, b: ProjectTask) => a.priority - b.priority || time(b.updated_at) - time(a.updated_at);
  out.doing.sort(urgent);
  out.review.sort(urgent);
  out.queue.sort((a, b) => Number(a.status === "blocked") - Number(b.status === "blocked") || a.priority - b.priority || time(a.created_at) - time(b.created_at));
  out.done.sort((a, b) => time(b.updated_at) - time(a.updated_at));
  return out;
}

/** How many a column holds. Finished tasks are counted by the host, since the board does not load them until asked. */
export function columnCount(column: Column, arranged: Arranged, counts?: ProjectBoardData["counts"]): number {
  if (column === "done") return counts ? (counts.done ?? 0) + (counts.dropped ?? 0) : arranged.done.length;
  return arranged[column].length;
}

/** The phone's chips: one per column that has something in it, and always "Needs you" when anything waits. */
export function chips(arranged: Arranged, counts?: ProjectBoardData["counts"]): { column: Column; count: number }[] {
  return COLUMNS.map((column) => ({ column, count: columnCount(column, arranged, counts) })).filter((c) => c.count > 0);
}

/** The sections a phone lists under its chips. No chip: everything open, with finished work folded
 *  away; a chip: that column alone, finished work included when that is the chip. */
export function sections(filter: Column | null, arranged: Arranged): Column[] {
  if (filter) return [filter];
  return COLUMNS.filter((c) => c !== "done" && arranged[c].length > 0);
}

/** The chip a tap leaves pressed: a second tap on the pressed one lets go of it. */
export function toggleFilter(current: Column | null, tapped: Column): Column | null {
  return current === tapped ? null : tapped;
}

/** The brief parts still empty. A staff member is not started on a task with any of them missing. */
export function missingBrief(brief: Brief): BriefField[] {
  return BRIEF_FIELDS.filter((field) => !brief[field]?.trim());
}

export type StatusLine =
  | { kind: "working"; status: StaffStatus; since: string | null; waiting: string }
  | { kind: "after"; title: string; more: number }
  | { kind: "waiting"; name: string }
  | { kind: "none" };

/** What a card says beside its assignee: their status while they work on this task, the task it
 *  waits for when it is blocked, who will take it. */
export function statusLine(task: ProjectTask, titles: Record<string, { title: string; status: TaskStatus }>): StatusLine {
  if (task.assignee?.on_task) return { kind: "working", status: task.assignee.status, since: task.assignee.status_at, waiting: task.assignee.waiting_for };
  const open = task.depends_on.filter((id) => titles[id] && titles[id].status !== "done" && titles[id].status !== "dropped");
  if (task.status === "blocked" && open.length > 0) return { kind: "after", title: titles[open[0]].title, more: open.length - 1 };
  if (task.assignee && (task.status === "todo" || task.status === "blocked")) return { kind: "waiting", name: task.assignee.name };
  return { kind: "none" };
}

/** Only the brief parts that changed, so an edit never writes back a part another window changed meanwhile. */
export function briefChanges(before: Brief, after: Brief): Partial<Brief> {
  const out: Partial<Brief> = {};
  for (const field of BRIEF_FIELDS) if ((before[field] ?? "") !== (after[field] ?? "")) out[field] = after[field];
  return out;
}

/** Where a task can be moved from where it stands. Done is not offered: a finished task is the
 *  operator's acceptance from review, which is its own button. */
export const NEXT: Record<TaskStatus, TaskStatus[]> = {
  todo: ["doing", "blocked", "dropped"],
  blocked: ["todo", "dropped"],
  doing: ["review", "todo", "blocked"],
  review: ["doing"],
  done: ["todo"],
  dropped: ["todo"],
};

export const emptyBrief = (): Brief => ({ objective: "", deliverable: "", boundaries: "", done_when: "" });
