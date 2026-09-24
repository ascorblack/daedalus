// Web Push for this device: whether it can have it, turning it on and off, and keeping the host's
// list of devices in step with the browser's own subscription.
//
// Push reaches the site installed as an app, not the Telegram Mini App: inside Telegram the bot is
// the push, and a second buzz for the same thing would be noise. An iPhone or iPad gets Web Push
// only once the site is on the Home Screen, so there the answer is an instruction rather than a
// button. The service worker (public/sw.js) shows what arrives and answers the lock-screen buttons.

import { useCallback, useEffect, useState } from "react";
import { api } from "./api";

export type PushState = "unsupported" | "telegram" | "needs-home-screen" | "unavailable" | "denied" | "off" | "on";

/** What the browser offers, read once; a separate value so every platform's shape can be tested. */
export interface PushEnv {
  telegram: boolean;
  secure: boolean;
  serviceWorker: boolean;
  pushManager: boolean;
  notification: boolean;
  ios: boolean;
  standalone: boolean;
  permission: NotificationPermission;
}

export interface PushConfig {
  available: boolean;
  reason: string;
  public_key: string;
}

export interface PushDevice {
  id: number;
  endpoint: string;
  device: string;
  user_agent: string;
  created_at: string;
  last_ok_at: string | null;
  failures: number;
  last_error: string | null;
  apple: boolean;
}

export function detectEnv(): PushEnv {
  const nav = typeof navigator !== "undefined" ? navigator : ({} as Navigator);
  const ua = nav.userAgent ?? "";
  // iPadOS reports itself as a Mac; the touch screen gives it away.
  const ios = /iPad|iPhone|iPod/.test(ua) || (nav.platform === "MacIntel" && (nav.maxTouchPoints ?? 0) > 1);
  const standalone =
    (typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia("(display-mode: standalone)").matches) ||
    (nav as Navigator & { standalone?: boolean }).standalone === true;
  return {
    telegram: typeof window !== "undefined" && !!window.Telegram?.WebApp?.initData,
    // The browser's own word for it: https, or the machine itself (which a browser also trusts).
    // Plain http from the machine then reaches the host's answer, "no public https address", which
    // says what to change; "unsupported" would not.
    secure: typeof window !== "undefined" && (window.isSecureContext ?? window.location.protocol === "https:"),
    serviceWorker: "serviceWorker" in nav,
    pushManager: typeof window !== "undefined" && "PushManager" in window,
    notification: typeof Notification !== "undefined",
    ios,
    standalone,
    permission: typeof Notification !== "undefined" ? Notification.permission : "default",
  };
}

/** The one word for this device. The order matters: an iPhone in Safari has no PushManager, and the
 *  helpful answer there is "add it to the Home Screen", not "unsupported". */
export function pushState(env: PushEnv, facts: { subscribed: boolean; hostReason: string }): PushState {
  if (env.telegram) return "telegram";
  if (env.ios && !env.standalone) return "needs-home-screen";
  if (!env.secure || !env.serviceWorker || !env.pushManager || !env.notification) return "unsupported";
  if (facts.hostReason) return "unavailable";
  if (env.permission === "denied") return "denied";
  return facts.subscribed && env.permission === "granted" ? "on" : "off";
}

/** A short name for this device in the host's list, from the user agent: products, not prose. */
export function deviceName(ua: string): string {
  const system = /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "Android" : /Windows/.test(ua) ? "Windows" : /Mac OS X|Macintosh/.test(ua) ? "macOS" : /Linux/.test(ua) ? "Linux" : "";
  const browser = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /OPR\//.test(ua) ? "Opera" : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : "";
  return [browser, system].filter(Boolean).join(" · ") || "Browser";
}

/** The VAPID key as the bytes `subscribe` wants. */
export function keyBytes(base64url: string): Uint8Array {
  const padded = base64url.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (base64url.length % 4)) % 4);
  const raw = atob(padded);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

function sameKey(subscription: PushSubscription, publicKey: string): boolean {
  const current = subscription.options?.applicationServerKey;
  if (!current) return true; // a browser that does not say: trust it rather than churn the subscription
  const a = new Uint8Array(current);
  const b = keyBytes(publicKey);
  return a.length === b.length && a.every((v, i) => v === b[i]);
}

async function registration(): Promise<ServiceWorkerRegistration | null> {
  if (!("serviceWorker" in navigator)) return null;
  return (await navigator.serviceWorker.getRegistration("/app/")) ?? null;
}

