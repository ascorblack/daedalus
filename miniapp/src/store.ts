// A small cache in front of the API: a screen renders what it showed last time the moment it
// opens, refreshes in the background, and polls while it stays open. Nothing here survives a
// reload; the point is that switching screens never flashes an empty frame.

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";

type Entry = { data: unknown; at: number; error: string | null };

const cache = new Map<string, Entry>();
const listeners = new Map<string, Set<() => void>>();
const inflight = new Map<string, Promise<unknown>>();

function notify(key: string) {
  for (const l of listeners.get(key) ?? []) l();
}

export function peek<T>(key: string): T | undefined {
  return cache.get(key)?.data as T | undefined;
}

/** Puts a value into the cache without a request (an optimistic update, a fresher copy from a mutation). */
export function prime<T>(key: string, data: T): void {
  cache.set(key, { data, at: Date.now(), error: null });
  notify(key);
}

export function invalidate(prefix: string): void {
  for (const key of cache.keys()) if (key.startsWith(prefix)) inflight.delete(key);
  for (const key of listeners.keys()) if (key.startsWith(prefix)) notify(`refresh:${key}`);
}

async function fetchInto<T>(key: string): Promise<T> {
  const running = inflight.get(key);
  if (running) return running as Promise<T>;
  const p = api
    .get<T>(key)
    .then((data) => {
      cache.set(key, { data, at: Date.now(), error: null });
      notify(key);
      return data;
    })
    .catch((e: Error) => {
      const prev = cache.get(key);
      cache.set(key, { data: prev?.data, at: prev?.at ?? 0, error: e.message || "request failed" });
      notify(key);
      throw e;
    })
    .finally(() => inflight.delete(key));
  inflight.set(key, p);
  return p;
}

export type Query<T> = { data: T | undefined; error: string | null; loading: boolean; refresh: () => Promise<void> };

/**
 * `key` is the API path. `pollMs` refreshes while the component is mounted and the tab is visible;
 * `staleMs` decides whether the cached copy is fresh enough to skip the first request.
 */
export function useQuery<T>(key: string | null, opts: { pollMs?: number; staleMs?: number } = {}): Query<T> {
  const { pollMs = 0, staleMs = 0 } = opts;
  const [, force] = useState(0);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const refresh = useCallback(async () => {
    if (!key) return;
    try {
      await fetchInto<T>(key);
    } catch {
      /* the entry carries the error */
    }
  }, [key]);
  useEffect(() => {
    if (!key) return;
    const on = () => mounted.current && force((n) => n + 1);
    const set = listeners.get(key) ?? new Set();
    set.add(on);
    listeners.set(key, set);
    const refreshers = listeners.get(`refresh:${key}`) ?? new Set();
    const onRefresh = () => void refresh();
    refreshers.add(onRefresh);
    listeners.set(`refresh:${key}`, refreshers);
    const entry = cache.get(key);
    if (!entry || Date.now() - entry.at > staleMs) void refresh();
    let timer: number | undefined;
    if (pollMs > 0) {
      timer = window.setInterval(() => {
        if (document.visibilityState === "visible") void refresh();
      }, pollMs);
    }
    const onVisible = () => {
      if (document.visibilityState === "visible" && pollMs > 0) void refresh();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      set.delete(on);
      refreshers.delete(onRefresh);
      if (timer) window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [key, pollMs, staleMs, refresh]);
  const entry = key ? cache.get(key) : undefined;
  return { data: entry?.data as T | undefined, error: entry?.error ?? null, loading: !!key && !entry, refresh };
}
