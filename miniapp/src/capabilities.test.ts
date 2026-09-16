import { describe, expect, it } from "vitest";
import { screenTag, visibleScreens } from "./capabilities";
import type { Screen } from "./router";

// Written out rather than imported: router.ts reads window.history the moment it loads, and these
// are pure functions over a list of names.
const SCREENS: Screen[] = ["agents", "voice", "inbox", "board", "changes", "schedules", "services", "memory", "usage", "health", "settings"];
const BETA: Screen[] = ["voice"];

describe("visibleScreens", () => {
  it("drops Changes where the installation cannot change its own code", () => {
    expect(visibleScreens(SCREENS, "off")).not.toContain("changes");
    expect(visibleScreens(SCREENS, "off").length).toBe(SCREENS.length - 1);
  });
  it("keeps every destination in local and server mode", () => {
    expect(visibleScreens(SCREENS, "local")).toEqual(SCREENS);
    expect(visibleScreens(SCREENS, "server")).toEqual(SCREENS);
  });
  it("leaves a list that never had Changes alone", () => {
    const some: Screen[] = ["agents", "inbox", "settings"];
    expect(visibleScreens(some, "off")).toEqual(some);
  });
});

describe("screenTag", () => {
  it("marks Changes as local, because a change there stays on this machine", () => {
    expect(screenTag("changes", "local", BETA)).toBe("local");
    expect(screenTag("changes", "server", BETA)).toBe("");
  });
  it("still marks what is in beta", () => {
    expect(screenTag("voice", "server", BETA)).toBe("beta");
    expect(screenTag("inbox", "server", BETA)).toBe("");
  });
});
