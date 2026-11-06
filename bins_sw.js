// INVENTRA Bins: the app opens fast (and shows "no connection" nicely) because its own page and icons are kept on the
// phone. The bin figures are never kept: they always come fresh from the server (.../api/...).
const BASE = "{{BASE}}";                       // "" on its own website, "/bins" inside the order tracking website
const CACHE = "inventra-bins-v2";
const SHELL = [BASE + "/", BASE + "/manifest.json", "/favicon.svg", "/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k.startsWith("inventra-bins") && k !== CACHE)
    .map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith(BASE + "/api/")) return;
  if (!(url.pathname === BASE + "/" || url.pathname.startsWith(BASE + "/") || SHELL.includes(url.pathname))) return;
  // the newest from the server; the kept copy when there is no connection (a bin link opens the app's page)
  e.respondWith(fetch(e.request).then(r => {
    if (r.ok && SHELL.includes(url.pathname)) { const copy = r.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); }
    return r;
  }).catch(() => caches.match(e.request).then(r => r || caches.match(BASE + "/"))));
});
