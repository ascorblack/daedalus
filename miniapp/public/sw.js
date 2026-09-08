// Daedalus app shell: hashed assets and icons are cached on first use, the shell itself
// is served network-first with the cache as the offline fallback, and the API is never cached.
const CACHE = "daedalus-shell-v1";
const SHELL = ["/app/", "/app/manifest.webmanifest", "/app/icons/icon-192.png", "/app/icons/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
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
    // Hashed by the build: a cached copy is the right copy for as long as it is referenced.
    event.respondWith(
      caches.match(request).then((hit) => hit || fetch(request).then((response) => {
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
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put("/app/", copy));
        return response;
      }).catch(() => caches.match("/app/")),
    );
  }
});
