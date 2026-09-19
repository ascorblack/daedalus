// How the Agents screen is arranged: one folder for every project and its agents.
//
// All of it is pure, and that is the point — the screen renders what these functions return, and a
// test can ask what the arrangement is without a browser. Nesting (a subagent under its leader, a
// fork under the session it was taken from) is decided here too, so one pass over the rows answers
// both questions and the screen does no bookkeeping of its own.

import { ProjectFolder, SessionSummary } from "./api";

export type Filter = "all" | "working" | "loops";
export type Kind = "waiting" | "working" | "loop" | "idle";

/** One agent and what hangs off it: its subagents, and the forks taken from it (each with its own). */
export type Row = { s: SessionSummary; kids: SessionSummary[]; forks: Row[] };

/** A project's folder in the list. */
export type Folder = {
  key: string;
  name: string;
  project: ProjectFolder;
  /** True on the installation's own project — the concierge's — which is drawn with a mic. */
  system: boolean;
  single: boolean;
  rows: Row[];
  /** What the rows of this folder would look different for, built once while they are arranged.
   *  The listing is re-fetched whole every few seconds and every object in it is new, so a folder
   *  that did not change has nothing but this to prove it by. */
  sig: string;
  /** Counted over the whole table by the API, not over the rows above: a folder says how many agents
   *  are in it even where the page of rows did not reach them all. */
  total: number;
  active: number;
  loops: number;
  last_message_at: string;
};

const OPEN_PREFIX = "daedalus.folder.";

/** Whether a folder is expanded. Collapsed is the default: a folder that opens itself is a list again. */
export function folderOpen(key: string): boolean {
  try {
    return localStorage.getItem(OPEN_PREFIX + key) === "1";
  } catch {
    return false;
  }
}

export function rememberFolder(key: string, open: boolean): void {
  try {
    if (open) localStorage.setItem(OPEN_PREFIX + key, "1");
    else localStorage.removeItem(OPEN_PREFIX + key);
  } catch {
    /* private mode: the folder just forgets between visits */
  }
}

/** What the status chips count and what the Active filter keeps. */
export function kindOf(s: SessionSummary, childRunning: boolean): Kind {
  if (s.status === "waiting") return "waiting";
  if (s.status === "running" || childRunning) return "working";
  if (s.metadata?.loop && s.metadata.loop.status === "active") return "loop";
  return "idle";
}

/** The name a subagent is listed under: the one its leader gave it, not the `[sub]` title. */
export function agentName(s: SessionSummary): string {
  return s.metadata?.subagent_of ? s.metadata?.subagent_name || s.title.replace(/^\[sub\]\s*/, "") : s.title;
}

export type Arranged = {
  folders: Folder[];
  /** Every top-level agent that survived the filter and the search, for the header's counts. */
  shown: number;
  /** Top-level agents before either, which is what "All · n" means. */
  total: number;
  active: number;
  loops: number;
};

function activity(s: SessionSummary): number {
  return Date.parse(s.last_message_at || s.created_at) || 0;
}

function rowSig(rows: Row[]): string {
  return rows.map((r) => `${r.s.id}:${r.s.status}:${r.s.last_message_at}:${r.s.title}:${r.s.model ?? ""}:${r.s.match?.snippet ?? ""}:${r.kids.map((k) => k.id + k.status).join(",")}|${rowSig(r.forks)}`).join(";");
}

/**
 * Arrange the listing.
 *
 * `project` narrows the whole screen to one project (the shell's switcher); `filter` and `query`
 * apply inside every folder because a reader who filters wants the answer across the screen.
 */
