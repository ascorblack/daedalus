// Navigation state lives in the URL: /app/agents/<id>, /app/inbox, /app/settings/models.
// Opening a detail pushes history, so the browser's Back, Android's back gesture and Telegram's
// BackButton all return to where the reader came from, and a reload lands on the same screen.

import { useEffect, useState } from "react";

export const BASE = "/app";

export type Screen = "agents" | "inbox" | "board" | "changes" | "schedules" | "services" | "memory" | "usage" | "health" | "settings";

export const SCREENS: Screen[] = ["agents", "inbox", "board", "changes", "schedules", "services", "memory", "usage", "health", "settings"];

export type Route = {
  screen: Screen;
  /** The session open on the agents screen. */
  session: string | null;
  /** A second session beside the first (wide screens). */
  with: string | null;
  /** The settings section, a board task, an inbox entry… */
  detail: string | null;
  query: URLSearchParams;
};

const ALIASES: Record<string, Screen> = { sessions: "agents", proposals: "changes", cron: "schedules" };

export function parse(pathname = window.location.pathname, search = window.location.search): Route {
  let path = pathname.startsWith(BASE) ? pathname.slice(BASE.length) : pathname;
  path = path.replace(/^\/+|\/+$/g, "");
  const [head, ...rest] = path.split("/").map(decodeURIComponent);
  const query = new URLSearchParams(search);
  const screen = (SCREENS as string[]).includes(head) ? (head as Screen) : ALIASES[head] ?? "agents";
  const detail = rest[0] || null;
  return { screen, session: screen === "agents" ? detail : null, with: screen === "agents" ? query.get("with") : null, detail: screen === "agents" ? null : detail, query };
}

export function pathFor(screen: Screen, detail?: string | null, query?: Record<string, string | null | undefined>): string {
  let p = `${BASE}/${screen}`;
  if (detail) p += `/${encodeURIComponent(detail)}`;
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(query ?? {})) if (v) qs.set(k, v);
  const s = qs.toString();
  return s ? `${p}?${s}` : p;
}

export function sessionPath(id: string, beside?: string | null): string {
  return pathFor("agents", id, { with: beside ?? undefined });
}

const listeners = new Set<() => void>();

function emit() {
  for (const l of listeners) l();
}

export function navigate(path: string, opts: { replace?: boolean } = {}): void {
  const current = window.location.pathname + window.location.search;
  if (current === path) return;
  if (opts.replace) window.history.replaceState(null, "", path);
  else window.history.pushState(null, "", path);
  emit();
}

/** Back when there is somewhere to go back to inside the app; the given fallback otherwise. */
export function back(fallback: string): void {
  if (window.history.length > 1 && (window.history.state as { daedalus?: boolean } | null)?.daedalus !== false && entered > 1) window.history.back();
  else navigate(fallback, { replace: true });
}

/** How many app navigations happened in this document: after a fresh load, Back would leave the app. */
let entered = 1;
window.addEventListener("popstate", () => {
  entered = Math.max(1, entered - 1);
  emit();
});
const push = window.history.pushState.bind(window.history);
window.history.pushState = (data, unused, url) => {
  entered += 1;
  push(data, unused, url);
};

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

/** Legacy addresses still reach the right screen: /app/#settings, /app/?startapp=session_<id>. */
export function migrateLegacyLocation(startParam?: string | null): void {
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
