// What this window shows, told to the host.
//
// The host decides whether a finished run was watched, whether a result is still unseen, and later
// whether a notification is worth a push, from what every window says here: whether it is visible,
// whether it has the focus, and which sessions, terminals and projects are on it. It is decided on
// the host because the host is what sends the push; a window only describes itself.
//
// A component declares what it shows with `usePresenceScope` for as long as it is mounted, so a
// split view, or the voice screen with a session inside it, is covered without anybody reading the
// route. The reporter merges every declaration and posts it on a change, on every visibility or
// focus change, every 20 s while the window is visible (a report lives a minute on the host), and
// once more with `keepalive` as the page goes away.

import { useEffect } from "react";
import { api } from "./api";
import { lang } from "./i18n";

export type PresenceKind = "browser" | "pwa" | "telegram" | "window";

export interface PresenceScope {
  session?: string;
  terminal?: string;
  project?: string;
}

export interface PresenceBody {
  client: string;
  kind: PresenceKind;
  visible: boolean;
  focused: boolean;
  sessions: string[];
  terminals: string[];
  projects: string[];
  lang: string;
  tz: string;
}

/** What the host accepts in one report; more than this is not a window describing itself. */
export const LIMITS = { sessions: 4, terminals: 8, projects: 4 } as const;
export const RESEND_MS = 20000;
const CLIENT_KEY = "daedalus.client";

let memoryClient = "";

/** This tab's id: kept for the tab's life, so a reload is the same window and a second tab is another. */
export function clientId(): string {
  try {
    const stored = sessionStorage.getItem(CLIENT_KEY);
    if (stored) return stored;
    const fresh = newId();
    sessionStorage.setItem(CLIENT_KEY, fresh);
    return fresh;
  } catch {
    if (!memoryClient) memoryClient = newId();
    return memoryClient;
  }
}

function newId(): string {
  const random = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  return `tab-${random.replace(/[^A-Za-z0-9]/g, "").slice(0, 24)}`;
}

/** Which kind of window this is: Telegram's, the desktop launcher's, an installed app, or a browser tab. */
export function clientKind(env: { initData?: string; desktopWindow?: boolean; standalone?: boolean }): PresenceKind {
  if (env.initData) return "telegram";
  if (env.desktopWindow) return "window";
  if (env.standalone) return "pwa";
  return "browser";
}

/** Every declaration merged, each list without repeats and cut to what the host accepts. */
export function mergeScopes(scopes: Iterable<PresenceScope>): Pick<PresenceBody, "sessions" | "terminals" | "projects"> {
  const sessions = new Set<string>();
  const terminals = new Set<string>();
  const projects = new Set<string>();
  for (const scope of scopes) {
    if (scope.session) sessions.add(scope.session);
    if (scope.terminal) terminals.add(scope.terminal);
    if (scope.project) projects.add(scope.project);
  }
  return {
    sessions: [...sessions].slice(0, LIMITS.sessions),
    terminals: [...terminals].slice(0, LIMITS.terminals),
    projects: [...projects].slice(0, LIMITS.projects),
  };
}

const scopes = new Map<number, PresenceScope>();
let nextScope = 1;
let changed: (() => void) | null = null;

/** Declare what the calling component shows, for as long as it is mounted. */
export function usePresenceScope(scope: PresenceScope): void {
  const { session, terminal, project } = scope;
  useEffect(() => {
    if (!session && !terminal && !project) return;
    const key = nextScope++;
    scopes.set(key, { session, terminal, project });
    changed?.();
    return () => {
      scopes.delete(key);
      changed?.();
    };
  }, [session, terminal, project]);
}

function timeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch {
    return "";
  }
}

/** This window's kind, as the host is told it in a report and on the event stream. */
export function currentKind(): PresenceKind {
  let standalone = false;
  try {
    standalone = window.matchMedia?.("(display-mode: standalone)").matches ?? false;
  } catch {
    /* no media queries: a browser tab */
  }
  return clientKind({ initData: window.Telegram?.WebApp?.initData, desktopWindow: !!window.daedalus?.window, standalone });
}

/** The report as it stands now. */
export function currentBody(): PresenceBody {
  return {
    client: clientId(),
    kind: currentKind(),
    visible: document.visibilityState === "visible",
    focused: document.hasFocus(),
    ...mergeScopes(scopes.values()),
    lang: lang(),
    tz: timeZone(),
  };
}

function send(body: PresenceBody, keepalive = false): void {
  // Not `api.post`: the answer is a 204 with no body to parse. `keepalive` rather than
  // `sendBeacon` for the last one, because a beacon cannot carry the auth header.
  fetch("/api/presence", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...api.authHeaders() },
    body: JSON.stringify(body),
    keepalive,
  }).catch(() => {
    /* the next report says it again; presence is a hint, not a record */
  });
}

/** Start reporting; returns the function that stops it. Called once the app is signed in. */
export function startPresence(): () => void {
  let timer: ReturnType<typeof setInterval> | null = null;
  let queued = false;
  const report = () => {
    queued = false;
    const body = currentBody();
    send(body);
    // Re-sent while visible only: a hidden window that says nothing is away when its report expires.
    if (body.visible && timer === null) timer = setInterval(() => send(currentBody()), RESEND_MS);
    if (!body.visible && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
  // Scopes change in bursts (a screen unmounts, the next one mounts): one report for the burst.
  const soon = () => {
    if (queued) return;
    queued = true;
    queueMicrotask(report);
  };
  const leave = () => send({ ...currentBody(), visible: false, focused: false }, true);
  changed = soon;
  document.addEventListener("visibilitychange", soon);
  window.addEventListener("focus", soon);
  window.addEventListener("blur", soon);
  window.addEventListener("pagehide", leave);
  report();
  return () => {
    changed = null;
    document.removeEventListener("visibilitychange", soon);
    window.removeEventListener("focus", soon);
    window.removeEventListener("blur", soon);
    window.removeEventListener("pagehide", leave);
    if (timer !== null) clearInterval(timer);
  };
}
