// @vitest-environment jsdom
// A project's focus mode decided without a browser: the route, the panel's tabs, the team as the
// sidebar draws it, the orchestrator's steps and events, and the one entry in the agents list.

import { describe, expect, it } from "vitest";
import type { ProjectFolder, SessionSummary } from "../api";
import { arrange } from "../grouping";
import { PANEL_TABS, PROJECT_TABS, defaultPanelTab, readPanelQuery, readPanelTab, tabsFor } from "../panel";
import { parse, projectHome, projectPagePath, projectSessionPath } from "../router";
import { eventTone, parseEvents, systemNote } from "../turns";
import type { Staff } from "../team/team";
import { askIdOf, canFocus, firstWait, focusView, isChat, splitTeam, staffTone, stepDetail, stepKey, STEP_KEYS, waitKey } from "./focus";

function member(over: Partial<Staff> = {}): Pick<Staff, "status" | "queued" | "one_off" | "archived_at"> {
  return { status: "off", queued: [], one_off: false, archived_at: null, ...over };
}

describe("a project's route", () => {
  it("names the orchestrator, a session of the project, or one of its pages", () => {
    const home = parse("/app/project/p1", "");
    expect([home.screen, home.project, home.page, home.inner]).toEqual(["project", "p1", null, null]);
    expect(focusView(home.page, home.inner)).toEqual({ kind: "orchestrator" });
    const staff = parse("/app/project/p1/s/sess-ira", "?panel=board");
    expect(focusView(staff.page, staff.inner)).toEqual({ kind: "session", id: "sess-ira" });
    expect(staff.query.get("panel")).toBe("board");
    expect(focusView("journal", null)).toEqual({ kind: "page", page: "journal" });
    // A page this build does not have is the project's home, not a blank centre.
    expect(focusView("nonsense", null)).toEqual({ kind: "orchestrator" });
    expect(focusView("s", null)).toEqual({ kind: "orchestrator" });
    expect(isChat({ kind: "orchestrator" })).toBe(true);
    expect(isChat({ kind: "page", page: "board" })).toBe(false);
  });

  it("builds the addresses it parses", () => {
    expect(projectHome("p 1", { panel: "board" })).toBe("/app/project/p%201?panel=board");
    expect(projectSessionPath("p1", "s1")).toBe("/app/project/p1/s/s1");
    expect(projectPagePath("p1", "terminals", { t: "t9", other: null })).toBe("/app/project/p1/terminals?t=t9");
    const back = parse("/app/project/p1/s/s1", "");
    expect([back.page, back.inner]).toEqual(["s", "s1"]);
  });

  it("is offered for real projects only", () => {
    expect(canFocus({ system: "", settings: { snapshots: true } })).toBe(true);
    expect(canFocus({ system: "voice", settings: { snapshots: false } })).toBe(false);
    expect(canFocus({ settings: { snapshots: true, ephemeral: true } })).toBe(false);
    expect(canFocus(null)).toBe(false);
  });
});

describe("the right panel in focus mode", () => {
  it("offers the project's tabs beside the orchestrator, and both sets beside a session of the project", () => {
    expect(tabsFor("session")).toEqual(["details", "files", "preview", "jobs"]);
    expect(tabsFor("orchestrator")).toEqual(["board", "brief", "wakeups", "folders"]);
    expect(tabsFor("member")).toEqual([...PANEL_TABS, ...PROJECT_TABS]);
  });

  it("reads only the tabs the context has from a link", () => {
    const q = new URLSearchParams("panel=details");
    expect(readPanelQuery(q, tabsFor("orchestrator"))).toBeNull();
    expect(readPanelQuery(new URLSearchParams("panel=brief"), tabsFor("orchestrator"))?.tab).toBe("brief");
    expect(readPanelQuery(new URLSearchParams("panel=brief"))).toBeNull();
  });

  it("opens on the context's first tab, and remembers the project's tab apart from a session's", () => {
    expect(defaultPanelTab(null, 1440, tabsFor("orchestrator"))).toBe("board");
    expect(defaultPanelTab("details", 1440, tabsFor("orchestrator"))).toBe("board");
    const kept = new Map<string, string>([["daedalus.session.panel", "files"], ["daedalus.session.panel.project", "brief"]]);
    const storage = { getItem: (k: string) => kept.get(k) ?? null };
    expect(readPanelTab(1440, storage, "session")).toBe("files");
    expect(readPanelTab(1440, storage, "orchestrator")).toBe("brief");
    expect(readPanelTab(900, storage, "orchestrator")).toBeNull();
  });
});

