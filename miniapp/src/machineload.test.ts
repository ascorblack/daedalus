import { describe, expect, it } from "vitest";
import type { TerminalLoad } from "./api";
import { level, loadFigures, overEstimate, roundBytes } from "./machineload";

const GB = 1 << 30;
const MB = 1 << 20;

/** A 62 GB machine with 20 GB in use, 6 terminals running at 1.4 GB together (the daemon's 60 MB in it). */
function load(fields: Partial<TerminalLoad> = {}, likelyRss = 500 * MB): TerminalLoad {
  return {
    cap: 20,
    running: 6,
    queued: [],
    used: { rss_bytes: 1.4 * GB, daemon_rss_bytes: 60 * MB, cpu_percent: 12, cpus: 8, mem_total_bytes: 62 * GB, mem_available_bytes: 42 * GB, machine_cpu_percent: 20 },
    profiles: {},
    likely: { rss_bytes: likelyRss, cpu_percent: 4, samples: 12, basis: "running" },
    projection: { cap: 20, sessions: 20, terminals_rss_bytes: 0, machine_used_bytes: 0, mem_total_bytes: 62 * GB, mem_percent: 0, cpu_percent: 0, level: "ok", cpu_level: "ok" },
    envs: [],
    thresholds: { warn: 70, bad: 90 },
    ...fields,
  };
}

describe("the load bar's arithmetic", () => {
  it("projects the cap as the host does: now plus the missing terminals at the likely cost", () => {
    const f = loadFigures(load());
    expect(f.extra).toBe(14);
    expect(f.terminalsNow).toBe(1.4 * GB); // the daemon's own memory is inside the host's figure
    expect(f.terminalsAtCap).toBe(1.4 * GB + 14 * 500 * MB);
    expect(f.machineNow).toBe(20 * GB);
    expect(f.machineAtCap).toBe(20 * GB + 14 * 500 * MB);
    expect(Math.round(f.memPercentAtCap)).toBe(43);
    expect(f.cpuAtCap).toBeCloseTo(20 + (14 * 4) / 8);
    expect(f.level).toBe("ok");
  });

  it("follows a cap being typed without asking the host again", () => {
    const f = loadFigures(load(), 60);
    expect(f.extra).toBe(54);
    expect(f.level).toBe("warn"); // (20 + 54 × 0.49) / 62 = 75 %
    expect(loadFigures(load(), 2).extra).toBe(0); // a cap below what runs adds nothing
  });

  it("colours above 70 and 90 per cent, and warns without refusing", () => {
    expect([level(70), level(70.1), level(90), level(90.1)]).toEqual(["ok", "warn", "warn", "bad"]);
    const f = loadFigures(load({}, 2 * GB), 40);
    expect(f.level).toBe("bad");
    expect(overEstimate(f)).toBe(true);
    // 90 % of 62 GB is 55.8 GB; 35.8 GB of room at 2 GB each is 17 more than the 6 running.
    expect(f.supported).toBe(23);
  });

  it("says nothing it cannot know: no machine figures, no bar", () => {
    const f = loadFigures(load({ used: { rss_bytes: 0, cpu_percent: 0, mem_total_bytes: 0, mem_available_bytes: 0, machine_cpu_percent: 0 } }));
    expect(f.known).toBe(false);
    expect(overEstimate(f)).toBe(false);
  });

  it("rounds bytes the way people compare machines", () => {
    expect(roundBytes(62 * GB)).toEqual({ value: "62", unit: "gb" });
    expect(roundBytes(1.44 * GB)).toEqual({ value: "1.4", unit: "gb" });
    expect(roundBytes(820 * MB)).toEqual({ value: "820", unit: "mb" });
  });
});
