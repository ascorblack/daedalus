// What the wake-ups page sends and shows, decided without a browser: the operator's "when" turned
// into the one field the host takes, and a wake-up's cadence as a line in the reader's own clock.

import type { Wakeup } from "../api";
import { absTime, cronFor, describeCron } from "../format";
import { t } from "../i18n";

export type WakeupWhen = "in" | "once" | "daily" | "cron";
export const WAKEUP_WHENS: WakeupWhen[] = ["in", "once", "daily", "cron"];

export type WakeupDraft = { note: string; when: WakeupWhen; minutes: string; date: string; time: string; cron: string };

export type WakeupBody = { note: string; in_minutes?: number; at?: string; cron?: string };

/** The request for a draft, or null while it is not one yet. A moment is sent with its offset, so the
 *  host never has to guess the reader's zone; a daily time becomes a UTC cron line like every schedule. */
export function wakeupBody(d: WakeupDraft): WakeupBody | null {
  const note = d.note.trim();
  if (!note) return null;
  if (d.when === "in") {
    const minutes = Number(d.minutes);
    return Number.isInteger(minutes) && minutes >= 1 ? { note, in_minutes: minutes } : null;
  }
  if (d.when === "once") {
    const at = new Date(`${d.date}T${d.time}`);
    return Number.isNaN(at.getTime()) ? null : { note, at: at.toISOString() };
  }
  if (d.when === "daily") {
    const [h, m] = d.time.split(":").map(Number);
    return Number.isInteger(h) && Number.isInteger(m) ? { note, cron: cronFor("daily", h, m) } : null;
  }
  const cron = d.cron.trim().split(/\s+/).join(" ");
  return cron.split(" ").length === 5 ? { note, cron } : null;
}

/** "Every day at 09:00" or "Once, 3 Oct, 14:30". */
export function wakeupWhen(w: Pick<Wakeup, "cron" | "at" | "next_run_at">): string {
  if (w.cron) return describeCron(w.cron);
  const at = w.at ?? w.next_run_at;
  return at ? t("fmt.once", { when: absTime(at) }) : "";
}
