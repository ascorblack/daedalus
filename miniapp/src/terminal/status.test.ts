// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { setLang } from "../i18n";
import type { ConnectionState } from "./connection";
import { connectionText } from "./status";

const EVERY: ConnectionState[] = [
  { kind: "connecting" },
  { kind: "live" },
  { kind: "reconnecting", attempt: 1, delayMs: 0 },
  { kind: "reconnecting", attempt: 3, delayMs: 4000 },
  { kind: "exited", code: 0, signal: null },
  { kind: "exited", code: null, signal: "SIGKILL" },
  { kind: "unavailable", reason: "gone" },
  { kind: "unavailable", reason: "environment" },
  { kind: "unavailable", reason: "origin" },
  { kind: "unavailable", reason: "auth" },
  { kind: "proxy-blocked" },
];

afterEach(() => setLang("en"));

describe("connectionText", () => {
  it("has words for every state, in both languages", () => {
    for (const lang of ["en", "ru"] as const) {
      setLang(lang);
      for (const state of EVERY) expect(connectionText(state), JSON.stringify(state)).not.toMatch(/^\[|undefined/);
    }
  });

  it("says when the next attempt is, and how a process ended", () => {
    setLang("en");
    expect(connectionText({ kind: "reconnecting", attempt: 3, delayMs: 4000 })).toBe("Connection lost · reconnecting in 4 s");
    expect(connectionText({ kind: "exited", code: 2, signal: null })).toBe("Process ended · code 2");
    expect(connectionText({ kind: "exited", code: null, signal: "SIGKILL" })).toBe("Process ended · signal SIGKILL");
    setLang("ru");
    expect(connectionText({ kind: "proxy-blocked" })).toMatch(/обратным прокси/);
  });
});
