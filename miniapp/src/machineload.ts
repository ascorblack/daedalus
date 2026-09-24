// What running terminals cost the machine, and what it would carry with the cap filled — the numbers
// behind the load bar, computed here from one `GET /api/terminals/load` so the bar follows the cap as
// the operator types instead of asking the host on every keystroke.
//
// The arithmetic is the host's own (daedalus/terminals/load.py, `project`), repeated on purpose and
// tested against the same figures: memory is judged for the whole machine, because a machine runs out
// of memory for everyone and not for terminals alone.

import type { TerminalLoad } from "./api";

export type Level = "ok" | "warn" | "bad";

export interface LoadFigures {
  cap: number;
  running: number;
  /** Terminals beyond the running ones that the cap would admit (never negative). */
  extra: number;
  total: number;
  /** Everything on the machine now, the terminals included. */
  machineNow: number;
  /** The terminals now: their process trees and the terminal daemon that holds their emulators. */
  terminalsNow: number;
  /** The terminals with the cap filled: now plus `extra` of the likely cost. */
  terminalsAtCap: number;
  /** The machine with the cap filled. */
  machineAtCap: number;
  memPercentNow: number;
  memPercentAtCap: number;
  cpuNow: number;
  cpuAtCap: number;
  level: Level;
  cpuLevel: Level;
  /** The largest cap the estimate carries below the "bad" line; null when nothing can be said. */
  supported: number | null;
  /** Whether there are machine figures at all: a daemon that measured nothing gives none. */
  known: boolean;
}

export function level(percent: number, warn = 70, bad = 90): Level {
  return percent > bad ? "bad" : percent > warn ? "warn" : "ok";
}

/** The figures at `cap` (the configured one when omitted). */
export function loadFigures(load: TerminalLoad, cap: number = load.cap): LoadFigures {
  const warn = load.thresholds?.warn ?? 70;
  const bad = load.thresholds?.bad ?? 90;
  const total = Math.max(0, load.used.mem_total_bytes || 0);
  const available = Math.max(0, load.used.mem_available_bytes || 0);
  const running = load.running;
  const extra = Math.max(0, cap - running);
  const extraRss = extra * load.likely.rss_bytes;
  const machineNow = total ? Math.max(0, total - available) : 0;
  const machineAtCap = total ? machineNow + extraRss : 0;
  const memPercentNow = total ? (100 * machineNow) / total : 0;
  const memPercentAtCap = total ? (100 * machineAtCap) / total : 0;
  const cpus = load.used.cpus || 0;
  const cpuNow = load.used.machine_cpu_percent || 0;
  const cpuAtCap = cpuNow + (cpus ? (extra * load.likely.cpu_percent) / cpus : 0);
  let supported: number | null = null;
  if (total && load.likely.rss_bytes > 0) {
    const room = (bad / 100) * total - machineNow;
    supported = Math.max(running, running + Math.floor(room / load.likely.rss_bytes));
  }
  return {
    cap,
    running,
    extra,
    total,
    machineNow,
    terminalsNow: load.used.rss_bytes,
    terminalsAtCap: load.used.rss_bytes + extraRss,
    machineAtCap,
    memPercentNow,
    memPercentAtCap,
    cpuNow,
    cpuAtCap,
    level: level(memPercentAtCap, warn, bad),
    cpuLevel: level(cpuAtCap, warn, bad),
    supported,
    known: total > 0,
  };
}

/** Whether the cap is more than the estimate says the machine carries: a warning, never a refusal. */
export function overEstimate(f: LoadFigures): boolean {
  return f.known && (f.level === "bad" || f.cpuLevel === "bad");
}

/** A byte count in the unit a person compares machines by: "14 GB", "820 MB". */
export function roundBytes(n: number): { value: string; unit: "gb" | "mb" } {
  const gb = n / (1 << 30);
  if (gb >= 10) return { value: String(Math.round(gb)), unit: "gb" };
  if (gb >= 1) return { value: gb.toFixed(1), unit: "gb" };
  return { value: String(Math.round(n / (1 << 20))), unit: "mb" };
}
