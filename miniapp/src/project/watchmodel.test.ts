// What the watch form sends, and a watch as a sentence: only the fields its kind has, nothing until the
// required ones are filled, and every kind worded in both languages.

import { afterEach, describe, expect, it } from "vitest";
import { setLang } from "../i18n";
import { WATCH_KINDS, emptyWatch, watchBody, watchThenText, watchWhenText } from "./watchmodel";

afterEach(() => setLang("en"));

describe("a watch's request", () => {
  it("sends a staff watch with its member, or for anyone", () => {
    expect(watchBody({ ...emptyWatch(), staff: "Max" })).toEqual({ when: { event: "staff_finished", staff: "Max" }, then: { action: "wake" }, cooldown_minutes: 10, once: false, note: "" });
    expect(watchBody(emptyWatch())?.when).toEqual({ event: "staff_finished" });
  });

  it("needs a member and at least five minutes for a silence", () => {
    expect(watchBody({ ...emptyWatch(), kind: "staff_silent", minutes: "20" })).toBeNull();
    expect(watchBody({ ...emptyWatch(), kind: "staff_silent", staff: "Ira", minutes: "3" })).toBeNull();
    expect(watchBody({ ...emptyWatch(), kind: "staff_silent", staff: "Ira", minutes: "20" })?.when).toEqual({ event: "staff_silent", staff: "Ira", minutes: 20 });
  });

  it("tells a terminal from a member's terminal, and needs a pattern", () => {
    const draft = { ...emptyWatch(), kind: "terminal_output" as const, terminal: "tm-api", regex: "FAILED" };
    expect(watchBody(draft)?.when).toEqual({ event: "terminal_output", terminal: "tm-api", regex: "FAILED" });
    expect(watchBody({ ...draft, terminal: "staff:Max" })?.when).toEqual({ event: "terminal_output", staff: "Max", regex: "FAILED" });
    expect(watchBody({ ...draft, regex: " " })).toBeNull();
  });

  it("sends only what a webhook kind has", () => {
    const ci = watchBody({ ...emptyWatch(), kind: "ci", provider: "github", conclusion: "failure", regex: "left over" });
    expect(ci?.when).toEqual({ event: "ci", provider: "github", conclusion: "failure" });
    expect(watchBody({ ...emptyWatch(), kind: "pr" })).toBeNull();
    expect(watchBody({ ...emptyWatch(), kind: "webhook", provider: "gitlab", regex: "deploy", repo: "left over" })?.when).toEqual({ event: "webhook", provider: "gitlab", regex: "deploy" });
  });

  it("builds each action, and refuses one missing its words or a cooldown under a minute", () => {
    expect(watchBody({ ...emptyWatch(), action: "tell", tellStaff: "Max" })).toBeNull();
    expect(watchBody({ ...emptyWatch(), action: "tell", tellStaff: "Max", tellText: " Rebase " })?.then).toEqual({ action: "tell", staff: "Max", text: "Rebase", when: "now" });
    expect(watchBody({ ...emptyWatch(), action: "notify" })).toBeNull();
    expect(watchBody({ ...emptyWatch(), action: "notify", title: "CI red", level: "urgent" })?.then).toEqual({ action: "notify", title: "CI red", text: "", level: "urgent" });
    expect(watchBody({ ...emptyWatch(), cooldown: "0.5" })).toBeNull();
    expect(watchBody({ ...emptyWatch(), wakeNote: "look" })?.then).toEqual({ action: "wake", note: "look" });
  });
});

describe("a watch as a sentence", () => {
  it("words every kind in both languages", () => {
    for (const lang of ["en", "ru"] as const) {
      setLang(lang);
      for (const kind of WATCH_KINDS) {
        const text = watchWhenText({ event: kind, staff: "Max", minutes: 10, provider: "github", folder_label: "site", regex: "x" });
        expect(text).not.toContain("focus.watch");
        expect(text).not.toMatch(/\{\w+\}/);
      }
      for (const action of ["wake", "tell", "notify"] as const) expect(watchThenText({ action, staff: "Max", text: "hi", title: "t" })).not.toContain("focus.watch");
    }
  });

  it("names the member, the branch and the column it waits for", () => {
    expect(watchWhenText({ event: "staff_finished", staff: "Max" })).toBe("When Max finishes a turn");
    expect(watchWhenText({ event: "git_commit", folder_label: "site", branch: "main" })).toBe("When a commit lands on main in site");
    expect(watchWhenText({ event: "task_moved", to: "review" })).toBe("When any task moves to Review");
  });
});
