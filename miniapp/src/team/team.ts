// A project's team as the app draws it: the shapes the host sends, and the few decisions the page
// makes from them — which executor can be chosen and why not, what a status is called, what branch a
// worktree will be given. Pure, so each one is tested without a browser.

export const HARNESSES = ["daedalus", "claude", "codex", "grok", "opencode", "pi"] as const;
export type Harness = (typeof HARNESSES)[number];

/** The two letters on a staff member's badge, the way the team page and the hiring form show an executor. */
export const HARNESS_BADGES: Record<Harness, string> = { daedalus: "D", claude: "CC", codex: "CX", grok: "GK", opencode: "OC", pi: "π" };

/** Each executor's own name. Product names: the same in every language, so they are not in the dictionary. */
export const HARNESS_NAMES: Record<Harness, string> = { daedalus: "Daedalus", claude: "Claude Code", codex: "Codex", grok: "Grok Build", opencode: "OpenCode", pi: "pi" };

/** Every status a staff member can show: the live session's, or "off" when there is none. */
export const STAFF_STATUSES = ["off", "starting", "working", "turn_done_unseen", "idle", "question", "permission", "error", "exited", "no_signal"] as const;
export type StaffStatus = (typeof STAFF_STATUSES)[number];

export const ISOLATIONS = ["worktree", "shared", "readonly"] as const;
export type Isolation = (typeof ISOLATIONS)[number];

/** The colours the host hands out, by the name of the stylesheet token for each. */
export const STAFF_COLOURS = ["blue", "green", "amber", "violet", "rose", "teal", "orange", "slate"] as const;

export type Env = "container" | "host";

export type StaffSession = {
  id: string;
  status: Exclude<StaffStatus, "off">;
  waiting_for: string;
  task_id: string | null;
  started_at: string;
  ended_at: string | null;
  branch: string | null;
  worktree_path: string | null;
};

export type Staff = {
  id: string;
  project_id: string;
  name: string;
  color: string;
  role: string;
  harness: Harness;
  agent: string;
  model: string;
  effort: string;
  permission_mode: string;
  env: "" | Env;
  default_folder_id: string | null;
  isolation: Isolation;
  instructions: string;
  notes: string;
  one_off: boolean;
  created_by: "operator" | "orchestrator";
  created_at: string;
  archived_at: string | null;
  live: StaffSession | null;
  status: StaffStatus;
  sessions: number;
};

export type TeamFolder = { id: string; path: string; label: string; env: Env; is_git: boolean; readonly: boolean };

export type Team = {
  project: {
    id: string;
    name: string;
    ephemeral: boolean;
    system: string;
    default_env: Env;
    local_env: Env;
    concurrency: number;
    concurrency_cap: number;
    orchestrator: boolean;
    folders: TeamFolder[];
  };
  staff: Staff[];
  counts: { staff: number; working: number };
  choices: { harnesses: Harness[]; personas: string[]; presets: { id: string; label: string }[]; default_preset: string };
};

/** What the harness manager knows about one command-line agent. Every field may be missing: the
 *  catalog belongs to another part of the installation, and absence reads as "not installed". */
export type CatalogEntry = {
  installed?: boolean;
  version?: string;
  latest?: string;
  logged_in?: boolean;
  agents?: { name: string; source?: string }[];
  models?: string[];
  error?: string;
  checked_at?: string;
};
export type Catalog = Partial<Record<Harness, CatalogEntry>>;

export type Unavailable = "" | "notinstalled" | "loggedout" | "error";

/** Whether an executor can be hired in an environment, and if not, why — as a dictionary key suffix.
 *
 *  Daedalus is always there. A command-line agent needs the catalog to say it is installed and signed
 *  in; with no catalog at all (an installation without the harness manager) every one of them is
 *  "not installed", which is the truth for this installation. */
export function availability(harness: Harness, catalog: Catalog | null): Unavailable {
  if (harness === "daedalus") return "";
  const entry = catalog?.[harness];
  if (!entry || !entry.installed) return entry?.error ? "error" : "notinstalled";
  if (entry.logged_in === false) return "loggedout";
  return "";
}

/** A name as it appears in a branch: lowercase, letters, digits, dot, dash and underscore.
 *
 *  The same rule the host's worktrees use, so the preview is the branch that will be made. A name
 *  with nothing left in it (written in another alphabet) becomes "staff" rather than an empty segment. */
export function branchSlug(name: string, max = 32): string {
  const slug = name
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/-{2,}/g, "-")
    .slice(0, max)
    .replace(/^[-._]+|[-._]+$/g, "");
  return slug || "staff";
}

/** The branch a staff member in its own worktree works on, with the task left as a placeholder. */
export function branchPreview(name: string, task: string): string {
  return `agent/${branchSlug(name)}/${task}`;
}

/** The session-status class the dot is drawn with: the colours the rest of the app already uses for "running", "waiting" and "failed". */
export function statusTone(status: StaffStatus): "running" | "waiting" | "failed" | "idle" {
  if (status === "working" || status === "starting") return "running";
  if (status === "question" || status === "permission") return "waiting";
  if (status === "error") return "failed";
  return "idle";
}

/** The letters in a staff member's avatar: the first letter of the first two words, or of the name. */
export function initials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (words.length >= 2) return (Array.from(words[0])[0] + Array.from(words[1])[0]).toUpperCase();
  return Array.from(words[0] ?? "?").slice(0, 2).join("").toUpperCase() || "?";
}

/** The colour token a member wears; an unknown name falls back to the first rather than drawing nothing. */
export function colourVar(color: string): string {
  return `var(--staff-${(STAFF_COLOURS as readonly string[]).includes(color) ? color : STAFF_COLOURS[0]})`;
}

/** Folders a member running in `env` can work in: the ones that live there. */
export function foldersFor(folders: TeamFolder[], env: Env): TeamFolder[] {
  return folders.filter((f) => f.env === env);
}

/** The isolation a new member starts with: its own worktree where there is a repository to make one in. */
export function defaultIsolation(folder: TeamFolder | undefined): Isolation {
  return folder && folder.is_git && !folder.readonly ? "worktree" : "shared";
}