describe("the team in the sidebar", () => {
  it("colours each member by what the operator would do about it", () => {
    expect(staffTone(member({ status: "working" }))).toBe("working");
    expect(staffTone(member({ status: "starting" }))).toBe("working");
    expect(staffTone(member({ status: "permission" }))).toBe("waiting");
    expect(staffTone(member({ status: "turn_done_unseen" }))).toBe("review");
    expect(staffTone(member({ status: "idle" }))).toBe("free");
    // Silence is grey, not red: silent is not failed.
    expect(staffTone(member({ status: "no_signal" }))).toBe("silent");
    expect(staffTone(member({ status: "error" }))).toBe("error");
  });

  it("says why a launch waits, first in the queue first", () => {
    const queued = [
      { staff_id: "a", task_id: "t2", priority: 3, position: 4, reason: "project", detail: "6 of 6 staff are working", since: 1, by: "operator" },
      { staff_id: "a", task_id: "t1", priority: 1, position: 2, reason: "machine", detail: "20 of 20 terminal sessions are running", since: 2, by: "orchestrator" },
    ];
    const waiting = member({ status: "off", queued });
    expect(staffTone(waiting)).toBe("waiting");
    expect(firstWait(waiting)?.task_id).toBe("t1");
    expect(waitKey("machine")).toBe("focus.wait.machine");
    // A reason the host adds later still reads as something, never as a bare key.
    expect(waitKey("tea-break")).toBe("focus.wait.queued");
    expect(waitKey(null)).toBe("focus.wait.queued");
    expect(firstWait(member())).toBeNull();
  });

  it("lists the one-off helpers apart and leaves the dismissed out", () => {
    const staff = [
      { id: "a", one_off: false, archived_at: null },
      { id: "b", one_off: true, archived_at: null },
      { id: "c", one_off: false, archived_at: "2026-09-24T00:00:00Z" },
    ];
    const { team, oneOff } = splitTeam(staff);
    expect(team.map((m) => m.id)).toEqual(["a"]);
    expect(oneOff.map((m) => m.id)).toEqual(["b"]);
  });
});

describe("the orchestrator's steps", () => {
  it("names each step by what it did, not by the tool", () => {
    expect(stepKey("Folders", { op: "add", path: "/work/bakery-bot" })).toBe("Folders.add");
    expect(stepKey("Folders", {})).toBe("Folders.list");
    expect(stepKey("Tasks", { op: "create", title: "Notify: endpoint" })).toBe("Tasks.create");
    expect(stepKey("Brief", { section: "goals" })).toBe("Brief.read");
    expect(stepKey("Brief", { section: "goals", body: "…" })).toBe("Brief.write");
    expect(stepKey("Team", { concurrency: 4 })).toBe("Team.concurrency");
    expect(stepKey("Assign", { staff: "Max" })).toBe("Assign");
    expect(stepKey("Exec", { command: "ls" })).toBe("other");
    for (const key of ["Folders.add", "Tasks.create", "Assign", "Watch", "AskOperator", "other"]) expect(STEP_KEYS).toContain(key);
  });

  it("shows what a step acted on", () => {
    expect(stepDetail("Folders", { op: "add", path: "/work/bakery-bot" })).toBe("/work/bakery-bot");
    expect(stepDetail("Tasks", { op: "move", task_id: "t3a9c1", status: "review" })).toBe("t3a9c1 → review");
    expect(stepDetail("Assign", { staff: "Max", title: "Notify: endpoint" })).toBe("Max · Notify: endpoint");
    expect(stepDetail("AskOperator", { question: "Postgres or SQLite?\nContext…" })).toBe("Postgres or SQLite?");
  });

  it("finds the request a tool result names", () => {
    expect(askIdOf("asked the operator as [qk7m2x]; do not wait")).toBe("qk7m2x");
    expect(askIdOf("done")).toBeNull();
    expect(askIdOf(undefined)).toBeNull();
  });
});

