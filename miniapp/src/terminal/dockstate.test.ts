// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";
import { clampHeight, closeTab, DOCK_DEFAULT, DOCK_MIN, DockState, EMPTY, loadDock, loadSandboxChoice, openTab, prune, replaceTab, sandboxOffer, sandboxToggle, saveDock, saveSandboxChoice, setSplit, splitCandidate, toggleDock } from "./dockstate";

const state = (over: Partial<DockState> = {}): DockState => ({ ...EMPTY, open: true, tabs: ["a", "b", "c"], active: "a", ...over });

beforeEach(() => localStorage.clear());

describe("the dock's memory", () => {
  it("keeps one state per session and reads back what it wrote", () => {
    saveDock("s1", state({ height: 300, split: "b" }));
    expect(loadDock("s1")).toEqual(state({ height: 300, split: "b" }));
    expect(loadDock("s2")).toEqual(EMPTY);
  });

  it("repairs what it cannot trust: unknown panes, duplicate tabs, a height below the floor", () => {
    localStorage.setItem("daedalus.dock.s1", JSON.stringify({ open: true, height: 12, tabs: ["a", "a", 3, "b"], active: "z", split: "a" }));
    expect(loadDock("s1")).toEqual({ open: true, height: DOCK_MIN, tabs: ["a", "b"], active: "a", split: null });
    localStorage.setItem("daedalus.dock.s1", "{not json");
    expect(loadDock("s1")).toEqual(EMPTY);
  });

  it("forgets a session whose dock is closed, empty and at the default height", () => {
    saveDock("s1", state());
    saveDock("s1", { ...EMPTY });
    expect(localStorage.getItem("daedalus.dock.s1")).toBeNull();
    expect(EMPTY.height).toBe(DOCK_DEFAULT);
  });
});

describe("tabs", () => {
  it("opens a terminal once and shows it", () => {
    expect(openTab(state(), "d")).toMatchObject({ tabs: ["a", "b", "c", "d"], active: "d" });
    expect(openTab(state(), "b")).toMatchObject({ tabs: ["a", "b", "c"], active: "b" });
  });

  it("closing a tab detaches it and hands its pane to the neighbour", () => {
    expect(closeTab(state({ active: "b" }), "b")).toMatchObject({ tabs: ["a", "c"], active: "c" });
    expect(closeTab(state({ active: "c" }), "c")).toMatchObject({ tabs: ["a", "b"], active: "b" });
    expect(closeTab(state({ tabs: ["a"], active: "a" }), "a")).toMatchObject({ tabs: [], active: null });
  });

  it("closing the left pane of a split moves the right one over", () => {
    expect(closeTab(state({ split: "c" }), "a")).toMatchObject({ tabs: ["b", "c"], active: "c", split: null });
    expect(closeTab(state({ split: "c" }), "c")).toMatchObject({ active: "a", split: null });
  });

  it("drops the tabs of terminals the host no longer lists, and keeps the exited ones it does", () => {
    expect(prune(state({ split: "b" }), ["a", "c"])).toMatchObject({ tabs: ["a", "c"], active: "a", split: null });
    expect(prune(state(), [])).toMatchObject({ tabs: [], active: null });
  });

  it("follows a restarted terminal to its new id", () => {
    expect(replaceTab(state({ split: "b" }), "b", "b2")).toMatchObject({ tabs: ["a", "b2", "c"], split: "b2" });
    expect(replaceTab(state(), "a", "a2")).toMatchObject({ active: "a2" });
  });
});

describe("the split", () => {
  it("puts the next tab beside the active one, and never the active one itself", () => {
    expect(splitCandidate(state({ active: "b" }))).toBe("c");
    expect(splitCandidate(state({ active: "c" }))).toBe("a");
    expect(splitCandidate(state({ tabs: ["a"], active: "a" }))).toBeNull();
    expect(setSplit(state(), "a")).toMatchObject({ split: null });
    expect(setSplit(state(), "z")).toMatchObject({ tabs: ["a", "b", "c", "z"], split: "z" });
  });
});

describe("toggling", () => {
  it("creates a terminal only when the session has none", () => {
    expect(toggleDock({ ...EMPTY }, [])).toEqual({ state: { ...EMPTY, open: true }, create: true });
  });

  it("brings in every running terminal when there are no tabs", () => {
    expect(toggleDock({ ...EMPTY }, ["x", "y"]).state).toMatchObject({ open: true, tabs: ["x", "y"], active: "x" });
  });

  it("keeps the tabs it had, and closes an open dock", () => {
    expect(toggleDock(state({ open: false }), ["x"]).state).toMatchObject({ open: true, tabs: ["a", "b", "c"] });
    expect(toggleDock(state(), ["x"])).toMatchObject({ state: { open: false }, create: false });
  });
});

describe("the height", () => {
  it("stays between the floor and four fifths of the column", () => {
    expect(clampHeight(50, 800)).toBe(DOCK_MIN);
    expect(clampHeight(900, 800)).toBe(640);
    expect(clampHeight(300, 800)).toBe(300);
    expect(clampHeight(300, 100)).toBe(DOCK_MIN);
  });
});

describe("the sandbox choice", () => {
  it("is remembered for the device and off until chosen", () => {
    expect(loadSandboxChoice()).toBe(false);
    saveSandboxChoice(true);
    expect(loadSandboxChoice()).toBe(true);
    saveSandboxChoice(false);
    expect(loadSandboxChoice()).toBe(false);
  });

  it("is offered where an environment can sandbox, and otherwise says why", () => {
    const up = { available: true, sandbox: "ok" };
    const refused = { available: true, sandbox: "bwrap cannot create namespaces here: no" };
    const down = { available: false, sandbox: "" };
    expect(sandboxOffer(up)).toEqual({ ok: true, reason: "" });
    expect(sandboxOffer(refused)).toEqual({ ok: false, reason: refused.sandbox });
    expect(sandboxOffer(down)).toEqual({ ok: false, reason: "" });
    expect(sandboxOffer(undefined)).toEqual({ ok: false, reason: "" });
    expect(sandboxToggle([refused, up])).toEqual({ ok: true, reason: "" });
    expect(sandboxToggle([down, refused])).toEqual({ ok: false, reason: refused.sandbox });
    expect(sandboxToggle([])).toEqual({ ok: false, reason: "" });
  });
});
