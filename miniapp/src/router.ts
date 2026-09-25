// Navigation state lives in the URL: /app/agents/<id>, /app/inbox, /app/settings/models.
// Opening a detail pushes history, so the browser's Back, Android's back gesture and Telegram's
// BackButton all return to where the reader came from, and a reload lands on the same screen.

import { useEffect, useState } from "react";

export const BASE = "/app";

export type Screen = "agents" | "voice" | "inbox" | "board" | "terminals" | "harnesses" | "changes" | "schedules" | "services" | "memory" | "usage" | "health" | "settings" | "orchestration";

export const SCREENS: Screen[] = ["agents", "voice", "inbox", "board", "terminals", "harnesses", "changes", "schedules", "services", "memory", "usage", "health", "settings"];
/** Orchestration is a mode of its own rather than a destination of the menu: its item on the rail
 *  (its tab on a phone) goes there, and so does everything that belongs to it. */
const INNER: Screen[] = ["orchestration"];

/** Orchestration mode's home on a desktop: the main orchestrator's chat. */
export const ORCHESTRATION = `${BASE}/orchestration`;
/** On a phone, which has no left column, the list the left column holds on a desktop: orchestration's
 *  home there. */
export const ORCHESTRATION_LIST = `${BASE}/orchestration/projects`;

export type Route = {
  screen: Screen;
  /** The session open on the agents screen. */
  session: string | null;
  /** A second session beside the first (wide screens). */
  with: string | null;
  /** The settings section, a board task, an inbox entry, the terminal shown full screen… */
  detail: string | null;
  /** The project orchestration mode is focused on: /app/orchestration/project/<id>/<page>. */
  project: string | null;
  /** Which of the project's pages: team, board, journal… Null is the project's home, its orchestrator. */
  page: string | null;
  /** What a project page is about: the session of /app/project/<id>/s/<session>, or the staff
   *  member of /app/project/<id>/staff/<member>. */
  inner: string | null;
  query: URLSearchParams;
};

const ALIASES: Record<string, Screen> = { sessions: "agents", proposals: "changes", cron: "schedules" };

export function parse(pathname = window.location.pathname, search = window.location.search): Route {
  let path = canonical(pathname).split("?")[0];
  path = path.startsWith(BASE) ? path.slice(BASE.length) : path;
  path = path.replace(/^\/+|\/+$/g, "");
  const [head, ...rest] = path.split("/").map(decodeURIComponent);
  const query = new URLSearchParams(search);
  const screen = ([...SCREENS, ...INNER] as string[]).includes(head) ? (head as Screen) : ALIASES[head] ?? "agents";
  const detail = rest[0] || null;
  if (screen === "orchestration") {
    if (detail === "project") {
      const page = rest[2] || null;
      return { screen, session: null, with: null, detail: null, project: rest[1] || null, page, inner: page === "s" || page === "staff" ? rest[3] || null : null, query };
    }
    return { screen, session: null, with: null, detail, project: null, page: null, inner: null, query };
  }
  return { screen, session: screen === "agents" ? detail : null, with: screen === "agents" ? query.get("with") : null, detail: screen === "agents" ? null : detail, project: null, page: null, inner: null, query };
}

export function pathFor(screen: Screen, detail?: string | null, query?: Record<string, string | null | undefined>): string {
  let p = `${BASE}/${screen}`;
  if (detail) p += `/${encodeURIComponent(detail)}`;
  return withQuery(p, query);
}

const PROJECT = `${ORCHESTRATION}/project`;

/** One of a project's pages. */
export function projectPagePath(projectId: string, page: string, query?: Record<string, string | null | undefined>): string {
  return withQuery(`${PROJECT}/${encodeURIComponent(projectId)}/${encodeURIComponent(page)}`, query);
}

/** A project's home in its focus mode: the orchestrator's chat, or the way to switch one on. */
export function projectHome(projectId: string, query?: Record<string, string | null | undefined>): string {
  return withQuery(`${PROJECT}/${encodeURIComponent(projectId)}`, query);
}

/** A session of a project, opened without leaving the project's focus mode. */
export function projectSessionPath(projectId: string, sessionId: string, query?: Record<string, string | null | undefined>): string {
  return withQuery(`${PROJECT}/${encodeURIComponent(projectId)}/s/${encodeURIComponent(sessionId)}`, query);
}

