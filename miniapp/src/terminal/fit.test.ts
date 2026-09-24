import { describe, expect, it } from "vitest";
import { FitContext, nextSize, ResizeScheduler, SchedulerDeps, Size } from "./fit";

const shown: FitContext = { visible: true, focused: true, interacted: false };

describe("nextSize", () => {
  it("never sends from a hidden terminal, whatever it measures", () => {
    for (const proposed of [{ cols: 9, rows: 5 }, { cols: 120, rows: 40 }, undefined]) {
      expect(nextSize(null, proposed, { visible: false, focused: true, interacted: true })).toEqual({ send: false });
    }
  });

  it("never sends from an unfocused window unless the person just used this terminal", () => {
    expect(nextSize(null, { cols: 100, rows: 30 }, { visible: true, focused: false, interacted: false })).toEqual({ send: false });
    expect(nextSize(null, { cols: 100, rows: 30 }, { visible: true, focused: false, interacted: true }).send).toBe(true);
  });

  it("never produces 9×5, or anything under 20×4", () => {
    const decisions = [
      nextSize(null, { cols: 9, rows: 5 }, shown),
      nextSize(null, { cols: 1, rows: 1 }, shown),
      nextSize(null, { cols: 0, rows: 0 }, shown),
      nextSize(null, { cols: -4, rows: 2.5 }, shown),
    ];
    for (const d of decisions) {
      expect(d.send).toBe(true);
      if (d.send) {
        expect(d.cols).toBeGreaterThanOrEqual(20);
        expect(d.rows).toBeGreaterThanOrEqual(4);
      }
    }
    expect(nextSize(null, { cols: 9, rows: 5 }, shown)).toEqual({ send: true, cols: 20, rows: 5, delay: 100 });
  });

  it("caps the size at what the daemon accepts", () => {
    expect(nextSize(null, { cols: 900, rows: 900 }, shown)).toEqual({ send: true, cols: 500, rows: 300, delay: 100 });
  });

  it("sends nothing for an undefined, non-numeric or unchanged proposal", () => {
    expect(nextSize(null, undefined, shown)).toEqual({ send: false });
    expect(nextSize(null, { cols: NaN, rows: 30 }, shown)).toEqual({ send: false });
    expect(nextSize(null, { cols: 80 }, shown)).toEqual({ send: false });
    expect(nextSize({ cols: 80, rows: 24 }, { cols: 80.7, rows: 24.2 }, shown)).toEqual({ send: false });
    // Two proposals that clamp to the same size are the same size.
    expect(nextSize({ cols: 20, rows: 4 }, { cols: 3, rows: 1 }, shown)).toEqual({ send: false });
  });

  it("debounces a change of columns and sends a change of rows alone at once", () => {
    expect(nextSize({ cols: 80, rows: 24 }, { cols: 81, rows: 24 }, shown)).toEqual({ send: true, cols: 81, rows: 24, delay: 100 });
    expect(nextSize({ cols: 80, rows: 24 }, { cols: 80, rows: 30 }, shown)).toEqual({ send: true, cols: 80, rows: 30, delay: 0 });
  });
});

function fakeTimers() {
  let now = 0;
  const timers: { at: number; fn: () => void; id: number }[] = [];
  let id = 0;
  const deps: SchedulerDeps = {
    setTimeout: (fn, ms) => {
      timers.push({ at: now + ms, fn, id: ++id });
      return id;
    },
    clearTimeout: (h) => {
      const i = timers.findIndex((t) => t.id === h);
      if (i >= 0) timers.splice(i, 1);
    },
    frame: (fn) => deps.setTimeout(fn, 16),
    cancelFrame: (h) => deps.clearTimeout(h),
  };
  const advance = (ms: number) => {
    now += ms;
    for (const t of timers.filter((t) => t.at <= now).sort((a, b) => a.at - b.at)) {
      timers.splice(timers.indexOf(t), 1);
      t.fn();
    }
  };
  return { deps, advance };
}

describe("ResizeScheduler", () => {
  it("sends only the last of a burst of column changes", () => {
    const { deps, advance } = fakeTimers();
    const sent: Size[] = [];
    const scheduler = new ResizeScheduler((s) => (sent.push(s), true), deps);
    for (let cols = 60; cols < 90; cols++) {
      scheduler.propose({ cols, rows: 24 }, shown);
      advance(16);
    }
    expect(sent).toEqual([]);
    advance(100);
    expect(sent).toEqual([{ cols: 89, rows: 24 }]);
  });

  it("drops a pending size when the terminal is hidden before it goes out", () => {
    const { deps, advance } = fakeTimers();
    const sent: Size[] = [];
    const scheduler = new ResizeScheduler((s) => (sent.push(s), true), deps);
    scheduler.propose({ cols: 100, rows: 30 }, shown);
    scheduler.propose({ cols: 9, rows: 5 }, { visible: false, focused: true, interacted: false });
    advance(500);
    expect(sent).toEqual([]);
  });

  it("tries again when a size could not leave, and sends it after a reconnect", () => {
    const { deps, advance } = fakeTimers();
    let open = false;
    const sent: Size[] = [];
    const scheduler = new ResizeScheduler((s) => (open ? (sent.push(s), true) : false), deps);
    scheduler.propose({ cols: 100, rows: 30 }, shown);
    advance(200);
    expect(scheduler.last).toBeNull();
    open = true;
    scheduler.propose({ cols: 100, rows: 30 }, shown);
    advance(200);
    expect(sent).toEqual([{ cols: 100, rows: 30 }]);
    scheduler.forget();
    scheduler.propose({ cols: 100, rows: 30 }, shown);
    advance(200);
    expect(sent.length).toBe(2);
  });
});
