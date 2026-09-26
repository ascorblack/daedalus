// The load bar's arithmetic, checked against figures the host's own projection gives for the same
// machine (daedalus/load.py, `project`): the page computes it while the cap is typed, and
// the two must never tell the operator different things.

import { describe, expect, it } from "vitest";
import type { BrowserLoad, TerminalLoad } from "./api";
import { level, loadFigures, overEstimate, roundBytes, workloadFigures } from "./machineload";

const GB = 1 << 30;
const MB = 1 << 20;

/** A 64 GB, 16-CPU machine with 24 GB in use and three sessions running, each new one about 700 MB. */
function machine(over: Partial<TerminalLoad> = {}): TerminalLoad {
  return {
    cap: 20,
    running: 3,
    queued: [],
    used: { rss_bytes: 3 * GB + 40 * MB, daemon_rss_bytes: 40 * MB, cpu_percent: 5, cpus: 16, mem_total_bytes: 64 * GB, mem_available_bytes: 40 * GB, machine_cpu_percent: 12 },
    profiles: {},
    likely: { rss_bytes: 700 * MB, cpu_percent: 4, samples: 10, basis: "running" },
    projection: { cap: 20, sessions: 20, terminals_rss_bytes: 0, machine_used_bytes: 0, mem_total_bytes: 64 * GB, mem_percent: 0, cpu_percent: 0, level: "ok", cpu_level: "ok" },
    envs: [],
    thresholds: { warn: 70, bad: 90 },
    ...over,
  };
}

describe("the projection at a cap", () => {
  it("is what the machine uses now plus the sessions still to come at the likely cost", () => {
    const f = loadFigures(machine());
    // The host, for the same machine: terminals 15741222912, machine 38247858176, 55.7 %, CPU 16.2 %.
    expect(f.extra).toBe(17);
    expect(f.terminalsAtCap).toBe(15741222912);
    expect(f.machineAtCap).toBe(38247858176);
    expect(f.memPercentAtCap).toBeCloseTo(55.7, 1);
    expect(f.cpuAtCap).toBeCloseTo(16.25, 2);
    expect(f.level).toBe("ok");
    expect(f.memPercentNow).toBeCloseTo(37.5, 1);
  });

  it("follows a cap that is being typed rather than the saved one", () => {
    const f = loadFigures(machine(), 60);
    // The host at 60: 67607986176 bytes, 98.4 %, "bad".
    expect(f.machineAtCap).toBe(67607986176);
    expect(f.memPercentAtCap).toBeCloseTo(98.4, 1);
    expect(f.level).toBe("bad");
    expect(overEstimate(f)).toBe(true);
  });

  it("adds nothing for a cap at or below what already runs", () => {
    const f = loadFigures(machine(), 2);
    expect(f.extra).toBe(0);
    expect(f.machineAtCap).toBe(f.machineNow);
    expect(f.terminalsAtCap).toBe(f.terminalsNow);
  });

  it("says how many sessions fit under the bad line", () => {
    // 90 % of 64 GB is 57.6 GB; 24 GB are in use; 33.6 GB / 700 MB = 49 more, so 52 in all.
    expect(loadFigures(machine()).supported).toBe(52);
    // A machine already past the line supports only what runs.
    expect(loadFigures(machine({ used: { ...machine().used, mem_available_bytes: 2 * GB } })).supported).toBe(3);
  });

  it("warns for the processors on their own", () => {
    const busy = machine({ used: { ...machine().used, machine_cpu_percent: 88 } });
    const f = loadFigures(busy, 20);
    expect(f.level).toBe("ok");
    expect(f.cpuLevel).toBe("bad");
    expect(overEstimate(f)).toBe(true);
  });

  it("claims nothing without the machine's figures", () => {
    const f = loadFigures(machine({ used: { ...machine().used, mem_total_bytes: 0, mem_available_bytes: 0 } }));
    expect(f.known).toBe(false);
    expect(f.supported).toBeNull();
    expect(overEstimate(f)).toBe(false);
  });
});

describe("the colours", () => {
  it("turn warn above 70 % and bad above 90 %", () => {
    expect([level(70), level(70.1), level(90), level(90.1)]).toEqual(["ok", "warn", "warn", "bad"]);
  });
});

describe("sizes in words", () => {
  it("rounds to what one compares machines by", () => {
    expect(roundBytes(62 * GB + 300 * MB)).toEqual({ value: "62", unit: "gb" });
    expect(roundBytes(1.46 * GB)).toEqual({ value: "1.5", unit: "gb" });
    expect(roundBytes(820 * MB)).toEqual({ value: "820", unit: "mb" });
  });
});

/** The same machine with one browser running, each new one about 260 MB. */
function browsers(over: Partial<BrowserLoad> = {}): BrowserLoad {
  const base = machine();
  return {
    cap: 2,
    running: 1,
    queued: [],
    used: { ...base.used, rss_bytes: 300 * MB, daemon_rss_bytes: 10 * MB },
    likely: { rss_bytes: 260 * MB, cpu_percent: 15, samples: 10, basis: "running" },
    projection: base.projection,
    envs: [],
    thresholds: base.thresholds,
    ...over,
  };
}

describe("terminals and browsers on one track", () => {
  it("fills both caps at once, as the host's project_workloads does", () => {
    const f = workloadFigures(machine(), browsers());
    // The host, for the same machine (test_browser_service.py): 38520487936 bytes, 56.1 %, CPU 17.2 %.
    expect(f.machineAtCap).toBe(38520487936);
    expect(f.memPercentAtCap).toBeCloseTo(56.05, 1);
    expect(f.cpuAtCap).toBeCloseTo(17.19, 1);
    expect(f.terminals?.atCap).toBe(3 * GB + 40 * MB + 17 * 700 * MB);
    expect(f.browsers?.atCap).toBe(560 * MB);
    expect(f.level).toBe("ok");
  });

  it("judges a browser cap together with the terminals'", () => {
    // Each alone would fit; together they pass the bad line (the host: "bad").
    const f = workloadFigures(machine({ used: { ...machine().used, rss_bytes: 3 * GB } }), browsers(), { terminals: 45, browsers: 32 });
    expect(loadFigures(machine(), 45).level).not.toBe("bad");
    expect(f.level).toBe("bad");
  });

  it("works with browsers alone", () => {
    const f = workloadFigures(null, browsers(), { browsers: 4 });
    expect(f.terminals).toBeNull();
    expect(f.machineAtCap).toBe(24 * GB + 3 * 260 * MB);
    expect(f.known).toBe(true);
  });
});
