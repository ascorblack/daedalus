// @vitest-environment jsdom
// The two modes decided without a browser: which route belongs to which, what the Agents list leaves
// to orchestration mode, what the switch counts, and where an old link or a plain session address
// lands. The operator asked for the modes apart; these are the rules that keep them so.

import { afterEach, describe, expect, it } from "vitest";
import type { MainAsk, MainView, ProjectFolder, ProjectRef, SessionSummary } from "./api";
import { arrange } from "./grouping";
import { agentsListing, modeHome, modeOf, orchestratedProjects, orchestrationPathOf, rememberMode, storedMode, waitingInOrchestration } from "./mode";
import { canonical, parse, projectHome, projectSessionPath } from "./router";

const at = "2026-09-25T10:00:00Z";

function agent(id: string, project: string, over: Partial<SessionSummary> = {}): SessionSummary {
  return { id, title: id, status: "idle", created_at: at, last_message_at: at, run_id: null, project_id: project, project, ...over };
}

function folder(id: string, over: Partial<ProjectFolder> = {}): ProjectFolder {
  return { id, name: id, created_at: at, settings: { snapshots: false }, folders: [], total: 0, active: 0, loops: 0, last_message_at: "", ...over };
}

const conductor = (over: Partial<NonNullable<ProjectFolder["orchestrator"]>> = {}) => ({ enabled: true, session_id: "orch", staff: 4, working: 2, needs_you: 0, ...over });

function ask(over: Partial<MainAsk> = {}): MainAsk {
  return {
    id: "ask-1", short_id: "q1abcd", project_id: "bakery", origin: "orchestrator", kind: "question", staff_id: null, task_id: null,
    text: "?", detail: {}, routed_to: "operator", suggestion: "", created_at: at, resolved_at: null, resolved_by: null, resolution: {},
    dispatch_id: "d1", project_name: "Bakery", asker: "orchestrator", host: false, ...over,
  };
}

function main(asks: MainAsk[]): MainView {
  return { session_id: "main-1", dispatches: [], asks, questions: asks.length, setup: [] };
}

afterEach(() => localStorage.clear());

describe("the mode of a route", () => {
  it("is the route's own for Agents and Orchestration, and none for the screens both share", () => {
    expect(modeOf(parse("/app/agents", ""))).toBe("agents");
    expect(modeOf(parse("/app/agents/s1", ""))).toBe("agents");
    expect(modeOf(parse("/app/orchestration", ""))).toBe("orchestration");
    expect(modeOf(parse("/app/orchestration/projects", ""))).toBe("orchestration");
    expect(modeOf(parse(projectHome("p1"), ""))).toBe("orchestration");
    expect(modeOf(parse("/app/terminals", ""))).toBeNull();
    expect(modeOf(parse("/app/harnesses", ""))).toBeNull();
    expect(modeOf(parse("/app/settings/models", ""))).toBeNull();
  });

  it("is remembered per device, Agents until a choice was made", () => {
    expect(storedMode()).toBe("agents");
    rememberMode("orchestration");
    expect(storedMode()).toBe("orchestration");
    expect(modeHome("orchestration")).toBe("/app/orchestration");
    expect(modeHome("agents")).toBe("/app/agents");
  });

  it("opens orchestration on a phone at its list, and on a desktop at the main chat", () => {
    // The desktop's column is the list, so the centre can be the main chat; a phone has no column.
    expect(modeHome("orchestration", false)).toBe("/app/orchestration/projects");
    expect(modeHome("orchestration", true)).toBe("/app/orchestration");
    expect(modeHome("agents", false)).toBe("/app/agents");
  });

  it("reads the phone's list of projects apart from the main chat", () => {
    const list = parse("/app/orchestration/projects", "");
    expect([list.screen, list.detail, list.project]).toEqual(["orchestration", "projects", null]);
  });
});

describe("old links", () => {
  it("lead into orchestration mode, with the rest of the address kept", () => {
    expect(canonical("/app/project/p1")).toBe("/app/orchestration/project/p1");
    expect(canonical("/app/project/p1/board?task=t1")).toBe("/app/orchestration/project/p1/board?task=t1");
    expect(canonical("/app/project/p1/staff/st-ira")).toBe("/app/orchestration/project/p1/staff/st-ira");
    expect(canonical("/app/main")).toBe("/app/orchestration");
    expect(canonical("/app/main?panel=jobs")).toBe("/app/orchestration?panel=jobs");
    // Nothing else is touched, not even a path that merely begins with the same letters.
    expect(canonical("/app/agents/s1")).toBe("/app/agents/s1");
    expect(canonical("/app/projects")).toBe("/app/projects");
    expect(canonical("/app/mainly")).toBe("/app/mainly");
    const old = parse("/app/project/p1/s/sess-ira", "");
    expect([old.screen, old.project, old.page, old.inner]).toEqual(["orchestration", "p1", "s", "sess-ira"]);
  });
});

