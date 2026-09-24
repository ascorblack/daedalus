// The arithmetic behind Settings → Notifications, kept apart from the page so each rule can be
// tested with values rather than clicks: how a cell of the matrix steps, which fields a save really
// changes, how quiet hours are written, and when a project's mute ends.

import type { NotificationPreferences, NotifyCell, NotifyChannel } from "./api";
import { t } from "./i18n";

export const CHANNELS: NotifyChannel[] = ["in_app", "push", "desktop", "telegram"];
export const CELLS: NotifyCell[] = ["on", "urgent", "off"];

/** The next state of a cell on a click: on → urgent only → off → on. "Urgent only" sits between the
 *  two because it is the step people take when something is too loud but must not be missed. */
export function nextCell(cell: NotifyCell): NotifyCell {
  return cell === "on" ? "urgent" : cell === "urgent" ? "off" : "on";
}

/** The preferences with one cell changed; the rest of the object is shared, not copied. */
export function withCell(prefs: NotificationPreferences, category: string, channel: NotifyChannel, cell: NotifyCell): NotificationPreferences {
  const row = prefs.matrix[category] ?? { in_app: "on", push: "on", desktop: "on", telegram: "off" };
  return { ...prefs, matrix: { ...prefs.matrix, [category]: { ...row, [channel]: cell } } };
}

function flatten(value: unknown, path: string, out: Map<string, string>): void {
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    const entries = Object.entries(value as Record<string, unknown>);
    // An empty map is a value of its own: unmuting the last project must still count as a change.
    if (entries.length === 0) out.set(path, "{}");
    for (const [key, inner] of entries) flatten(inner, path ? `${path}.${key}` : key, out);
    return;
  }
  out.set(path, JSON.stringify(value));
}

/** The dotted paths whose values differ between two sets of preferences, sorted. A save with none
 *  is not sent. */
export function preferencesDiff(before: NotificationPreferences, after: NotificationPreferences): string[] {
  const a = new Map<string, string>();
  const b = new Map<string, string>();
  flatten(before, "", a);
  flatten(after, "", b);
  const keys = new Set([...a.keys(), ...b.keys()]);
  return [...keys].filter((k) => a.get(k) !== b.get(k)).sort();
}

const HHMM = /^([01]\d|2[0-3]):[0-5]\d$/;

/** `"23:00-07:30"` → its two ends; null for none or for anything malformed. */
export function splitQuietHours(spec: string): { from: string; to: string } | null {
  const [from, to] = spec.split("-").map((s) => s.trim());
  return from && to && HHMM.test(from) && HHMM.test(to) ? { from, to } : null;
}

/** The two ends written the way the host reads them; "" when either is missing or they are equal
 *  (a window of no length would silence nothing and read as a mistake). */
export function joinQuietHours(from: string, to: string): string {
  return HHMM.test(from) && HHMM.test(to) && from !== to ? `${from}-${to}` : "";
}

export type MuteEnd = "hour" | "tomorrow" | "always";
export const MUTE_ENDS: MuteEnd[] = ["hour", "tomorrow", "always"];

/** Where "until tomorrow" ends: the next morning in the reader's own day, not 24 hours on. */
export const MORNING_HOUR = 8;

/** The value stored for a mute that starts now: an ISO moment, or "" for until it is lifted. */
export function muteUntil(end: MuteEnd, now: Date): string {
  if (end === "always") return "";
  if (end === "hour") return new Date(now.getTime() + 3600_000).toISOString();
  const next = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, MORNING_HOUR, 0, 0, 0);
  return next.toISOString();
}

/** Whether a project is muted now, and until when (null: until lifted). A mute that has run out is
 *  not one, exactly as the host reads it; one whose end cannot be read is kept, as the host keeps it. */
export function muteState(prefs: NotificationPreferences, projectId: string, now: Date): { muted: boolean; until: Date | null } {
  if (!(projectId in prefs.muted_projects)) return { muted: false, until: null };
  const raw = prefs.muted_projects[projectId];
  if (!raw) return { muted: true, until: null };
  const end = new Date(raw);
  if (Number.isNaN(end.getTime())) return { muted: true, until: null };
  return end.getTime() > now.getTime() ? { muted: true, until: end } : { muted: false, until: null };
}

/** The preferences with a project muted until `until`, or unmuted with null. Mutes that have run out
 *  are dropped on the way, so the map does not collect every project ever muted for an hour. */
export function withMute(prefs: NotificationPreferences, projectId: string, until: string | null, now: Date): NotificationPreferences {
  const kept: Record<string, string> = {};
  for (const id of Object.keys(prefs.muted_projects)) {
    if (id !== projectId && muteState(prefs, id, now).muted) kept[id] = prefs.muted_projects[id];
  }
  if (until !== null) kept[projectId] = until;
  return { ...prefs, muted_projects: kept };
}

interface TestDevice {
  id: number;
  device: string;
  outcome: string;
}

/** One outcome the host wrote, in words; anything this table does not know is shown as the host
 *  wrote it, since a reason nobody translated is still better than no reason. */
export function outcomeText(outcome: string): string {
  if (outcome === "toast") return t("nset.out.toast");
  if (outcome === "sent" || outcome === "test" || outcome === "general" || outcome === "session") return t("nset.out.sent");
  if (outcome === "skipped: no device") return t("nset.out.nodevice");
  if (outcome === "skipped: no launcher") return t("nset.out.nolauncher");
  if (outcome === "skipped: no bot") return t("nset.out.nobot");
  if (outcome === "skipped: no topic") return t("nset.out.notopic");
  if (outcome.startsWith("failed: ")) return t("nset.out.failed", { why: outcome.slice("failed: ".length) });
  return outcome;
}

/** What the test button reached, one line per channel and one per push device, in channel order. */
export function testLines(delivered: Record<string, unknown>): string[] {
  const lines: string[] = [];
  for (const channel of CHANNELS) {
    const value = delivered[channel];
    if (value === undefined || value === null) continue;
    const name = t(`nset.channel.${channel}`);
    const devices = typeof value === "object" ? (value as { devices?: TestDevice[] }).devices : undefined;
    if (Array.isArray(devices)) {
      if (devices.length === 0) lines.push(t("nset.test.line", { channel: name, outcome: t("nset.out.nodevice") }));
      for (const d of devices) lines.push(t("nset.test.line", { channel: name, outcome: t("nset.out.device", { device: d.device, outcome: outcomeText(d.outcome) }) }));
      continue;
    }
    lines.push(t("nset.test.line", { channel: name, outcome: typeof value === "string" ? outcomeText(value) : JSON.stringify(value) }));
  }
  return lines;
}
