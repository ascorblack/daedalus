// What the app reads about the main chat: one listing, kept current by the event stream. A card
// appears when a request is linked to a dispatch and closes when it is answered anywhere, without
// anyone waking the main orchestrator's model — the host's events say "look again", this reads.

import type { MainView } from "../api";
import { useEvent, useStreamUp } from "../events";
import { invalidate, useQuery } from "../store";

export const MAIN_KEY = "/api/main";

/** The main chat: its session (empty until first opened), the dispatches, the questions. */
export function useMain(enabled = true) {
  const live = useStreamUp();
  const query = useQuery<MainView>(enabled ? MAIN_KEY : null, { pollMs: live ? 60000 : 10000, staleMs: 3000 });
  useEvent(["dispatch.", "ask.", "permission.", "project.changed"], () => invalidate(MAIN_KEY));
  // An answer of the wrong shape (an older host without the route) draws nothing rather than breaking the column.
  const data = query.data && Array.isArray(query.data.dispatches) && Array.isArray(query.data.asks) ? query.data : null;
  return { data, error: query.error, loading: query.loading };
}