describe("the events the orchestrator was woken with", () => {
  const batch = [
    "[events · Bakery 2.0 · 3 since 14:30]",
    '- 14:31 Max (Codex) finished a turn on "Notification: endpoint" (t3a9c1): "3 files, tests green" — ReadStaff("Max") for the whole reply',
    "- 14:33 Naya needs permission [qk7m2]: Exec: npm install grammy (in bakery-bot) (autonomy normal) — yours to answer or escalate",
    "- 14:34 the operator moved \"Menu photos\" (t88e02) review → doing",
    "- … and 2 more (Team, Tasks)",
  ].join("\n");

  it("is recognised by its origin and drawn as a card", () => {
    expect(systemNote({ role: "user", text: batch, origin: "events", internal: false })?.kind).toBe("events");
  });

  it("reads back each line with its time and tone, without the advice meant for the model", () => {
    const parsed = parseEvents(batch)!;
    expect([parsed.project, parsed.count, parsed.since, parsed.more]).toEqual(["Bakery 2.0", 3, "14:30", 2]);
    expect(parsed.lines.map((l) => [l.time, l.tone])).toEqual([["14:31", "ok"], ["14:33", "warn"], ["14:34", "info"]]);
    expect(parsed.lines[0].text).toBe('Max (Codex) finished a turn on "Notification: endpoint" (t3a9c1): "3 files, tests green"');
    expect(parsed.lines[1].text).not.toContain("yours to answer");
    expect(parsed.lines[1].ask).toBe("qk7m2");
  });

  it("keeps a wrapped line with its event and refuses anything that is not a batch", () => {
    const wrapped = parseEvents("[events · P · 1 since 09:00]\n- 09:01 Ada reported stuck: \"the build\nfails on arm\"")!;
    expect(wrapped.lines).toHaveLength(1);
    expect(wrapped.lines[0].text).toContain("fails on arm");
    expect(wrapped.lines[0].tone).toBe("warn");
    expect(parseEvents("Hello")).toBeNull();
    expect(eventTone("Ada stopped with an error on \"Menu\": boom")).toBe("bad");
  });
});

describe("the agents list", () => {
  const at = "2026-09-24T10:00:00Z";
  function agent(id: string, project: string, over: Partial<SessionSummary> = {}): SessionSummary {
    return { id, title: id, status: "idle", created_at: at, last_message_at: at, run_id: null, project_id: project, project, ...over };
  }
  function folder(id: string, over: Partial<ProjectFolder> = {}): ProjectFolder {
    return { id, name: id, created_at: at, settings: { snapshots: false }, folders: [], total: 0, active: 0, loops: 0, last_message_at: "", ...over };
  }

  it("draws an orchestrated project as one entry with its counts, and lists none of its sessions", () => {
    const orchestration = { enabled: true, session_id: "orch", staff: 4, working: 2, needs_you: 1 };
    const projects = [folder("bakery", { total: 3, orchestrator: orchestration }), folder("plain", { total: 1, orchestrator: null })];
    const sessions = [agent("orch", "bakery", { metadata: { orchestrator_of: "bakery" } }), agent("ira", "bakery", { metadata: { staff_id: "st-ira" } }), agent("notes", "plain")];
    const { folders } = arrange(sessions, projects);
    const bakery = folders.find((f) => f.key === "bakery")!;
    expect(bakery.orchestrated).toEqual(orchestration);
    expect(bakery.rows).toEqual([]);
    expect(bakery.single).toBe(false);
    const plain = folders.find((f) => f.key === "plain")!;
    expect(plain.orchestrated).toBeNull();
    expect(plain.rows.map((r) => r.s.id)).toEqual(["notes"]);
    // The entry redraws when its counts change: they are its whole signature.
    const busier = arrange(sessions, [folder("bakery", { total: 3, orchestrator: { ...orchestration, working: 3 } })]).folders[0];
    expect(busier.sig).not.toBe(bakery.sig);
  });

  it("still finds an orchestrated project's sessions in a search", () => {
    const projects = [folder("bakery", { orchestrator: { enabled: true, session_id: "orch", staff: 1, working: 0, needs_you: 0 } })];
    const { folders } = arrange([agent("ira", "bakery")], projects, { results: true });
    expect(folders[0].rows.map((r) => r.s.id)).toEqual(["ira"]);
  });
});