describe("the Agents list", () => {
  const projects = [folder("bakery", { total: 3, orchestrator: conductor() }), folder("plain", { total: 1, orchestrator: null }), folder("p-main", { system: "dispatcher", total: 2 })];
  const sessions = [
    agent("orch", "bakery", { metadata: { orchestrator_of: "bakery" } }),
    agent("ira", "bakery", { metadata: { staff_id: "st-ira" } }),
    agent("old-chat", "bakery"),
    agent("notes", "plain"),
    agent("main-now", "p-main", { metadata: { dispatcher: true } }),
    agent("main-before", "p-main", { metadata: { dispatcher_retired: true } }),
  ];

  it("holds none of an orchestrated project, its orchestrator, its staff, nor the main chats", () => {
    const listing = agentsListing({ sessions, projects });
    expect(listing.projects.map((p) => p.id)).toEqual(["plain"]);
    expect(listing.sessions.map((s) => s.id)).toEqual(["notes"]);
    const { folders } = arrange(listing.sessions, listing.projects);
    expect(folders.map((f) => [f.key, f.rows.map((r) => r.s.id)])).toEqual([["plain", ["notes"]]]);
  });

  it("leaves a search's results to the same rule", () => {
    const listing = agentsListing({ sessions: [agent("ira", "bakery"), agent("notes", "plain")], projects, semantic: false });
    expect(listing.sessions.map((s) => s.id)).toEqual(["notes"]);
    expect(listing.semantic).toBe(false);
  });

  it("takes a project back once its orchestrator is switched off", () => {
    const off = [folder("bakery", { total: 3, orchestrator: null })];
    expect(agentsListing({ sessions: [agent("old-chat", "bakery")], projects: off }).sessions.map((s) => s.id)).toEqual(["old-chat"]);
  });

  it("is the same object when there is nothing to take out, so memos below it hold", () => {
    const listing = { sessions: [agent("notes", "plain")], projects: [folder("plain")] };
    expect(agentsListing(listing)).toBe(listing);
  });
});

describe("orchestration mode's list", () => {
  it("holds only the projects with an orchestrator, the most recent first", () => {
    const list = orchestratedProjects([
      folder("old", { orchestrator: conductor(), last_message_at: "2026-09-20T10:00:00Z" }),
      folder("plain", { orchestrator: null, last_message_at: "2026-09-25T11:00:00Z" }),
      folder("off", { orchestrator: conductor({ enabled: false }) }),
      folder("new", { orchestrator: conductor(), last_message_at: "2026-09-25T09:00:00Z" }),
      folder("p-main", { system: "dispatcher" }),
    ]);
    expect(list.map((p) => p.id)).toEqual(["new", "old"]);
  });
});

describe("the count on the switch", () => {
  it("adds what waits in every orchestrated project and the main chat's own, each once", () => {
    const projects = [folder("bakery", { orchestrator: conductor({ needs_you: 2 }) }), folder("garden", { orchestrator: conductor({ needs_you: 1 }) }), folder("plain", { orchestrator: null })];
    const view = main([
      ask({ id: "mirrored", project_id: "bakery" }),
      ask({ id: "create", project_id: null, origin: "dispatcher", kind: "project" }),
      ask({ id: "answered", project_id: null, resolved_at: at }),
      ask({ id: "not-mine", project_id: null, routed_to: "orchestrator" }),
    ]);
    expect(waitingInOrchestration(projects, view)).toBe(4);
    expect(waitingInOrchestration(projects, null)).toBe(3);
    expect(waitingInOrchestration([folder("plain")], main([]))).toBe(0);
  });
});

describe("a session opened by its plain address", () => {
  const ref = (id: string, over: Partial<ProjectRef> = {}) => ({ id, system: "", settings: { snapshots: false }, ...over }) as Pick<ProjectRef, "id" | "system" | "settings">;
  const on = { enabled: true, session_id: "orch", model: "", autonomy: "normal" as const, concurrency: 3, concurrency_cap: 10, telegram_topic_id: 0 };

  it("moves into orchestration mode when it belongs there, and stays when it does not", () => {
    const bakery = ref("bakery", { settings: { snapshots: false, orchestrator: on } });
    expect(orchestrationPathOf({ id: "orch", project: bakery }, "main-1")).toBe(projectHome("bakery"));
    expect(orchestrationPathOf({ id: "ira", project: bakery }, "main-1")).toBe(projectSessionPath("bakery", "ira"));
    expect(orchestrationPathOf({ id: "main-1", project: ref("p-main", { system: "dispatcher" }) }, "main-1")).toBe("/app/orchestration");
    // A replaced main chat is history; an ordinary chat is Agents mode's.
    expect(orchestrationPathOf({ id: "main-0", project: ref("p-main", { system: "dispatcher" }) }, "main-1")).toBeNull();
    expect(orchestrationPathOf({ id: "notes", project: ref("plain") }, "main-1")).toBeNull();
    expect(orchestrationPathOf({ id: "notes", project: ref("was", { settings: { snapshots: false, orchestrator: { ...on, enabled: false } } }) }, null)).toBeNull();
  });
});
