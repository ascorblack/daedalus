// The screens that load as chunks of their own, and the two things a chunk needs beyond `lazy`:
// retrying a download that failed, and rendering at once once it has arrived.

import { type ComponentType, type LazyExoticComponent, lazy } from "react";

/** A loader retried a couple of times before it gives up.
 *
 *  `lazy` remembers the promise it was given, rejection included, so a chunk that failed to arrive
 *  once never arrives at all: re-rendering the screen replays the same rejection and only a reload
 *  recovers. The target here is a phone on a flaky link, where a failed chunk is a normal event and
 *  not a broken build, so the loader is retried before the boundary sees it. */
export function retried<T>(load: () => Promise<T>): () => Promise<T> {
  return async () => {
    for (let attempt = 0; ; attempt++) {
      try {
        return await load();
      } catch (e) {
        if (attempt >= 2) throw e;
        await new Promise((r) => setTimeout(r, 400 * (attempt + 1)));
      }
    }
  };
}

export type Chunk<T extends ComponentType<any>> = LazyExoticComponent<T> & { prefetch: () => void };

/** `lazy`, plus a prefetch after which the first render does not suspend.
 *
 *  A plain `lazy` suspends on its first render even when the module arrived long ago: it is handed a
 *  promise, and a promise answers only on a later tick, by which time the nearest Suspense boundary
 *  has already drawn its fallback. That fallback is what made opening a project from the main chat
 *  empty the column and put "Loading…" where the conversation was for a frame. Once fetched, the
 *  module is handed over through a thenable that answers at once, which `lazy` reads in the same
 *  render. */
export function chunk<T extends ComponentType<any>>(load: () => Promise<{ default: T }>): Chunk<T> {
  let module: { default: T } | null = null;
  let pending: Promise<{ default: T }> | null = null;
  const fetch = () => {
    pending ??= load().then(
      (m) => (module = m),
      (e) => {
        pending = null; // the next render or prefetch tries again
        throw e;
      },
    );
    return pending;
  };
  const component = lazy(() => {
    const ready = module;
    if (!ready) return fetch();
    const now = { then: (resolve: (m: { default: T }) => void) => resolve(ready) };
    return now as unknown as Promise<{ default: T }>;
  });
  return Object.assign(component, { prefetch: () => void fetch().catch(() => undefined) }); // a prefetch that fails is not an error: the render retries
}

/** The conversation: the agents list's, the main chat's and a project's, one chunk and one component,
 *  so fetching it once readies all three. */
export const SessionScreen = chunk(retried(() => import("./screens/Session").then((m) => ({ default: m.SessionScreen }))));

/** Work for the browser's idle time: what the operator is likely to open next. */
export function whenIdle(work: () => void): void {
  const idle = (window as { requestIdleCallback?: (cb: () => void) => void }).requestIdleCallback;
  if (idle) idle(work);
  else window.setTimeout(work, 2000);
}
