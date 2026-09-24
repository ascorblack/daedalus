// Daedalus app shell: hashed assets and icons are cached on first use, the shell itself
// is served network-first with the cache as the offline fallback, and the API is never cached.
//
// Two rules keep a deploy from breaking a page that is already open. Only an `ok` response is ever
// written to the cache — a 404 or a 502 for a chunk during a deploy would otherwise be served as
// that chunk from then on, and the screen would stay broken after the deploy finished. And the new
// worker waits: it does not take over a page whose build it is about to delete the files of, so a
// page opened on the previous build keeps its chunks until the reader reloads it.
const CACHE = "daedalus-shell-v4";
const SHELL = ["/app/", "/app/manifest.webmanifest", "/app/icons/icon-192.png", "/app/icons/icon-512.png"];

self.addEventListener("install", (event) => {
  // No skipWaiting: an open page is mid-session, and its lazy chunks are the ones this build
  // replaces. The new worker activates when the last page on the old one has gone.
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
});

self.addEventListener("activate", (event) => {
  // By now no page is running the build whose files these are.
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;
  if (url.pathname.startsWith("/app/assets/") || url.pathname.startsWith("/app/icons/")) {
    // Hashed by the build — the entry, the stylesheet and one chunk per screen: a cached copy is
    // the right copy for as long as it is referenced, and a screen that has been opened once opens
    // again without the network.
    event.respondWith(
      caches.match(request).then((hit) => hit || fetch(request).then((response) => {
        // A 404 for a chunk means the build moved; caching it would make that permanent.
        if (!response.ok) return response;
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(request, copy));
        return response;
      })),
    );
    return;
  }
  if (request.mode === "navigate" || url.pathname === "/app/" || url.pathname === "/app/index.html") {
    event.respondWith(
      fetch(request).then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put("/app/", copy));
        }
        return response;
      }).catch(() => caches.match("/app/")),
    );
  }
});

// Web Push. The host decides what is pushed and encrypts it for this browser; what arrives here is
// shown, and the buttons on it are answered from here without opening the app.
//
// Every push must show a notification: a browser that sees pushes with nothing shown treats the
// site as abusing push (Safari cancels the subscription). The one message that shows nothing is a
// withdrawal — a request answered elsewhere — and the host never sends that to Apple's service.
const APP = "/app/";

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = {};
  }
  event.waitUntil(onPush(data));
});

async function badge(unseen) {
  const nav = self.navigator;
  if (typeof unseen !== "number" || !nav || !("setAppBadge" in nav)) return;
  try {
    if (unseen > 0) await nav.setAppBadge(unseen);
    else await nav.clearAppBadge();
  } catch {
    /* a platform that has the call and refuses it: the badge is a nicety */
  }
}

async function onPush(data) {
  await badge(data.unseen);
  if (data.kind === "withdraw") {
    const shown = await self.registration.getNotifications({ tag: data.tag });
    for (const notification of shown) notification.close();
    return;
  }
  const actions = Array.isArray(data.actions) ? data.actions.map((a) => ({ action: String(a.id), title: String(a.label) })) : [];
  const options = {
    body: data.body || "",
    tag: data.tag || undefined,
    // A repeat of the same thing replaces the old notification; renotify makes the replacement heard,
    // since the host sends a repeat only when it is worth hearing.
    renotify: !!data.tag,
    requireInteraction: data.level === "urgent",
    icon: "/app/icons/icon-192.png",
    badge: "/app/icons/icon-192.png",
    timestamp: Date.parse(data.at || "") || Date.now(),
    data: { id: data.id, link: data.link || APP, token: data.token || "" },
  };
  if (actions.length) options.actions = actions;
  return self.registration.showNotification(data.title || "Daedalus", options);
}

self.addEventListener("notificationclick", (event) => {
  const notification = event.notification;
  const data = notification.data || {};
  notification.close();
  event.waitUntil(event.action ? act(data, event.action) : openApp(data.link));
});

async function act(data, action) {
  try {
    const subscription = await self.registration.pushManager.getSubscription();
    const response = await fetch(`/api/notifications/${encodeURIComponent(data.id)}/act`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, token: data.token || undefined, via: "push", endpoint: subscription ? subscription.endpoint : undefined }),
    });
    // 409: answered already, from somewhere else; nothing is left to do here either.
    if (response.ok || response.status === 409) return;
  } catch {
    /* offline, or the host is restarting: the app can still answer it */
  }
  return openApp(data.link);
}

async function openApp(link) {
  const path = typeof link === "string" && link.startsWith(APP) ? link : APP;
  const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  const open = windows.find((w) => new URL(w.url).pathname.startsWith(APP));
  if (open) {
    // The app moves itself to the item: no reload, and the conversation it had open keeps its state.
    open.postMessage({ type: "daedalus.open", link: path });
    return open.focus();
  }
  return self.clients.openWindow(path);
}

self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil(resubscribe(event.oldSubscription, event.newSubscription));
});

function keyBytes(base64url) {
  const raw = atob(base64url.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (base64url.length % 4)) % 4));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

function b64url(buffer) {
  let text = "";
  for (const byte of new Uint8Array(buffer)) text += String.fromCharCode(byte);
  return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// The browser replaced the subscription on its own, usually with no page open. The old
// subscription's secret proves to the host that this is the same device; without an old one, the
// sign-in cookie is the only way in, and if that is refused the app re-registers on its next start.
async function resubscribe(old, fresh) {
  let key = old && old.options ? old.options.applicationServerKey : null;
  if (!key) {
    const response = await fetch("/api/push/config", { credentials: "include" });
    if (!response.ok) return;
    const config = await response.json();
    if (!config.public_key) return;
    key = keyBytes(config.public_key);
  }
  const subscription = fresh || (await self.registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key }));
  const body = subscription.toJSON();
  const oldAuth = old && old.getKey ? old.getKey("auth") : null;
  if (old && oldAuth) {
    await fetch("/api/push/subscriptions/renew", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, old_endpoint: old.endpoint, old_auth: b64url(oldAuth) }),
    });
    return;
  }
  await fetch("/api/push/subscriptions", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
