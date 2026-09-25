// @vitest-environment jsdom
// The main chat decided without a browser: one card per request, grouped by project and oldest
// first; the one line an answered request collapses to, the same in every window; the dispatches in
// the order the operator reads them; the model preselected in Settings; and the address of the main
// chat, which is orchestration mode's home.

import { afterEach, describe, expect, it } from "vitest";
import type { Dispatch, MainAsk, Preset } from "../api";
import { setLang } from "../i18n";
import { parse } from "../router";
import { eventTone, parseEvents } from "../turns";
import { answeredLine, dispatchState, goingDispatches, mainPreset } from "./model";

function ask(over: Partial<MainAsk> = {}): MainAsk {
  return {
    id: "ask-1", short_id: "q1abcd", project_id: "p1", origin: "orchestrator", kind: "question", staff_id: null, task_id: null,
    text: "Postgres or SQLite?", detail: { options: ["Postgres", "SQLite"] }, routed_to: "operator", suggestion: "",
    created_at: "2026-09-25T10:00:00Z", resolved_at: null, resolved_by: null, resolution: {}, dispatch_id: "d1",
    project_name: "Bakery", asker: "orchestrator", host: false, ...over,
  };
}

function dispatch(over: Partial<Dispatch> = {}): Dispatch {
  return {
    id: "d1", project_id: "p1", project_name: "Bakery", seq: 1, kind: "work", title: "Menu", text: "Add a menu", status: "open", result: "",
    created_at: "2026-09-25T10:00:00Z", updated_at: "2026-09-25T10:00:00Z", closed_at: null, stalled_at: null, ...over,
  };
}

afterEach(() => setLang("en"));

describe("the questions of the main chat", () => {
  it("says where and how a request was answered, the same wherever it was shown", () => {
    expect(answeredLine(ask({ resolved_at: "x", resolved_by: "operator", resolution: { selected: ["Postgres"], via: "main" } }))).toBe("answered in the main chat: Postgres");
    expect(answeredLine(ask({ resolved_at: "x", resolved_by: "operator", resolution: { text: "SQLite, small data", via: "telegram" } }))).toBe("answered in Telegram: SQLite, small data");
    expect(answeredLine(ask({ kind: "permission", resolved_at: "x", resolved_by: "operator", resolution: { allow: false, via: "push" } }))).toBe("answered from a notification: no");
    expect(answeredLine(ask({ resolved_at: "x", resolved_by: "operator", resolution: { text: "Postgres", via: "dispatcher" } }))).toBe("answered through the main orchestrator: Postgres");
    expect(answeredLine(ask({ resolved_at: "x", resolved_by: "system", resolution: { closed: "dispatch d1 was closed as done" } }))).toBe("withdrawn");
    setLang("ru");
    expect(answeredLine(ask({ resolved_at: "x", resolved_by: "operator", resolution: { selected: ["Postgres"], via: "project" } }))).toBe("ответ в чате проекта: Postgres");
  });
});

describe("the dispatches", () => {
  it("names each state, a quiet open one as stalled", () => {
    expect(dispatchState(dispatch()).tone).toBe("info");
    expect(dispatchState(dispatch({ stalled_at: "2026-09-25T10:40:00Z" }))).toEqual({ word: "stalled", tone: "warn" });
    expect(dispatchState(dispatch({ status: "done" })).tone).toBe("ok");
    expect(dispatchState(dispatch({ status: "blocked" })).tone).toBe("bad");
    expect(dispatchState(dispatch({ status: "cancelled" })).tone).toBe("faint");
  });

  it("draws only the work under way, the longest waiting first", () => {
    // A closed dispatch is already a line of the reports in the chat; a card for it said it twice.
    const order = goingDispatches([
      dispatch({ id: "done-old", status: "done", closed_at: "2026-09-25T09:00:00Z" }),
      dispatch({ id: "open-new", created_at: "2026-09-25T11:00:00Z" }),
      dispatch({ id: "blocked-old", status: "blocked", created_at: "2026-09-25T08:00:00Z" }),
      dispatch({ id: "done-new", status: "done", closed_at: "2026-09-25T12:00:00Z" }),
    ]);
    expect(order.map((d) => d.id)).toEqual(["blocked-old", "open-new"]);
  });
});

describe("the main chat in the app", () => {
  it("is orchestration mode's home, and its old address still leads there", () => {
    const route = parse("/app/orchestration", "");
    expect([route.screen, route.session, route.project, route.detail]).toEqual(["orchestration", null, null, null]);
    const old = parse("/app/main", "");
    expect([old.screen, old.project, old.detail]).toEqual(["orchestration", null, null]);
  });

  it("reads the main orchestrator's wake-ups as a card of reports", () => {
    const batch = parseEvents("[reports · 2 since 14:02]\n- 14:02 Bakery closed dispatch d1abc \"Menu\" as done: six dishes\n- 14:03 dispatch d2xyz of Garden has been quiet for 31 min and nobody there is working — tell the operator; do not prod the project yourself");
    expect(batch?.count).toBe(2);
    expect(batch?.lines.map((l) => l.tone)).toEqual(["ok", "warn"]);
    expect(batch?.lines[1].text.endsWith("nobody there is working")).toBe(true);
    expect(eventTone("Bakery closed dispatch d1 as blocked: needs a password")).toBe("bad");
  });
});

describe("the model it runs", () => {
  const presets = { small: {} as Preset, mid: {} as Preset, big: {} as Preset };
  it("shows the chosen preset, else the host's mid-tier pick, else the first", () => {
    expect(mainPreset(presets, "big", "mid")).toBe("big");
    expect(mainPreset(presets, "", "mid")).toBe("mid");
    expect(mainPreset(presets, "gone", "mid")).toBe("mid");
    expect(mainPreset(presets, undefined, undefined)).toBe("small");
  });
});
