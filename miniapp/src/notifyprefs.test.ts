import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { NotificationPreferences } from "./api";
import { setLang } from "./i18n";
import { joinQuietHours, muteState, muteUntil, nextCell, preferencesDiff, splitQuietHours, testLines, withCell, withMute } from "./notifyprefs";

function prefs(over: Partial<NotificationPreferences> = {}): NotificationPreferences {
  return {
    matrix: {
      run_finished: { in_app: "on", push: "on", desktop: "on", telegram: "off" },
      question: { in_app: "on", push: "on", desktop: "on", telegram: "on" },
    },
    finished_min_seconds: 30,
    quiet_hours: "",
    muted_projects: {},
    quick_actions: true,
    telegram_covers_push: true,
    keep_days: 30,
    ...over,
  };
}

describe("a cell of the matrix", () => {
  it("steps on → urgent only → off → on", () => {
    expect(nextCell("on")).toBe("urgent");
    expect(nextCell("urgent")).toBe("off");
    expect(nextCell("off")).toBe("on");
  });

  it("changes one channel of one kind and nothing else", () => {
    const before = prefs();
    const after = withCell(before, "run_finished", "push", "off");
    expect(after.matrix.run_finished).toEqual({ in_app: "on", push: "off", desktop: "on", telegram: "off" });
    expect(after.matrix.question).toBe(before.matrix.question);
    expect(before.matrix.run_finished.push).toBe("on");
  });
});

describe("the preferences diff", () => {
  it("names exactly the paths a change touched", () => {
    const before = prefs();
    expect(preferencesDiff(before, withCell(before, "run_finished", "push", "off"))).toEqual(["matrix.run_finished.push"]);
    expect(preferencesDiff(before, { ...before, quiet_hours: "23:00-07:30", quick_actions: false })).toEqual(["quick_actions", "quiet_hours"]);
    expect(preferencesDiff(before, before)).toEqual([]);
  });

  it("sees a project muted and the last mute lifted", () => {
    const now = new Date("2026-09-25T10:00:00Z");
    const muted = withMute(prefs(), "p1", "", now);
    expect(preferencesDiff(prefs(), muted)).toEqual(["muted_projects", "muted_projects.p1"]);
    expect(preferencesDiff(muted, withMute(muted, "p1", null, now))).toEqual(["muted_projects", "muted_projects.p1"]);
  });
});

describe("quiet hours", () => {
  it("split into their ends and join back, across midnight too", () => {
    expect(splitQuietHours("23:00-07:30")).toEqual({ from: "23:00", to: "07:30" });
    expect(joinQuietHours("23:00", "07:30")).toBe("23:00-07:30");
    expect(splitQuietHours("")).toBeNull();
    expect(splitQuietHours("25:00-07:00")).toBeNull();
  });

  it("refuse a window of no length", () => {
    expect(joinQuietHours("08:00", "08:00")).toBe("");
    expect(joinQuietHours("8:00", "09:00")).toBe("");
  });
});

describe("a project's mute", () => {
  const now = new Date(2026, 8, 25, 21, 15);

  it("ends an hour on, the next morning, or never", () => {
    expect(new Date(muteUntil("hour", now)).getTime() - now.getTime()).toBe(3600_000);
    const morning = new Date(muteUntil("tomorrow", now));
    expect([morning.getDate(), morning.getHours(), morning.getMinutes()]).toEqual([26, 8, 0]);
    expect(muteUntil("always", now)).toBe("");
  });

  it("is over once its end has passed, and kept when its end cannot be read", () => {
    const p = prefs({ muted_projects: { a: new Date(now.getTime() + 60_000).toISOString(), b: new Date(now.getTime() - 60_000).toISOString(), c: "", d: "whenever" } });
    expect(muteState(p, "a", now).muted).toBe(true);
    expect(muteState(p, "b", now)).toEqual({ muted: false, until: null });
    expect(muteState(p, "c", now)).toEqual({ muted: true, until: null });
    expect(muteState(p, "d", now).muted).toBe(true);
    expect(muteState(p, "e", now).muted).toBe(false);
  });

  it("drops mutes that ran out while it records a new one", () => {
    const p = prefs({ muted_projects: { old: new Date(now.getTime() - 1).toISOString(), keep: "" } });
    expect(Object.keys(withMute(p, "new", "", now).muted_projects).sort()).toEqual(["keep", "new"]);
    expect(withMute(p, "keep", null, now).muted_projects).toEqual({});
  });
});

describe("the test's outcome", () => {
  beforeEach(() => setLang("en"));
  afterEach(() => setLang("en"));

  it("is one line per channel and one per push device, in words", () => {
    const lines = testLines({
      in_app: "toast",
      push: { devices: [{ id: 1, device: "Chrome · Linux", outcome: "sent" }, { id: 2, device: "Firefox · Android", outcome: "gone" }] },
      desktop: "skipped: no launcher",
      telegram: "failed: chat not found",
    });
    expect(lines).toEqual([
      "In app: shown here",
      "Push: Chrome · Linux, sent",
      "Push: Firefox · Android, gone",
      "Desktop: no desktop launcher is listening",
      "Telegram: failed: chat not found",
    ]);
  });

  it("says when no device has push, and speaks Russian", () => {
    setLang("ru");
    expect(testLines({ push: "skipped: no device", telegram: "skipped: no bot" })).toEqual(["Push: ни на одном устройстве push не включён", "Telegram: бот Telegram не подключён"]);
  });
});
