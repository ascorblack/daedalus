import { beforeEach, describe, expect, it } from "vitest";
import type { ProjectFolder, SessionSummary } from "./api";
import { FREE, arrange, folderOpen, rememberFolder } from "./grouping";

let clock = 1_700_000_000_000;

function agent(id: string, over: Partial<SessionSummary> = {}): SessionSummary {
  clock += 1000;
  const at = new Date(clock).toISOString();
  return { id, title: id, status: "idle", created_at: at, last_message_at: at, run_id: null, ...over };
}

function folder(id: string, name: string, over: Partial<ProjectFolder> = {}): ProjectFolder {
  return {
    id,
    name,
    root: `/projects/${name}`,
    created_at: "2026-09-01T00:00:00Z",
    settings: { snapshots: false, system: "" },
    reachable: true,
    writable: true,
    total: 0,
    active: 0,
    loops: 0,
    last_message_at: "",
    ...over,
  };
}

const NOTHING = { total: 0, active: 0, loops: 0, last_message_at: "" };

describe("arrange", () => {
  it("puts each agent in its project and everything else in the free bucket", () => {
    const projects = [folder("p1", "Bakery", { total: 2 }), folder("p2", "Expenses", { total: 1 })];
    const sessions = [
      agent("a", { project_id: "p1" }),
      agent("b", { project_id: "p1" }),
      agent("c", { project_id: "p2" }),
      agent("d"),
      // A project that was removed elsewhere: its agents are free, not in a folder that is gone.
      agent("e", { project_id: "vanished" }),
    ];
    const { folders } = arrange(sessions, projects, { ...NOTHING, total: 2 });
    expect(folders.map((f) => f.key)).toEqual(["p1", "p2", FREE]);
    expect(folders[0].rows.map((r) => r.s.id)).toEqual(["a", "b"]);
    expect(folders[2].rows.map((r) => r.s.id)).toEqual(["d", "e"]);
    // The count on a folder is the API's, over the whole table — not the rows that reached this page.
    expect(folders[0].total).toBe(2);
    expect(folders[2].total).toBe(2);
  });

  it("puts the concierge's own project first and the operator's in name order", () => {
    const projects = [folder("p2", "Zebra"), folder("p1", "Apples"), folder("v", "Voice", { system: "voice" })];
    const { folders } = arrange([], projects, NOTHING);
    expect(folders.map((f) => f.name)).toEqual(["Voice", "Apples", "Zebra", ""]);
    expect(folders[0].system).toBe(true);
    expect(folders[3].project).toBeNull();
  });

  it("nests a subagent under its leader and a fork under what it was taken from", () => {
    const sessions = [
      agent("leader"),
      agent("sub", { metadata: { subagent_of: "leader", subagent_name: "triage" } }),
      agent("fork", { metadata: { forked_from: { session_id: "leader", seq: 12 } } }),
      agent("forkfork", { metadata: { forked_from: { session_id: "fork", seq: 20 } } }),
      // A fork of a session that is not in the list stands on its own.
      agent("orphan-fork", { metadata: { forked_from: { session_id: "gone", seq: 3 } } }),
    ];
    const { folders } = arrange(sessions, [], NOTHING);
    const rows = folders[0].rows;
    expect(rows.map((r) => r.s.id)).toEqual(["leader", "orphan-fork"]);
    expect(rows[0].kids.map((k) => k.id)).toEqual(["sub"]);
    expect(rows[0].forks.map((f) => f.s.id)).toEqual(["fork"]);
    expect(rows[0].forks[0].forks.map((f) => f.s.id)).toEqual(["forkfork"]);
  });

  it("counts a leader whose subagent is running as working", () => {
    const sessions = [agent("leader"), agent("sub", { status: "running", metadata: { subagent_of: "leader" } })];
    const { active, total } = arrange(sessions, [], NOTHING);
    expect(total).toBe(1);
    expect(active).toBe(1);
  });

  it("applies a filter inside every folder, the free bucket included", () => {
    const projects = [folder("p1", "Bakery")];
    const loop = { mode: "interval" as const, interval_seconds: 60, status: "active" as const, run_count: 1, max_runs: null, next_run_at: null, last_run_at: null, last_reason: null, stop_reason: null, pause_note: null, instruction: "x" };
    const sessions = [
      agent("busy", { status: "running", project_id: "p1" }),
      agent("quiet", { project_id: "p1" }),
      agent("looping", { metadata: { loop } }),
      agent("alone"),
    ];
    const working = arrange(sessions, projects, NOTHING, { filter: "working" });
    expect(working.folders[0].rows.map((r) => r.s.id)).toEqual(["busy"]);
    expect(working.folders[1].rows).toEqual([]);
    expect(working.shown).toBe(1);
    const loops = arrange(sessions, projects, NOTHING, { filter: "loops" });
    expect(loops.folders[1].rows.map((r) => r.s.id)).toEqual(["looping"]);
    // The header's own counts describe the screen and not the filter, which is what "All · n" means.
    expect(loops.total).toBe(4);
    expect(loops.loops).toBe(1);
  });

  it("searches across the folders and keeps a matching fork's parent on screen", () => {
    const projects = [folder("p1", "Bakery")];
    const sessions = [
      agent("menu", { title: "Menu page", project_id: "p1" }),
      agent("photos", { title: "Photos" }),
      agent("fork", { title: "Menu page (fork)", metadata: { forked_from: { session_id: "menu", seq: 4 } } }),
      agent("sub", { title: "[sub] menu prices", metadata: { subagent_of: "photos", subagent_name: "menu prices" } }),
    ];
    const found = arrange(sessions, projects, NOTHING, { query: "menu" });
    expect(found.folders[0].rows.map((r) => r.s.id)).toEqual(["menu"]);
    expect(found.folders[0].rows[0].forks.map((f) => f.s.id)).toEqual(["fork"]);
    // "Photos" itself does not match; the subagent named "menu prices" under it does.
    expect(found.folders[1].rows.map((r) => r.s.id)).toEqual(["photos"]);
    expect(arrange(sessions, projects, NOTHING, { query: "nothing here" }).shown).toBe(0);
  });

  it("narrows the whole screen to one project, free bucket and all", () => {
    const projects = [folder("p1", "Bakery"), folder("p2", "Expenses")];
    const sessions = [agent("a", { project_id: "p1" }), agent("b", { project_id: "p2" }), agent("c")];
    const { folders, total } = arrange(sessions, projects, NOTHING, { project: "p1" });
    expect(folders.map((f) => f.key)).toEqual(["p1"]);
    expect(total).toBe(1);
  });
});

describe("folderOpen", () => {
  // There is no DOM in this repo's vitest setup, so the store the module reads is supplied here.
  // What is being tested is the default and the per-folder memory, not the browser's implementation.
  const store = new Map<string, string>();
  beforeEach(() => {
    store.clear();
    (globalThis as { localStorage?: unknown }).localStorage = {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    };
  });

  it("is collapsed until it is opened, and remembers per folder", () => {
    expect(folderOpen("p1")).toBe(false);
    rememberFolder("p1", true);
    expect(folderOpen("p1")).toBe(true);
    expect(folderOpen("p2")).toBe(false);
    rememberFolder("p1", false);
    expect(folderOpen("p1")).toBe(false);
  });
});
