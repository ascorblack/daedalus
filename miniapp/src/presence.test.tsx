// @vitest-environment jsdom
// What a window tells the host about itself, read back without a host.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LIMITS, clientId, clientKind, currentBody, mergeScopes, startPresence, usePresenceScope, type PresenceScope } from "./presence";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function Shows(props: PresenceScope) {
  usePresenceScope(props);
  return null;
}

describe("the report", () => {
  it("names the kind of window from what the page can see", () => {
    expect(clientKind({ initData: "query_id=1" })).toBe("telegram");
    expect(clientKind({ desktopWindow: true, standalone: true })).toBe("window");
    expect(clientKind({ standalone: true })).toBe("pwa");
    expect(clientKind({})).toBe("browser");
  });

  it("merges declarations without repeats and within what the host accepts", () => {
    const many = Array.from({ length: 6 }, (_, i) => ({ session: `s${i}` }));
    const merged = mergeScopes([{ session: "s0" }, ...many, { terminal: "t1" }, { terminal: "t1", project: "p1" }]);
    expect(merged.sessions).toEqual(["s0", "s1", "s2", "s3"]);
    expect(merged.sessions.length).toBe(LIMITS.sessions);
    expect(merged.terminals).toEqual(["t1"]);
    expect(merged.projects).toEqual(["p1"]);
  });

  it("keeps one id for the tab", () => {
    const id = clientId();
    expect(id).toMatch(/^[A-Za-z0-9_-]{1,64}$/);
    expect(clientId()).toBe(id);
  });
});

describe("the reporter", () => {
  let root: Root;
  let host: HTMLDivElement;
  let posted: Record<string, unknown>[];
  let stop: () => void;

  beforeEach(() => {
    posted = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_path: string, init: RequestInit) => {
        posted.push(JSON.parse(String(init.body)));
        return new Response(null, { status: 204 });
      }),
    );
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    stop = startPresence();
  });

  afterEach(() => {
    stop();
    act(() => root.unmount());
    host.remove();
    vi.unstubAllGlobals();
  });

  it("reports what the mounted screens show, and again when that changes", async () => {
    await act(async () => root.render(<><Shows session="left" /><Shows session="right" /></>));
    expect(posted.at(-1)).toMatchObject({ sessions: ["left", "right"], terminals: [], projects: [] });
    expect(posted.at(-1)).toHaveProperty("tz");
    await act(async () => root.render(<Shows session="left" />));
    expect(posted.at(-1)).toMatchObject({ sessions: ["left"] });
    expect(currentBody().sessions).toEqual(["left"]);
  });

  it("says it is hidden when the page is, and goes away on pagehide with keepalive", async () => {
    await act(async () => root.render(<Shows session="s1" />));
    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(posted.at(-1)).toMatchObject({ visible: false, sessions: ["s1"] });
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    window.dispatchEvent(new Event("pagehide"));
    const last = (fetch as unknown as { mock: { calls: [string, RequestInit][] } }).mock.calls.at(-1)!;
    expect(last[0]).toBe("/api/presence");
    expect(last[1].keepalive).toBe(true);
    expect(posted.at(-1)).toMatchObject({ visible: false, focused: false });
  });
});