export function arrange(
  sessions: SessionSummary[],
  projects: ProjectFolder[],
  opts: { project?: string; filter?: Filter; query?: string; results?: boolean } = {},
): Arranged {
  const { project = "", filter = "all", query = "" } = opts;
  const all = (project ? sessions.filter((s) => s.project_id === project) : [...sessions]).sort((a, b) =>
    opts.results ? (b.match?.score ?? 0) - (a.match?.score ?? 0) : activity(b) - activity(a) || a.id.localeCompare(b.id));
  const ids = new Set(all.map((s) => s.id));

  // A subagent sits under its leader; one whose leader is gone is listed on its own.
  const children = new Map<string, SessionSummary[]>();
  for (const s of all) {
    const leader = s.metadata?.subagent_of;
    if (!opts.results && leader && ids.has(leader)) children.set(leader, [...(children.get(leader) ?? []), s]);
  }
  // A fork sits under the session it was taken from: it has a copy of that session's files as of the
  // fork point, and the kinship is what the reader is looking for. A fork whose origin is gone is listed on its own.
  const forks = new Map<string, SessionSummary[]>();
  for (const s of all) {
    const origin = s.metadata?.forked_from?.session_id;
    if (!opts.results && origin && origin !== s.id && ids.has(origin) && !s.metadata?.subagent_of) forks.set(origin, [...(forks.get(origin) ?? []), s]);
  }
  const nested = (s: SessionSummary) => !opts.results && ((!!s.metadata?.subagent_of && ids.has(s.metadata.subagent_of)) || (!!s.metadata?.forked_from?.session_id && ids.has(s.metadata.forked_from!.session_id) && !s.metadata?.subagent_of));

  const q = query.trim().toLowerCase();
  const hits = (s: SessionSummary) => !q || agentName(s).toLowerCase().includes(q) || (s.model ?? "").toLowerCase().includes(q) || (children.get(s.id) ?? []).some((c) => agentName(c).toLowerCase().includes(q));
  // A fork that matches keeps its parent on screen, or the row it hangs under would be gone with it.
  const matches = (s: SessionSummary) => hits(s) || (forks.get(s.id) ?? []).some(hits);
  const childRunning = (s: SessionSummary) => (children.get(s.id) ?? []).some((c) => c.status === "running" || c.status === "waiting");
  const kind = (s: SessionSummary) => kindOf(s, childRunning(s));
  const keeps = (s: SessionSummary) => (filter === "all" ? true : filter === "loops" ? kind(s) === "loop" : kind(s) === "waiting" || kind(s) === "working");

  const top = all.filter((s) => !nested(s));
  const kept = top.filter((s) => matches(s) && keeps(s));
  const row = (s: SessionSummary): Row => ({
    s,
    kids: children.get(s.id) ?? [],
    forks: (forks.get(s.id) ?? []).filter((f) => !q || hits(f)).map(row),
  });

  const byProject = new Map<string, Row[]>();
  for (const s of kept) {
    const key = s.project_id;
    if (!projects.some((p) => p.id === key)) continue;
    byProject.set(key, [...(byProject.get(key) ?? []), row(s)]);
  }

  // Empty projects use their creation time; Voice follows the same recency order as every project.
  const folders: Folder[] = projects
    .filter((p) => !project || p.id === project)
    .map((p) => ({
      key: p.id,
      name: p.name,
      project: p,
      system: !!p.system,
      single: (p.members ?? p.total) === 1 && (byProject.get(p.id)?.length ?? 0) === 1
        && !byProject.get(p.id)![0].kids.length && !byProject.get(p.id)![0].forks.length,
      rows: byProject.get(p.id) ?? [],
      sig: rowSig(byProject.get(p.id) ?? []),
      total: p.total,
      active: p.active,
      loops: p.loops,
      last_message_at: p.last_message_at || all.filter((s) => s.project_id === p.id).sort((a, b) => activity(b) - activity(a))[0]?.last_message_at || "",
    }))
    .sort((a, b) => Date.parse(b.last_message_at || b.project.created_at) - Date.parse(a.last_message_at || a.project.created_at) || a.key.localeCompare(b.key));
  return {
    folders,
    shown: kept.length,
    total: top.length,
    active: top.filter((s) => kind(s) === "waiting" || kind(s) === "working").length,
    loops: top.filter((s) => kind(s) === "loop").length,
  };
}