/** A command-line staff member's view inside the project: its terminal or its Feed, its messages, its requests. */
export function projectStaffPath(projectId: string, staffId: string, query?: Record<string, string | null | undefined>): string {
  return withQuery(`${PROJECT}/${encodeURIComponent(projectId)}/staff/${encodeURIComponent(staffId)}`, query);
}

/**
 * The address a link means, in the shape the app uses now.
 *
 * The host writes `/app/project/<id>/…` and `/app/main` into notifications, pushes and Telegram
 * messages, and those already sent keep their words: a tap on one weeks later must still land in
 * orchestration mode, where a project and the main chat now live. Everything else is returned as it came.
 */
export function canonical(path: string): string {
  const m = /^\/app\/(project|main)(?=\/|\?|$)(.*)$/.exec(path);
  if (!m) return path;
  if (m[1] === "project") return `${PROJECT}${m[2]}`;
  // The main chat is orchestration's home; whatever followed it (the panel's query) goes along.
  return `${ORCHESTRATION}${m[2].replace(/^\/[^?]*/, "")}`;
}

function withQuery(path: string, query?: Record<string, string | null | undefined>): string {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(query ?? {})) if (v) qs.set(k, v);
  const s = qs.toString();
  return s ? `${path}?${s}` : path;
}

export function sessionPath(id: string, beside?: string | null): string {
  return pathFor("agents", id, { with: beside ?? undefined });
}

const listeners = new Set<() => void>();

function emit() {
  for (const l of listeners) l();
}

/** Each entry the app pushes carries its depth; the entry the document started on is depth 0. */
type HistoryState = { d: number } | null;
let depth = ((window.history.state as HistoryState)?.d ?? 0) as number;
if ((window.history.state as HistoryState)?.d === undefined) {
  try {
    window.history.replaceState({ d: 0 }, "", window.location.href);
  } catch {
    /* a sandboxed frame */
  }
}

export function navigate(path: string, opts: { replace?: boolean } = {}): void {
  path = canonical(path);
  const current = window.location.pathname + window.location.search;
  if (current === path) return;
  if (opts.replace) window.history.replaceState({ d: depth }, "", path);
  else {
    depth += 1;
    window.history.pushState({ d: depth }, "", path);
  }
  emit();
}

/** Back when there is an app entry to go back to; the given fallback otherwise (a fresh load, a deep link). */
export function back(fallback: string): void {
  if (depth > 0) window.history.back();
  else navigate(fallback, { replace: true });
}

window.addEventListener("popstate", () => {
  depth = (window.history.state as HistoryState)?.d ?? 0;
  emit();
});

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parse());
  useEffect(() => {
    const on = () => setRoute(parse());
    listeners.add(on);
    return () => {
      listeners.delete(on);
    };
  }, []);
  return route;
}

/** Legacy addresses still reach the right screen: /app/#settings, /app/?startapp=session_<id>,
 *  /app/project/<id>. A bare /app opens the mode this device was last in, orchestration at
 *  `orchestrationHome` (the main chat on a desktop, the list on a phone). */
export function migrateLegacyLocation(startParam?: string | null, mode?: "agents" | "orchestration", orchestrationHome = ORCHESTRATION): void {
  const here = window.location.pathname + window.location.search;
  if (canonical(here) !== here) {
    navigate(canonical(here), { replace: true });
    return;
  }
  if (mode === "orchestration" && /^\/app\/?$/.test(window.location.pathname) && !window.location.hash && !startParam) {
    navigate(orchestrationHome + window.location.search, { replace: true });
    return;
  }
  const hash = window.location.hash.replace(/^#/, "");
  if (hash && ((SCREENS as string[]).includes(hash) || ALIASES[hash])) {
    navigate(pathFor(ALIASES[hash] ?? (hash as Screen)), { replace: true });
    return;
  }
  const m = startParam ? /^session_([A-Za-z0-9_-]+)$/.exec(startParam) : null;
  if (m) navigate(sessionPath(m[1]), { replace: true });
}

/** Scroll offsets of the list screens, restored when the reader comes back. */
const scrolls = new Map<string, number>();
export function rememberScroll(key: string, top: number): void {
  scrolls.set(key, top);
}
export function recallScroll(key: string): number {
  return scrolls.get(key) ?? 0;
}
