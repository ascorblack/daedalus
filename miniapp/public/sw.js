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
