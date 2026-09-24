// @vitest-environment jsdom
// Whether this device can have push, for every shape of browser that asks, and the service worker's
// push handlers run in a fake worker scope: what a push shows, what a button does, what a tap opens.

/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import { deviceName, keyBytes, pushState, type PushEnv } from "./push";
import workerSource from "../public/sw.js?raw";

const CHROME: PushEnv = { telegram: false, secure: true, serviceWorker: true, pushManager: true, notification: true, ios: false, standalone: false, permission: "default" };
const none = { subscribed: false, hostReason: "" };

describe("push state", () => {
  it("inside Telegram the bot is the push", () => {
    expect(pushState({ ...CHROME, telegram: true }, none)).toBe("telegram");
    expect(pushState({ ...CHROME, telegram: true, ios: true }, none)).toBe("telegram");
  });

  it("an iPhone in Safari is told to add the site to the Home Screen, and gets push once it has", () => {
    const safari: PushEnv = { ...CHROME, ios: true, pushManager: false };
    expect(pushState(safari, none)).toBe("needs-home-screen");
    expect(pushState({ ...safari, standalone: true, pushManager: true }, none)).toBe("off");
    expect(pushState({ ...safari, standalone: true, pushManager: true, permission: "granted" }, { subscribed: true, hostReason: "" })).toBe("on");
  });

  it("plain http, or a browser without the pieces, cannot have it", () => {
    expect(pushState({ ...CHROME, secure: false }, none)).toBe("unsupported");
    expect(pushState({ ...CHROME, pushManager: false }, none)).toBe("unsupported");
    expect(pushState({ ...CHROME, serviceWorker: false }, none)).toBe("unsupported");
    expect(pushState({ ...CHROME, notification: false }, none)).toBe("unsupported");
  });

  it("a host without a public https address, a refused permission, off and on", () => {
    expect(pushState(CHROME, { subscribed: false, hostReason: "no_https_url" })).toBe("unavailable");
    expect(pushState({ ...CHROME, permission: "denied" }, none)).toBe("denied");
    expect(pushState(CHROME, none)).toBe("off");
    expect(pushState({ ...CHROME, permission: "granted" }, none)).toBe("off");
    expect(pushState({ ...CHROME, permission: "granted" }, { subscribed: true, hostReason: "" })).toBe("on");
    // Subscribed once, but the permission was taken back in the browser's settings since.
    expect(pushState({ ...CHROME, permission: "default" }, { subscribed: true, hostReason: "" })).toBe("off");
  });

  it("names a device by its browser and system", () => {
    expect(deviceName("Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Mobile Safari/537.36")).toBe("Chrome · Android");
    expect(deviceName("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")).toBe("Safari · iPhone");
    expect(deviceName("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36 Edg/140.0")).toBe("Edge · Windows");
    expect(deviceName("Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0")).toBe("Firefox · Linux");
    expect(deviceName("")).toBe("Browser");
  });

  it("decodes the host's key into the 65 bytes a subscription wants", () => {
    const key = "BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8";
    const bytes = keyBytes(key);
    expect(bytes.length).toBe(65);
    expect(bytes[0]).toBe(4);
  });
});

// -- the service worker ------------------------------------------------------------------------

type Listener = (event: Record<string, unknown>) => void;

interface Shown {
  title: string;
  options: Record<string, any>;
  closed: boolean;
}

function worker(opts: { windows?: { url: string }[]; fetchStatus?: number; fetchFails?: boolean } = {}) {
  const listeners: Record<string, Listener> = {};
  const shown: Shown[] = [];
  const fetched: { url: string; init: RequestInit }[] = [];
  const opened: string[] = [];
  const posted: unknown[] = [];
  const focused: string[] = [];
  const badges: (number | "clear")[] = [];
  const windows = (opts.windows ?? []).map((w) => ({
    url: w.url,
    postMessage: (m: unknown) => posted.push(m),
    focus: async () => {
      focused.push(w.url);
    },
  }));
  const scope = {
    addEventListener: (type: string, listener: Listener) => {
      listeners[type] = listener;
    },
    location: { origin: "https://daedalus.example.com" },
    navigator: {
      setAppBadge: async (n: number) => {
        badges.push(n);
      },
      clearAppBadge: async () => {
        badges.push("clear");
      },
    },
    registration: {
      showNotification: async (title: string, options: Record<string, any>) => {
        shown.push({ title, options, closed: false });
      },
      getNotifications: async ({ tag }: { tag: string }) =>
        shown.filter((n) => n.options.tag === tag && !n.closed).map((n) => ({ close: () => (n.closed = true) })),
      pushManager: { getSubscription: async () => ({ endpoint: "https://push.example.com/me" }) },
    },
    clients: {
      matchAll: async () => windows,
      openWindow: async (url: string) => {
        opened.push(url);
      },
    },
  };
  const fetch = async (url: string, init: RequestInit) => {
    fetched.push({ url, init });
    if (opts.fetchFails) throw new TypeError("offline");
    const status = opts.fetchStatus ?? 200;
    return { ok: status >= 200 && status < 300, status, json: async () => ({}) };
  };
  // The worker's own file, run as the browser would run it, with this scope as its `self`.
  new Function("self", "caches", "fetch", workerSource)(scope, {}, fetch);

  async function fire(type: string, event: Record<string, unknown>) {
    const waits: Promise<unknown>[] = [];
    listeners[type]({ ...event, waitUntil: (p: Promise<unknown>) => waits.push(p) });
    await Promise.all(waits);
  }
  const push = (payload: unknown) => fire("push", { data: { json: () => payload } });
  const click = (index: number, action = "") => {
    const n = shown[index];
    return fire("notificationclick", { action, notification: { data: n.options.data, close: () => (n.closed = true) } });
  };
  return { shown, fetched, opened, posted, focused, badges, push, click };
}

