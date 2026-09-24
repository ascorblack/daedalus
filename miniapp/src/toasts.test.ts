// @vitest-environment jsdom
// The toast rules as rules: how many are shown, in which order, for how long, and which events never
// become a toast at all.

import { describe, expect, it } from "vitest";
import type { Notification } from "./api";
import { ACTIONABLE_MS, STACK_MAX, TOAST_MS, ToastQueue, shouldToast, type ToastContext } from "./toasts";

function entry(id: number, over: Partial<Notification> = {}): Notification {
  return {
    id, at: "2026-09-24T10:00:00Z", updated_at: "2026-09-24T10:00:00Z", category: "run_finished", kind: "run", level: "normal", tone: "ok",
    title: `Entry ${id}`, body: "", link: "", session_id: null, run_id: null, project_id: null, staff_id: null, terminal_id: null, source: "run",
    dedupe_key: null, request_ref: null, count: 1, actions: [], seen: false, resolved: null, needs_you: false, delivered: {}, ...over,
  };
}

const permission = (id: number) => entry(id, { category: "permission", level: "urgent", needs_you: true, request_ref: `policy:s:${id}`, actions: [{ id: "allow", label: "Allow", style: "primary", quick: true }, { id: "deny", label: "Deny", style: "default", quick: true }, { id: "open", label: "Open", style: "ghost", quick: false }] });

function clock(start = 1_000_000) {
  const c = { now: start, advance: (ms: number) => { c.now += ms; } };
  return c;
}

describe("the queue", () => {
  it("shows at most three, newest on top, and keeps the rest in line", () => {
    const c = clock();
    const q = new ToastQueue(STACK_MAX, () => c.now);
    for (let i = 1; i <= 5; i++) q.push(entry(i), i);
    expect(q.visible().map((i) => i.entry.id)).toEqual([3, 2, 1]);
    expect(q.waiting.map((i) => i.entry.id)).toEqual([4, 5]);
  });

  it("lets each one go after its time and fills its place from the line", () => {
    const c = clock();
    const q = new ToastQueue(STACK_MAX, () => c.now);
    for (let i = 1; i <= 5; i++) q.push(entry(i), i);
    c.advance(TOAST_MS - 1);
    q.tick();
    expect(q.visible()).toHaveLength(3);
    c.advance(1);
    q.tick();
    expect(q.visible().map((i) => i.entry.id)).toEqual([5, 4]);
    c.advance(TOAST_MS);
    expect(q.tick()).toBeNull();
    expect(q.visible()).toEqual([]);
  });

  it("gives a request to answer longer than a line to read", () => {
    const c = clock();
    const q = new ToastQueue(STACK_MAX, () => c.now);
    q.push(permission(1), 1);
    q.push(entry(2), 2);
    c.advance(TOAST_MS);
    q.tick();
    expect(q.visible().map((i) => i.entry.id)).toEqual([1]);
    c.advance(ACTIONABLE_MS - TOAST_MS);
    q.tick();
    expect(q.visible()).toEqual([]);
  });

  it("stops the clock while the reader holds the stack", () => {
    const c = clock();
    const q = new ToastQueue(STACK_MAX, () => c.now);
    q.push(entry(1), 1);
    c.advance(TOAST_MS - 1000);
    q.pause();
    c.advance(60_000);
    expect(q.tick()).toBeNull();
    expect(q.visible()).toHaveLength(1);
    q.resume();
    c.advance(999);
    q.tick();
    expect(q.visible()).toHaveLength(1);
    c.advance(1);
    q.tick();
    expect(q.visible()).toEqual([]);
  });

  it("replaces a repeat of an entry already shown instead of stacking it", () => {
    const c = clock();
    const q = new ToastQueue(STACK_MAX, () => c.now);
    q.push(entry(1), 1);
    c.advance(5000);
    q.push(entry(1, { title: "Again", count: 2 }), 9);
    expect(q.visible()).toHaveLength(1);
    expect(q.visible()[0].entry.title).toBe("Again");
    c.advance(5000);
    q.tick();
    expect(q.visible()).toHaveLength(1);
  });

  it("dismisses one and lets the next in", () => {
    const q = new ToastQueue(STACK_MAX, () => 0);
    for (let i = 1; i <= 4; i++) q.push(entry(i), i);
    expect(q.dismiss(2)).toBe(true);
    expect(q.visible().map((i) => i.entry.id)).toEqual([4, 3, 1]);
    expect(q.dismiss(42)).toBe(false);
  });

  it("keeps one on a phone and puts the others back in line", () => {
    const q = new ToastQueue(STACK_MAX, () => 0);
    for (let i = 1; i <= 3; i++) q.push(entry(i), i);
    q.setMax(1);
    expect(q.visible().map((i) => i.entry.id)).toEqual([3]);
    expect(q.waiting.map((i) => i.entry.id)).toEqual([2, 1]);
  });
});

describe("which events become a toast", () => {
  const here: ToastContext = { visible: true, shown: { sessions: new Set(["s-open"]), terminals: new Set(["t-open"]), projects: new Set(["p-open"]) } };
  const live = { replayed: false };
  const payload = (n: Notification, toast = true) => ({ notification: n, toast, deliver: {}, merged: false, summary: { unseen: 1, needs_you: 0 } });

  it("raises a live one the host marked", () => {
    expect(shouldToast(payload(entry(1, { session_id: "s-other" })), live, here)).toBe(true);
  });

  it("never raises a replay, an unmarked one, a quiet one or an answered one", () => {
    expect(shouldToast(payload(entry(1)), { replayed: true }, here)).toBe(false);
    expect(shouldToast(payload(entry(1), false), live, here)).toBe(false);
    expect(shouldToast(payload(entry(1, { level: "quiet" })), live, here)).toBe(false);
    expect(shouldToast(payload(entry(1, { resolved: "allow" })), live, here)).toBe(false);
  });

  it("stays quiet in a hidden page", () => {
    expect(shouldToast(payload(entry(1)), live, { ...here, visible: false })).toBe(false);
  });

  it("says nothing about what the window already shows", () => {
    expect(shouldToast(payload(entry(1, { session_id: "s-open" })), live, here)).toBe(false);
    expect(shouldToast(payload(entry(1, { terminal_id: "t-open" })), live, here)).toBe(false);
    expect(shouldToast(payload(entry(1, { project_id: "p-open" })), live, here)).toBe(false);
    // A session of that project is not the project's page.
    expect(shouldToast(payload(entry(1, { project_id: "p-open", session_id: "s-other" })), live, here)).toBe(true);
  });
});
