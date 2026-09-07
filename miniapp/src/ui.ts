// Small UI helpers shared by the screens: Telegram bridge, confirmations, formatting.

import { telegram } from "./api";

/** Ask before a destructive action: Telegram's own dialog inside the app, the browser's outside it. */
export function confirmAsync(text: string): Promise<boolean> {
  const tg = telegram();
  if (tg?.showConfirm) {
    return new Promise((resolve) => tg.showConfirm!(text, (ok) => resolve(!!ok)));
  }
  try {
    return Promise.resolve(window.confirm(text));
  } catch {
    // A sandboxed frame refuses confirm(): fall through to "yes", the caller's button was an explicit tap.
    return Promise.resolve(true);
  }
}

/** Whether Enter should send: desktop clients send, phones insert a newline. */
export function enterSends(): boolean {
  const platform = telegram()?.platform;
  if (platform) return !["ios", "android", "android_x"].includes(platform);
  return !("ontouchstart" in window);
}

export function haptic(kind: "light" | "medium" | "success" | "error" = "light"): void {
  const h = telegram()?.HapticFeedback;
  if (!h) return;
  if (kind === "success" || kind === "error") h.notificationOccurred(kind);
  else h.impactOccurred(kind);
}

/** Compact token counts: 71.1M, 28k, 950. */
export function fmtTok(n: number | null | undefined): string {
  const v = n ?? 0;
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 10_000) return `${Math.round(v / 1000)}k`;
  return v.toLocaleString();
}

/** A number field's value, or null when it is empty or not a number (then nothing is saved). */
export function numInput(raw: string, min?: number): number | null {
  if (raw.trim() === "") return null;
  const v = Number(raw);
  if (Number.isNaN(v) || !Number.isFinite(v)) return null;
  if (min !== undefined && v < min) return null;
  return v;
}

/** A readable message for a failed request. */
export function errorText(e: unknown): string {
  const msg = e instanceof Error ? e.message : String(e);
  if (!msg || msg === "Failed to fetch" || msg === "Load failed" || msg === "NetworkError when attempting to fetch resource.") return "No connection to the bot";
  return msg;
}
