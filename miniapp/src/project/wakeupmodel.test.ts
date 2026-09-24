// What a wake-up form sends: exactly one of in_minutes, at (with its offset) or a UTC cron line.

import { describe, expect, it } from "vitest";
import { wakeupBody, wakeupWhen, type WakeupDraft } from "./wakeupmodel";

const base: WakeupDraft = { note: "  Check the build  ", when: "in", minutes: "30", date: "2030-05-01", time: "09:30", cron: "" };

describe("a wake-up's request", () => {
  it("sends minutes from now as a whole number, and nothing for a bad one", () => {
    expect(wakeupBody(base)).toEqual({ note: "Check the build", in_minutes: 30 });
    expect(wakeupBody({ ...base, minutes: "0" })).toBeNull();
    expect(wakeupBody({ ...base, minutes: "2.5" })).toBeNull();
    expect(wakeupBody({ ...base, note: "   " })).toBeNull();
  });

  it("sends a moment with its offset, so the host never guesses the zone", () => {
    const body = wakeupBody({ ...base, when: "once" });
    expect(body?.at).toBe(new Date("2030-05-01T09:30").toISOString());
    expect(body?.in_minutes).toBeUndefined();
    expect(wakeupBody({ ...base, when: "once", date: "" })).toBeNull();
  });

  it("turns a daily time into a UTC cron line, and passes a five-field cron through", () => {
    const daily = wakeupBody({ ...base, when: "daily" });
    expect(daily?.cron).toMatch(/^\d+ \d+ \* \* \*$/);
    expect(wakeupBody({ ...base, when: "cron", cron: " 0  9 * * 1-5 " })).toEqual({ note: "Check the build", cron: "0 9 * * 1-5" });
    expect(wakeupBody({ ...base, when: "cron", cron: "0 9 * *" })).toBeNull();
  });

  it("describes a one-off by its moment and a recurring one by its cadence", () => {
    expect(wakeupWhen({ cron: null, at: "2030-05-01T07:30:00Z", next_run_at: "2030-05-01T07:30:00Z" })).not.toBe("");
    expect(wakeupWhen({ cron: "0 */2 * * *", at: null, next_run_at: null })).not.toContain("*/2");
  });
});
