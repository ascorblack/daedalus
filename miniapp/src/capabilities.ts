// What this installation can do, from GET /api/capabilities. The app offers what is there and
// nothing else: a destination that answers 404 is worse than a destination that is not in the nav.

import type { Screen } from "./router";

export type SelfDevMode = "off" | "local" | "server";

export type Capabilities = { selfdev: { mode: SelfDevMode; configured: string; reasons: string[]; missing: string[]; tools: string[] } };

/** The destinations this installation really has. */
export function visibleScreens(list: Screen[], selfdev: SelfDevMode): Screen[] {
  return selfdev === "off" ? list.filter((s) => s !== "changes") : list;
}

/** The word beside a destination's name when it works differently here than the name suggests. */
export function screenTag(s: Screen, selfdev: SelfDevMode, beta: Screen[]): string {
  if (s === "changes" && selfdev === "local") return "local";
  return beta.includes(s) ? "beta" : "";
}
