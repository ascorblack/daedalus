// The Components page's data layer. The rules it has to keep are the ones that took a screen down
// on the speech picker once already: an answer that is missing a field must not erase the field, and
// a bar that is moving must survive the list being reloaded under it.

import { describe, expect, it } from "vitest";

// The module's API client reads the address bar as it loads, and none of what is tested here touches
// it; the little of a window it needs is given first, as the speech picker's test does.
(globalThis as unknown as { window: unknown }).window = { location: { search: "", pathname: "/", hash: "" }, history: { replaceState: () => undefined } };
const { EMPTY_COMPONENTS, applyFrame, installFrame, mergeComponents, restartPending, settled } = await import("./componentsview");
type ComponentsView = typeof EMPTY_COMPONENTS;
type ComponentEntry = ComponentsView["components"][number];

function entry(id: string, over: Partial<ComponentEntry> = {}): ComponentEntry {
  return {
    id,
    state: "missing",
    detail: "not here",
    installable: true,
    how: "extra",
    fix: "",
    download_bytes: 15_728_640,
    disk_bytes: 0,
    requires_restart: false,
    installed_count: 0,
    total_count: 0,
    skills: [],
    enables: ["stt"],
    progress: null,
    ...over,
  };
}

function view(over: Partial<ComponentsView> = {}): ComponentsView {
  return { ...EMPTY_COMPONENTS, mode: "native", components: [entry("speech"), entry("node", { how: "launcher" })], ...over };
}

describe("merging an answer into the view", () => {
  it("keeps every field the answer did not mention", () => {
    const merged = mergeComponents(view({ launcher: true, disk_bytes: 42 }), { components: [entry("speech")] });
    expect(merged.launcher).toBe(true);
    expect(merged.disk_bytes).toBe(42);
    expect(merged.missing).toEqual([]);
  });

  it("is a whole view even when nothing has answered yet", () => {
    const merged = mergeComponents(null, undefined);
    expect(merged.components).toEqual([]);
    expect(merged.missing).toEqual([]);
    expect(merged.mode).toBe("docker");
  });

  it("keeps a bar that is moving when the list is reloaded under it", () => {
    const running = { id: "speech", state: "running", step: "uv sync", error: "", restart_required: false };
    const before = view({ components: [entry("speech", { progress: running })] });
    const merged = mergeComponents(before, { components: [entry("speech")] });
    expect(merged.components[0].progress).toEqual(running);
  });
});

describe("a frame off the install stream", () => {
  it("is read, or dropped when it is not one", () => {
    expect(installFrame({ id: "speech", state: "running", step: "x" })).toEqual({
      id: "speech",
      state: "running",
      step: "x",
      error: "",
      restart_required: false,
    });
    expect(installFrame({ state: "running" })).toBeNull();
    expect(installFrame("nonsense")).toBeNull();
    expect(installFrame(null)).toBeNull();
  });

  it("moves one card and only that card", () => {
    const next = applyFrame(view(), { id: "speech", state: "running", step: "uv sync", error: "", restart_required: false });
    expect(next.components[0].state).toBe("installing");
    expect(next.components[0].progress?.step).toBe("uv sync");
    expect(next.components[1].state).toBe("missing");
    expect(next.busy).toBe("speech");
  });

  it("gives the lane back when the install it was holding finishes", () => {
    const running = applyFrame(view(), { id: "speech", state: "running", step: "", error: "", restart_required: false });
    const done = applyFrame(running, { id: "speech", state: "installed", step: "done", error: "", restart_required: false });
    expect(done.busy).toBe("");
  });

  it("knows which states end an install", () => {
    const frame = (state: string) => ({ id: "speech", state, step: "", error: "", restart_required: false });
    expect(["installed", "failed", "cancelled"].map((s) => settled(frame(s)))).toEqual([true, true, true]);
    expect(["queued", "running"].map((s) => settled(frame(s)))).toEqual([false, false]);
  });
});

describe("the restart an install can ask for", () => {
  it("is pending only after something that needs one has actually gone in", () => {
    const done = (restart: boolean) => ({ id: "node", state: "installed", step: "", error: "", restart_required: restart });
    expect(restartPending(view())).toBe(false);
    expect(restartPending(applyFrame(view(), done(false)))).toBe(false);
    expect(restartPending(applyFrame(view(), done(true)))).toBe(true);
  });

  it("is not pending for one that only failed", () => {
    const failed = { id: "node", state: "failed", step: "", error: "no launcher is running", restart_required: true };
    expect(restartPending(applyFrame(view(), failed))).toBe(false);
  });
});
