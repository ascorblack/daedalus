import { beforeEach, describe, expect, it } from "vitest";
import { RegistryDeps, TerminalRegistry, WEBGL_MAX } from "./registry";

type Fake = { id: string; log: string[] };

let now = 0;
let timers: { at: number; fn: () => void; id: number }[] = [];
let log: string[] = [];

const deps: RegistryDeps = {
  setTimeout: (fn, ms) => {
    const id = timers.length + 1000 * Math.random();
    timers.push({ at: now + ms, fn, id });
    return id;
  },
  clearTimeout: (h) => {
    timers = timers.filter((t) => t.id !== h);
  },
  now: () => ++now,
};

function advance(ms: number) {
  now += ms;
  const due = timers.filter((t) => t.at <= now);
  timers = timers.filter((t) => t.at > now);
  due.forEach((t) => t.fn());
}

function registry() {
  return new TerminalRegistry<Fake>(
    {
      create: (id) => (log.push(`create ${id}`), { id, log: [] }),
      dispose: (f) => log.push(`dispose ${f.id}`),
      sleep: (f) => log.push(`sleep ${f.id}`),
      wake: (f) => log.push(`wake ${f.id}`),
      webgl: (f, on) => log.push(`webgl ${f.id} ${on ? "on" : "off"}`),
    },
    undefined,
    deps,
  );
}

const webglHolders = (r: TerminalRegistry<Fake>) => r.snapshot().filter((e) => e.webgl).map((e) => e.id);

beforeEach(() => {
  now = 0;
  timers = [];
  log = [];
});

describe("the terminal registry", () => {
  it("hands back the same instance when a terminal moves between views", () => {
    const r = registry();
    const inDock = r.acquire("a");
    r.release("a");
    const fullScreen = r.acquire("a");
    expect(fullScreen).toBe(inDock);
    expect(log.filter((l) => l.startsWith("create"))).toEqual(["create a"]);
  });

  it("closes a detached terminal's connection after 30 seconds, and reattaches on return", () => {
    const r = registry();
    r.acquire("a");
    r.release("a");
    advance(29_000);
    expect(log).not.toContain("sleep a");
    advance(2_000);
    expect(log).toContain("sleep a");
    r.acquire("a");
    expect(log).toContain("wake a");
  });

  it("does not put a terminal to sleep when it came back within the linger", () => {
    const r = registry();
    r.acquire("a");
    r.release("a");
    advance(10_000);
    r.acquire("a");
    advance(60_000);
    expect(log).not.toContain("sleep a");
    expect(log).not.toContain("wake a");
  });

  it("keeps a terminal attached while any view still holds it", () => {
    const r = registry();
    r.acquire("a");
    r.acquire("a");
    r.release("a");
    advance(60_000);
    expect(log).not.toContain("sleep a");
  });

  it("keeps at most eight detached terminals, dropping the one detached longest ago", () => {
    const r = registry();
    for (let i = 0; i < 10; i++) {
      r.acquire(`t${i}`);
      r.release(`t${i}`);
    }
    expect(r.snapshot().map((e) => e.id)).toEqual(["t2", "t3", "t4", "t5", "t6", "t7", "t8", "t9"]);
    expect(log).toContain("dispose t0");
    expect(log).toContain("dispose t1");
  });

  it("never lets more than six terminals hold a WebGL context, preferring the most recently used", () => {
    const r = registry();
    for (let i = 0; i < 9; i++) {
      r.acquire(`t${i}`);
      r.setVisible(`t${i}`, true);
    }
    expect(webglHolders(r).length).toBe(WEBGL_MAX);
    expect(webglHolders(r).sort()).toEqual(["t3", "t4", "t5", "t6", "t7", "t8"]);
    r.touch("t0");
    expect(webglHolders(r)).toContain("t0");
    expect(webglHolders(r).length).toBe(WEBGL_MAX);
    // Across every step the count of contexts held never went past the limit.
    let held = 0;
    let peak = 0;
    for (const line of log) {
      if (line.endsWith(" on")) peak = Math.max(peak, ++held);
      if (line.endsWith(" off")) held--;
    }
    expect(peak).toBeLessThanOrEqual(WEBGL_MAX);
  });

  it("takes WebGL from a terminal that is hidden or detached", () => {
    const r = registry();
    r.acquire("a");
    r.setVisible("a", true);
    r.acquire("b");
    r.setVisible("b", true);
    expect(webglHolders(r).sort()).toEqual(["a", "b"]);
    r.setVisible("a", false);
    expect(webglHolders(r)).toEqual(["b"]);
    r.release("b");
    expect(webglHolders(r)).toEqual([]);
  });

  it("disposes a removed terminal at once", () => {
    const r = registry();
    r.acquire("a");
    r.release("a");
    r.remove("a");
    expect(log).toContain("dispose a");
    advance(60_000);
    expect(log).not.toContain("sleep a");
    expect(r.has("a")).toBe(false);
  });
});