async function currentSubscription(): Promise<PushSubscription | null> {
  const reg = await registration();
  return reg ? reg.pushManager.getSubscription() : null;
}

async function post(subscription: PushSubscription): Promise<void> {
  await api.post("/api/push/subscriptions", { ...subscription.toJSON(), device: deviceName(navigator.userAgent) });
}

/** Where this device stands, asking the host only when the browser could have push at all. */
export async function readPushState(env: PushEnv = detectEnv()): Promise<PushState> {
  const early = pushState(env, { subscribed: false, hostReason: "" });
  if (early === "telegram" || early === "needs-home-screen" || early === "unsupported") return early;
  const config = await api.get<PushConfig>("/api/push/config");
  if (!config.available) return pushState(env, { subscribed: false, hostReason: config.reason || "unavailable" });
  const subscription = await currentSubscription();
  if (!subscription) return pushState(env, { subscribed: false, hostReason: "" });
  const { subscriptions } = await api.get<{ subscriptions: PushDevice[] }>("/api/push/subscriptions");
  return pushState(env, { subscribed: subscriptions.some((d) => d.endpoint === subscription.endpoint), hostReason: "" });
}

/** Turn push on. Called from the click itself: Safari and Firefox grant the permission prompt only
 *  inside a user gesture, so asking is the first thing it does, before any request to the host. */
export async function enablePush(): Promise<PushState> {
  const permission = await Notification.requestPermission();
  if (permission !== "granted") return permission === "denied" ? "denied" : "off";
  const config = await api.get<PushConfig>("/api/push/config");
  if (!config.available) return "unavailable";
  const reg = (await registration()) ?? (await navigator.serviceWorker.ready);
  let subscription = await reg.pushManager.getSubscription();
  if (subscription && !sameKey(subscription, config.public_key)) {
    // Made for another key (the host's state was restored from elsewhere): no message would verify.
    await subscription.unsubscribe();
    subscription = null;
  }
  subscription ??= await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(config.public_key) as BufferSource });
  await post(subscription);
  return "on";
}

export async function disablePush(): Promise<PushState> {
  const subscription = await currentSubscription();
  if (subscription) {
    const { subscriptions } = await api.get<{ subscriptions: PushDevice[] }>("/api/push/subscriptions");
    const mine = subscriptions.find((d) => d.endpoint === subscription.endpoint);
    if (mine) await api.delete(`/api/push/subscriptions/${mine.id}`);
    await subscription.unsubscribe();
  }
  return "off";
}

/** On start: a device the host forgot (it failed for a week, or the list was cleared) is added again,
 *  since the browser still believes it is subscribed and would otherwise wait for pushes forever. */
export async function syncPush(env: PushEnv = detectEnv()): Promise<void> {
  if (pushState(env, { subscribed: true, hostReason: "" }) !== "on") return;
  try {
    const subscription = await currentSubscription();
    if (!subscription) return;
    const { subscriptions } = await api.get<{ subscriptions: PushDevice[] }>("/api/push/subscriptions");
    if (!subscriptions.some((d) => d.endpoint === subscription.endpoint)) await post(subscription);
  } catch {
    /* the host without push, or offline: the next start tries again */
  }
}

/** A tap on a notification while the app is open arrives as a message from the service worker;
 *  moving inside the app keeps the page instead of reloading it. */
export function listenForOpen(open: (path: string) => void): () => void {
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return () => undefined;
  const onMessage = (event: MessageEvent) => {
    const data = event.data as { type?: string; link?: string } | null;
    if (data?.type === "daedalus.open" && typeof data.link === "string" && data.link.startsWith("/app/")) open(data.link);
  };
  navigator.serviceWorker.addEventListener("message", onMessage);
  return () => navigator.serviceWorker.removeEventListener("message", onMessage);
}

/** The state for a screen, with the two actions; `busy` covers the prompt and the round trips. */
export function usePush(enabled = true): { state: PushState | null; busy: boolean; enable: () => Promise<void>; disable: () => Promise<void>; error: string } {
  const [state, setState] = useState<PushState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!enabled) return undefined;
    let live = true;
    readPushState()
      .then((s) => live && setState(s))
      .catch(() => live && setState("unavailable"));
    return () => {
      live = false;
    };
  }, [enabled]);
  const run = useCallback(async (action: () => Promise<PushState>) => {
    setBusy(true);
    setError("");
    try {
      setState(await action());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, []);
  return { state, busy, error, enable: () => run(enablePush), disable: () => run(disablePush) };
}
