/* Service worker: caches only the static app shell (no patient data). /api/* is never cached. */
const CACHE = "hp-shell-v9";
const SHELL = ["/", "/static/css/app.css", "/static/css/mobile.css", "/static/js/shell.js", "/manifest.webmanifest", "/static/icons/icon-192.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const req = e.request; const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api/")) return; // never cache API (patient data)
  // network-first for everything static, fall back to cache when offline
  e.respondWith(
    fetch(req).then((res) => {
      if (res.ok && (url.pathname.startsWith("/static/") || url.pathname === "/")) {
        const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy));
      }
      return res;
    }).catch(() => caches.match(req).then((r) => r || (req.mode === "navigate" ? caches.match("/") : Response.error())))
  );
});