const PERMISSION = {
  v: 1, kind: "show", id: 812, title: "Naya is waiting for permission", body: "npm install grammy", link: "/app/agents/a1b2c3d4e5f6",
  tag: "n812", level: "urgent", at: "2026-09-24T12:00:00Z", actions: [{ id: "allow", label: "Allow" }, { id: "deny", label: "Deny" }], token: "mac.1790000000", unseen: 2,
};

describe("the service worker", () => {
  it("shows a push with its buttons, keeps an urgent one on screen and sets the badge", async () => {
    const w = worker();
    await w.push(PERMISSION);
    const [n] = w.shown;
    expect(n.title).toBe("Naya is waiting for permission");
    expect(n.options).toMatchObject({ body: "npm install grammy", tag: "n812", renotify: true, requireInteraction: true, data: { id: 812, link: "/app/agents/a1b2c3d4e5f6", token: "mac.1790000000" } });
    expect(n.options.actions).toEqual([{ action: "allow", title: "Allow" }, { action: "deny", title: "Deny" }]);
    expect(w.badges).toEqual([2]);
  });

  it("shows something even for a message it cannot read", async () => {
    const w = worker();
    await w.push({});
    expect(w.shown.map((n) => n.title)).toEqual(["Daedalus"]);
    expect(w.shown[0].options.actions).toBeUndefined();
  });

  it("a button answers from the lock screen with the token and opens nothing", async () => {
    const w = worker();
    await w.push(PERMISSION);
    await w.click(0, "allow");
    expect(w.fetched).toHaveLength(1);
    expect(w.fetched[0].url).toBe("/api/notifications/812/act");
    expect(w.fetched[0].init.credentials).toBe("include");
    expect(JSON.parse(String(w.fetched[0].init.body))).toEqual({ action: "allow", token: "mac.1790000000", via: "push", endpoint: "https://push.example.com/me" });
    expect(w.opened).toEqual([]);
    expect(w.shown[0].closed).toBe(true);
  });

  it("an answer already given elsewhere is not an error; a refused one opens the app at the request", async () => {
    const answered = worker({ fetchStatus: 409 });
    await answered.push(PERMISSION);
    await answered.click(0, "deny");
    expect(answered.opened).toEqual([]);
    const refused = worker({ fetchStatus: 400 });
    await refused.push(PERMISSION);
    await refused.click(0, "allow");
    expect(refused.opened).toEqual(["/app/agents/a1b2c3d4e5f6"]);
    const offline = worker({ fetchFails: true });
    await offline.push(PERMISSION);
    await offline.click(0, "allow");
    expect(offline.opened).toEqual(["/app/agents/a1b2c3d4e5f6"]);
  });

  it("a tap on the body moves an open app to the item, or opens one", async () => {
    const open = worker({ windows: [{ url: "https://daedalus.example.com/elsewhere" }, { url: "https://daedalus.example.com/app/agents" }] });
    await open.push(PERMISSION);
    await open.click(0);
    expect(open.fetched).toEqual([]);
    expect(open.focused).toEqual(["https://daedalus.example.com/app/agents"]);
    expect(open.posted).toEqual([{ type: "daedalus.open", link: "/app/agents/a1b2c3d4e5f6" }]);
    const closed = worker();
    await closed.push({ ...PERMISSION, link: "https://elsewhere.example/phish" });
    await closed.click(0);
    expect(closed.opened).toEqual(["/app/"]); // only a path inside the app is ever opened
  });

  it("a withdrawal closes the answered notification and leaves the others", async () => {
    const w = worker();
    await w.push(PERMISSION);
    await w.push({ ...PERMISSION, id: 813, tag: "n813" });
    await w.push({ v: 1, kind: "withdraw", id: 812, tag: "n812", unseen: 1 });
    expect(w.shown.map((n) => [n.options.tag, n.closed])).toEqual([["n812", true], ["n813", false]]);
    expect(w.badges).toEqual([2, 2, 1]);
    await w.push({ v: 1, kind: "withdraw", id: 900, tag: "n900", unseen: 0 });
    expect(w.shown).toHaveLength(2);
    expect(w.badges.at(-1)).toBe("clear");
  });
});
