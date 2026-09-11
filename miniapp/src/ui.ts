// Small UI helpers shared by the screens: Telegram bridge, confirmations, formatting.

import { telegram } from "./api";
import { confirmDialog } from "./dialogs";

/** The Telegram bridge only when the app really runs inside Telegram: the script also loads in a plain
 *  browser, where it reports version 6.0 and rejects every method (showConfirm → WebAppMethodUnsupported). */
function insideTelegram() {
  const tg = telegram();
  return tg && tg.initData ? tg : null;
}

/** Ask before a destructive action: the app's own dialog, which says what the action does. */
export function confirmAsync(text: string, opts: { body?: string; action?: string; danger?: boolean } = {}): Promise<boolean> {
  return confirmDialog({ title: text, body: opts.body, action: opts.action ?? "Confirm", danger: opts.danger ?? true });
}

/** Whether Enter should send: desktop clients send, phones insert a newline. */
export function enterSends(): boolean {
  const platform = insideTelegram()?.platform;
  if (platform && platform !== "unknown") return !["ios", "android", "android_x"].includes(platform);
  return !("ontouchstart" in window) || window.matchMedia?.("(pointer: fine)").matches;
}

export function haptic(kind: "light" | "medium" | "success" | "error" = "light"): void {
  const h = insideTelegram()?.HapticFeedback;
  if (!h) return;
  try {
    if (kind === "success" || kind === "error") h.notificationOccurred(kind);
    else h.impactOccurred(kind);
  } catch {
    /* an old client without haptics */
  }
}

/** Bytes as people read them: 950 B, 12 KB, 1.4 MB. */
export function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
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
